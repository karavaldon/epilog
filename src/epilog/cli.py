import argparse
import logging
import re
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone

import requests

from . import schedule, wizard
from .config import DATA_DIR, LOG_DIR, PREVIEW_PATH, Config, load_config, read_accounts
from .digest import collect, deliver, keep_random_posts, save_seen, send_auth_alert
from .graph import Account, AuthError, Graph, GraphError, Media, NotSupported, Post
from .inbox import process_replies
from .render import Digest, render
from .send import send_email
from .state import State

log = logging.getLogger("epilog")


# ---------------------------------------------------------------- run

def cmd_run(args) -> int:
    cfg = _require_config()
    state = State()
    if not args.dry_run:
        _process_replies(cfg, state)  # pick up accounts added by email before building the digest
    accounts = read_accounts()
    if not accounts:
        log.info("No accounts yet — reply to the welcome email (or any digest) with handles to add some.")
        return 0

    try:
        digest, newest = collect(cfg, accounts, state, since=args.since)
    except AuthError as e:
        log.error("Instagram rejected Epilog's connection: %s", e)
        if not args.dry_run:
            send_auth_alert(cfg, e)
        return 1

    if args.sample:
        keep_random_posts(digest, args.sample)
        newest = {}  # a sample is a one-off: don't advance what counts as "seen"

    if args.dry_run:
        html, _, _ = render(digest, inline_images=True)
        PREVIEW_PATH.write_text(html)
        log.info("Wrote %s (%d posts). Nothing sent, state unchanged.", PREVIEW_PATH, digest.post_count)
        webbrowser.open(PREVIEW_PATH.as_uri())
        return 0

    if digest.post_count or digest.notices:
        deliver(cfg, digest)
    else:
        log.info("Nothing new; no email sent")
    save_seen(state, newest)
    return 0


# ---------------------------------------------------------------- inbox

def cmd_inbox(args) -> int:
    cfg = _require_config()
    return 0 if _process_replies(cfg, State()) is not None else 1


def _process_replies(cfg: Config, state: State) -> int | None:
    try:
        count = process_replies(cfg, state)
    except AuthError as e:
        log.error("Instagram rejected Epilog's connection while checking replies: %s", e)
        return None
    except Exception as e:  # noqa: BLE001 — never let reply handling block the digest
        log.error("Couldn't check email replies: %s", e)
        return None
    if count:
        log.info("Processed %d email repl%s", count, "y" if count == 1 else "ies")
    return count


# ---------------------------------------------------------------- check / status

def cmd_check(args) -> int:
    cfg = _require_config()
    accounts = read_accounts()
    if not accounts:
        print("You're not following any accounts yet.")
        return 0
    graph = Graph(cfg.access_token, cfg.api_version)
    ok = bad = 0
    for i, username in enumerate(accounts):
        if i:
            time.sleep(1)
        try:
            account = graph.business_discovery(cfg.ig_user_id, username, limit=1)
        except NotSupported:
            print(f"  ✗ @{username} — not available (personal account, or username doesn't exist)")
            bad += 1
            continue
        except AuthError as e:
            print(f"\nInstagram rejected Epilog's connection: {e}\nRun `uv run epilog setup` to reconnect.")
            return 1
        except (GraphError, requests.RequestException) as e:
            print(f"  ! @{username} — error: {e}")
            bad += 1
            continue
        latest = f"latest post {account.posts[0].timestamp.astimezone():%b %-d}" if account.posts else "no posts"
        print(f"  ✓ @{username} — {account.name} ({latest})")
        ok += 1
    print(f"\n{ok} supported, {bad} not supported.")
    return 0


def cmd_status(args) -> int:
    cfg = load_config()
    now = datetime.now(timezone.utc)
    print(f"Settings folder: {DATA_DIR}")
    if cfg.instagram_ready:
        expires = cfg.token_expires_at
        note = f"renew by {expires.astimezone():%b %-d, %Y}" if expires else "no expiry"
        if expires and expires <= now:
            note = "EXPIRED — run setup to reconnect"
        print(f"Instagram:       @{cfg.ig_username} ({note})")
    else:
        print("Instagram:       not connected")
    print(f"Gmail:           {cfg.gmail_address if cfg.gmail_ready else 'not connected'}")
    jobs = schedule.status()
    when = schedule.format_time(*schedule.parse_time(cfg.digest_time))
    print(f"Schedule:        {'daily at ' + when if jobs.get('digest') else 'not scheduled'}"
          f"{', reply check every 15 min' if jobs.get('inbox') else ''}")
    print(f"Accounts:        {len(read_accounts())}")
    log_file = LOG_DIR / "digest.log"
    if log_file.exists():
        sent = [l for l in log_file.read_text().splitlines() if " Sent " in l]
        if sent:
            print(f"Last digest:     {sent[-1][:19]}")
    return 0


# ---------------------------------------------------------------- setup / schedule

