"""Reply to a digest with Instagram handles to add (or `remove @handle`) them."""

import email
import imaplib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import Message
from email.policy import default as default_policy
from email.utils import parseaddr

import requests

from .config import Config, add_accounts, read_accounts, remove_accounts
from .graph import AuthError, Graph, GraphError, NotSupported
from .send import EPILOG_MSGID_DOMAIN, send_email
from .state import State

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
LOOKBACK = timedelta(days=7)
FIRST_DIGEST_WINDOW = timedelta(days=7)
HANDLE = re.compile(r"^@?([a-z0-9._]{1,30})$")
IG_URL = re.compile(r"instagram\.com/([A-Za-z0-9._]{1,30})", re.I)
REMOVE_WORDS = {"remove", "delete", "unfollow", "drop"}
ADD_WORDS = {"add", "follow"}
FILLER = {"add", "these", "this", "please", "pls", "and", "also", "plus", "thanks", "thank", "you", "thx",
          "hi", "hey", "hello", "ok", "okay", "cheers", "best", "x", "xx", "xo", "ty"}
NOT_PROFILES = {"p", "reel", "reels", "stories", "explore", "tv"}
QUOTE_START = re.compile(r"^\s*(On .*(\n.*)?wrote:|-{2,}\s*Original Message|From: .*Epilog)", re.M)


@dataclass
class Reply:
    message_id: str
    subject: str
    body: str


@dataclass
class Changes:
    added: list[str]
    removed: list[str]
    already: list[str]
    unsupported: list[str]
    not_found_for_removal: list[str]


def process_replies(cfg: Config, state: State) -> int:
    """Handle new replies to digests. Returns how many replies were processed."""
    replies = _fetch_replies(cfg, set(state.processed_replies))
    for reply in replies:
        adds, removes = parse_handles(reply.body)
        log.info("Reply %s: add %s, remove %s", reply.message_id, adds, removes)
        changes = _apply(cfg, adds, removes)
        first_digest = state.welcome_pending and bool(changes.added)
        send_email(cfg, _reply_subject(reply.subject), _confirmation(changes, first_digest),
                   in_reply_to=reply.message_id)
        state.mark_reply(reply.message_id)
        state.save()
        if first_digest:
            _send_first_digest(cfg, state)
    return len(replies)


def _send_first_digest(cfg: Config, state: State) -> None:
    """Right after onboarding, show the past week so the new setup visibly works."""
    from .digest import collect, deliver, save_seen  # digest → render; keep inbox light to import

    digest, newest = collect(cfg, read_accounts(), state, since=FIRST_DIGEST_WINDOW)
    if digest.post_count:
        deliver(cfg, digest)
    state.welcome_pending = False
    save_seen(state, newest)


def parse_handles(body: str) -> tuple[list[str], list[str]]:
    text = QUOTE_START.split(body, maxsplit=1)[0]
    adds: list[str] = []
    removes: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(">") or line.lower().startswith("sent from my"):
            continue
        urls = [u.lower() for u in IG_URL.findall(line) if u.lower() not in NOT_PROFILES]
        tokens = [t.strip(".!?:") for t in re.split(r"[\s,;]+", IG_URL.sub(" ", line).lower())]
        tokens = [t for t in tokens if t]
        target, command = adds, False
        if tokens and tokens[0] in REMOVE_WORDS:
            target, tokens, command = removes, tokens[1:], True
        elif tokens and tokens[0] in ADD_WORDS:
            tokens, command = tokens[1:], True
        words = [t for t in tokens if t.lstrip("@") not in FILLER]
        is_list = command or len(words) == 1 or "," in line or ";" in line
        # In a sentence, plain words ("add", "the") look like handles too, so only
        # trust @mentions and handle-shaped words there.
        handles = [w.lstrip("@") for w in words if HANDLE.match(w)
                   and (is_list or w.startswith("@") or re.search(r"[._0-9]", w))]
        for h in urls + handles:
            if h not in target:
                target.append(h)
    return adds, removes


