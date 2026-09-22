"""Fetch new posts for a set of accounts and turn them into a sent digest."""

import logging
import random
import time
from datetime import datetime, timedelta, timezone

import requests

from .config import Config
from .graph import AuthError, Graph, GraphError, NotSupported
from .render import Digest, render
from .send import send_email
from .state import State

log = logging.getLogger(__name__)

FIRST_RUN_WINDOW = timedelta(hours=24)
TOKEN_WARNING = timedelta(days=7)


def collect(cfg: Config, accounts: list[str], state: State,
            since: timedelta | None = None) -> tuple[Digest, dict[str, datetime]]:
    """Returns the digest plus the newest post time per account (to save once sent).
    Raises graph.AuthError if the token is rejected."""
    now = datetime.now(timezone.utc)
    graph = Graph(cfg.access_token, cfg.api_version)
    digest = Digest(accounts=[])
    newest: dict[str, datetime] = {}

    for i, username in enumerate(accounts):
        if i:
            time.sleep(1)
        try:
            account = graph.business_discovery(cfg.ig_user_id, username)
        except NotSupported:
            log.info("@%s: not a Business/Creator account", username)
            digest.unsupported.append(username)
            continue
        except AuthError:
            raise
        except (GraphError, requests.RequestException) as e:
            log.warning("@%s: %s", username, e)
            digest.failed.append((username, str(e)))
            continue

        if account.posts:
            newest[username] = max(p.timestamp for p in account.posts)
        cutoff = now - since if since else (state.get(username) or now - FIRST_RUN_WINDOW)
        account.posts = sorted((p for p in account.posts if p.timestamp > cutoff),
                               key=lambda p: p.timestamp, reverse=True)
        log.info("@%s: %d new", username, len(account.posts))
        if account.posts:
            digest.accounts.append(account)

    digest.accounts.sort(key=lambda a: a.posts[0].timestamp, reverse=True)
    if cfg.token_expires_at and cfg.token_expires_at - now < TOKEN_WARNING:
        digest.notices.append(
            f"Your Instagram connection expires {cfg.token_expires_at.astimezone():%a %b %-d}. "
            "To renew it, run Epilog setup again (double-click “Setup Epilog” in the Epilog folder, "
            "or run `uv run epilog setup`). It takes about 2 minutes."
        )
    return digest, newest


def keep_random_posts(digest: Digest, n: int) -> None:
    posts = [p for a in digest.accounts for p in a.posts]
    picked = {id(p) for p in random.sample(posts, min(n, len(posts)))}
    for a in digest.accounts:
        a.posts = [p for p in a.posts if id(p) in picked]
    digest.accounts = [a for a in digest.accounts if a.posts]


def deliver(cfg: Config, digest: Digest) -> None:
    html, text, images = render(digest, inline_images=False)
    send_email(cfg, digest.subject, text, html, images)
    log.info("Sent “%s” to %s", digest.subject, cfg.digest_to)


def save_seen(state: State, newest: dict[str, datetime]) -> None:
    for username, ts in newest.items():
        state.mark(username, ts)
    state.save()


def send_auth_alert(cfg: Config, err: Exception) -> None:
    try:
        send_email(
            cfg, "⁕ Epilog needs you to reconnect Instagram",
            "Epilog couldn't read Instagram today because Instagram rejected its connection:\n\n"
            f"  {err}\n\n"
            "To fix it, run Epilog setup again: double-click “Setup Epilog” in the Epilog folder, "
            "or run `uv run epilog setup`. It takes about 2 minutes.\n",
        )
    except Exception as e:  # noqa: BLE001
        log.error("Also couldn't send the alert email: %s", e)