def cmd_setup(args) -> int:
    if args.terminal:
        return wizard.run()
    from . import webui
    return webui.run(demo=args.demo)


def cmd_setup_token(args) -> int:
    """Kept for anyone used to it: just the Instagram step of setup."""
    try:
        return 0 if wizard.step_instagram() else 1
    except (KeyboardInterrupt, EOFError):
        return 1


def cmd_schedule(args) -> int:
    cfg = load_config()
    if args.action == "install":
        schedule.install(cfg.digest_time)
        print(f"Scheduled daily at {schedule.format_time(*schedule.parse_time(cfg.digest_time))}, "
              "plus a reply check every 15 minutes.")
    elif args.action == "uninstall":
        schedule.uninstall()
        print("Removed Epilog's scheduled jobs.")
    else:
        for name, installed in schedule.status().items():
            print(f"{name}: {'installed' if installed else 'not installed'}")
    return 0


# ---------------------------------------------------------------- test-email / demo

def cmd_test_email(args) -> int:
    cfg = load_config()
    send_email(cfg, "⁕ Epilog test email", "If you can read this, Epilog can send email from your Gmail.")
    print(f"Sent a test email to {cfg.digest_to}.")
    return 0


def cmd_demo(args) -> int:
    """Render a sample digest with placeholder photos — no Instagram needed."""
    now = datetime.now(timezone.utc)

    def post(n: int, hours: float, kind: str = "image", caption: str = "", count: int = 1) -> Post:
        size = "1080/1920" if kind == "video" else "1080/1350"
        items = [Media(f"https://picsum.photos/seed/epilog{n}-{i}/{size}", is_video=kind == "video")
                 for i in range(count)]
        return Post(id=f"demo{n}", permalink="https://www.instagram.com/", caption=caption,
                    timestamp=now - timedelta(hours=hours), kind=kind, items=items)

    digest = Digest(
        accounts=[
            Account("tinyroomceramics", "Tiny Room Ceramics", "https://picsum.photos/seed/avatar1/200", [
                post(1, 3, caption="New glaze tests out of the kiln this morning. The speckled oat is my favorite "
                                   "so far — shop update Friday at noon."),
                post(2, 20, "carousel", "Studio clean-up day.\nSwipe for the before/after.", count=3),
            ]),
            Account("slowbread.co", "Slow Bread Co.", "https://picsum.photos/seed/avatar2/200", [
                post(3, 9, "video", "72-hour sourdough, start to finish. Recipe in bio."),
            ]),
        ],
        unsupported=["a_friend", "another.friend"],
    )
    html, _, _ = render(digest, inline_images=True)
    PREVIEW_PATH.write_text(html)
    print(f"Wrote {PREVIEW_PATH}")
    webbrowser.open(PREVIEW_PATH.as_uri())
    return 0


# ---------------------------------------------------------------- helpers

def _require_config() -> Config:
    cfg = load_config()
    if not (cfg.instagram_ready and cfg.gmail_ready):
        sys.exit("Epilog isn't set up yet. Run `uv run epilog setup` (or double-click “Setup Epilog”).")
    return cfg


def _duration(value: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([hd])", value.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("use e.g. 48h or 3d")
    n = int(m.group(1))
    return timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    parser = argparse.ArgumentParser(prog="epilog", description="A daily email digest of new Instagram posts.")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="guided setup in your browser (also renews Instagram or changes the time)")
    setup.add_argument("--terminal", action="store_true", help="use the text-only setup instead of the browser")
    setup.add_argument("--demo", action="store_true",
                       help="walk through setup with invented answers; nothing is saved, sent or scheduled")
    setup.set_defaults(func=cmd_setup)
    sub.add_parser("status", help="show what's connected and scheduled").set_defaults(func=cmd_status)

    run = sub.add_parser("run", help="fetch new posts and email the digest")
    run.add_argument("--dry-run", action="store_true", help="write preview.html instead of sending")
    run.add_argument("--since", type=_duration, help="ignore saved state; include posts from e.g. 48h or 3d")
    run.add_argument("--sample", type=int, metavar="N",
                     help="send N random posts (use with --since); doesn't change what counts as seen")
    run.set_defaults(func=cmd_run)

    sub.add_parser("inbox", help="process email replies that add/remove accounts").set_defaults(func=cmd_inbox)
    sub.add_parser("check", help="show which followed accounts Instagram's API can read").set_defaults(
        func=cmd_check)

    sched = sub.add_parser("schedule", help="install, remove or show the background jobs")
    sched.add_argument("action", choices=["install", "uninstall", "status"])
    sched.set_defaults(func=cmd_schedule)

    sub.add_parser("setup-token", help="reconnect Instagram only").set_defaults(func=cmd_setup_token)
    sub.add_parser("test-email", help="send a test email via Gmail").set_defaults(func=cmd_test_email)
    sub.add_parser("demo", help="preview the email design with sample data").set_defaults(func=cmd_demo)

    args = parser.parse_args()
    sys.exit(args.func(args))