def _apply(cfg: Config, adds: list[str], removes: list[str]) -> Changes:
    current = set(read_accounts())
    changes = Changes([], [], [], [], [])

    to_remove = {h for h in removes if h in current}
    changes.not_found_for_removal = [h for h in removes if h not in current]
    if to_remove:
        remove_accounts(to_remove)
        changes.removed = sorted(to_remove)

    graph = Graph(cfg.access_token, cfg.api_version)
    for i, handle in enumerate(h for h in adds if h not in to_remove):
        if handle in current:
            changes.already.append(handle)
            continue
        if i:
            time.sleep(1)
        try:
            graph.business_discovery(cfg.ig_user_id, handle, limit=1)
        except NotSupported:
            changes.unsupported.append(handle)
            continue
        except AuthError:
            raise
        except (GraphError, requests.RequestException) as e:
            log.warning("Couldn't check @%s: %s", handle, e)
            changes.unsupported.append(handle)
            continue
        changes.added.append(handle)
        current.add(handle)
    if changes.added:
        add_accounts(changes.added)
    return changes


def _confirmation(c: Changes, first_digest: bool = False) -> str:
    def fmt(hs: list[str]) -> str:
        return ", ".join(f"@{h}" for h in hs)

    lines = []
    if c.added:
        lines.append(f"Added: {fmt(c.added)}")
    if c.removed:
        lines.append(f"Removed: {fmt(c.removed)}")
    if c.already:
        lines.append(f"Already on your list: {fmt(c.already)}")
    if c.unsupported:
        lines.append(f"Not added (personal account, or the username doesn't exist): {fmt(c.unsupported)}")
    if c.not_found_for_removal:
        lines.append(f"Not on your list, so nothing to remove: {fmt(c.not_found_for_removal)}")
    if not lines:
        lines.append("I couldn't find any Instagram handles in your reply. Put one per line "
                     "(or \"add @handle\"), or write \"remove @handle\".")
    n = len(read_accounts())
    lines.append(f"\nYou're following {n} account{'' if n == 1 else 's'} in Epilog.")
    if first_digest:
        lines.append("Your first digest, with their posts from the past week, is on its way.")
    return "\n".join(lines) + "\n"


def _reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _fetch_replies(cfg: Config, processed: set[str]) -> list[Reply]:
    since = (datetime.now() - LOOKBACK).strftime("%d-%b-%Y")
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(cfg.gmail_address, cfg.gmail_app_password)
        imap.select("INBOX", readonly=True)
        _, data = imap.uid("SEARCH", None, "SINCE", since, "FROM", f'"{cfg.gmail_address}"')
        uids = data[0].split()
        if not uids:
            return []

        # Headers first: digests carry megabytes of images we don't want to download.
        # A reply is recognised by its In-Reply-To/References pointing at an Epilog
        # email, not by subject, since digest subjects don't say "Epilog".
        wanted = []
        _, headers = imap.uid(
            "FETCH", b",".join(uids),
            "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM IN-REPLY-TO REFERENCES)])")
        ours = f"@{EPILOG_MSGID_DOMAIN}>"
        for part in headers:
            if not isinstance(part, tuple):
                continue
            uid = re.search(rb"UID (\d+)", part[0]).group(1)
            h = email.message_from_bytes(part[1], policy=default_policy)
            msg_id, subject = (h["Message-ID"] or "").strip(), str(h["Subject"] or "")
            sender = parseaddr(str(h["From"] or ""))[1].lower()
            replying_to_epilog = ours in f"{h['In-Reply-To'] or ''} {h['References'] or ''}"
            if (msg_id and msg_id not in processed and not msg_id.endswith(ours)
                    and replying_to_epilog and sender == cfg.gmail_address.lower()):
                wanted.append((uid, msg_id, subject))

        replies = []
        for uid, msg_id, subject in wanted:
            _, body = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            msg = email.message_from_bytes(body[0][1], policy=default_policy)
            replies.append(Reply(msg_id, subject, _plain_text(msg)))
        return replies
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def _plain_text(msg: Message) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<blockquote.*", "", text)  # Gmail wraps the quoted digest in a blockquote
        text = re.sub(r"(?i)<br\s*/?>|</(div|p)>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
    return text
