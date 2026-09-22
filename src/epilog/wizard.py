"""`epilog setup`: a guided, re-runnable setup. Each step checks what's already
working and skips it (or offers to redo it), so running setup again is also how
you renew the Instagram connection or change the delivery time."""

import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from getpass import getpass

from . import schedule
from .config import DATA_DIR, load_config, read_accounts, update_env
from .instagram import ConnectError, check_connection, connect
from .send import check_gmail
from .state import State
from .welcome import send_welcome

RENEW_WITHIN = timedelta(days=14)
STEPS = 4

META_APPS = "https://developers.facebook.com/apps"
GRAPH_EXPLORER = "https://developers.facebook.com/tools/explorer/"
CREATE_PAGE = "https://www.facebook.com/pages/create"
APP_PASSWORDS = "https://myaccount.google.com/apppasswords"
TWO_STEP = "https://myaccount.google.com/signinoptions/twosv"
PERMISSIONS = ["instagram_basic", "instagram_manage_insights", "pages_read_engagement",
               "pages_show_list", "business_management"]


# ---------------------------------------------------------------- terminal helpers

BOLD, DIM, GREEN, RED, RESET = ("\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m") \
    if sys.stdout.isatty() else ("",) * 5


def say(text: str = "") -> None:
    print(text)


def ok(text: str) -> None:
    print(f"{GREEN}✓{RESET} {text}")


def problem(text: str) -> None:
    print(f"{RED}✗{RESET} {text}")


def heading(n: int, title: str) -> None:
    print(f"\n{BOLD}Step {n} of {STEPS} · {title}{RESET}\n")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" {DIM}[{default}]{RESET}" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def ask_secret(prompt: str) -> str:
    return getpass(f"{prompt} {DIM}(hidden as you paste){RESET}: ").strip()


def confirm(prompt: str, default: bool = True) -> bool:
    answer = input(f"{prompt} {DIM}[{'Y/n' if default else 'y/N'}]{RESET} ").strip().lower()
    return default if not answer else answer.startswith("y")


def open_page(url: str, label: str) -> None:
    if confirm(f"Open {label} in your browser?"):
        webbrowser.open(url)


def fmt_date(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%b %-d, %Y") if dt else "never"


# ---------------------------------------------------------------- steps

def step_instagram() -> bool:
    heading(1, "Connect Instagram")
    cfg = load_config()

    if cfg.instagram_ready:
        issue = check_connection(cfg)
        expiring = cfg.token_expires_at and cfg.token_expires_at - datetime.now(timezone.utc) < RENEW_WITHIN
        if issue is None and not expiring:
            ok(f"Connected as @{cfg.ig_username} (renew by {fmt_date(cfg.token_expires_at)}).")
            if not confirm("Reconnect anyway?", default=False):
                return True
        elif issue is None:
            say(f"Connected as @{cfg.ig_username}, but the connection expires {fmt_date(cfg.token_expires_at)}. "
                "Let's renew it — you'll only need a fresh token (steps 5–6 below).")
        else:
            problem(f"The saved Instagram connection doesn't work ({issue}). Let's reconnect.")

    if not cfg.app_id:
        say("Epilog reads Instagram through Meta's official API, which needs a few free things set up once.\n"
            "This is the longest part of setup (about 15 minutes). Keep this window open while you work.\n")
        say(f"{BOLD}1. Make your Instagram a professional account{RESET}")
        say("   In the Instagram app: Settings → Account type and tools → Switch to professional account.\n"
            "   Creator or Business both work. It's free and you can switch back later.\n")
        say(f"{BOLD}2. Link it to a Facebook Page{RESET}")
        say("   Create a Page (a placeholder is fine — nobody needs to see it), then in the Page's settings:\n"
            "   Linked accounts → Instagram → Connect account.")
        open_page(CREATE_PAGE, "Facebook's “Create a Page”")
        say(f"\n{BOLD}3. Create a Meta app{RESET}")
        say("   At developers.facebook.com: Create app → use case “Manage messaging & content on Instagram”.\n"
            "   Then Customize → “API setup with Facebook login”, click “Add required content permissions”,\n"
            "   and under Permissions and features also add instagram_manage_insights.\n"
            "   Leave the app in Development mode — that's all a personal tool needs.")
        open_page(META_APPS, "Meta for Developers")
        say(f"\n{BOLD}4. Copy the app's ID and secret{RESET}")
        say("   In your app: App settings → Basic.\n")

    app_id = ask("App ID", cfg.app_id)
    app_secret = cfg.app_secret if cfg.app_secret and confirm("Use the saved App Secret?") \
        else ask_secret("App Secret")

    say(f"\n{BOLD}5. Generate an access token{RESET}")
    say("   In Graph API Explorer: pick your app (top right) → “Get User Access Token”, and tick:\n"
        f"   {', '.join(PERMISSIONS)}\n"
        "   Click “Generate Access Token” and, when asked, choose the Page linked to your Instagram.")
    open_page(GRAPH_EXPLORER, "Graph API Explorer")
    say(f"\n{BOLD}6. Paste the token here{RESET}")

    while True:
        token = ask_secret("Access token")
        if not token:
            problem("No token entered.")
        else:
            try:
                conn = connect(load_config(), app_id, app_secret, token, choose=_choose_page)
            except ConnectError as e:
                problem(str(e))
            else:
                ok(f"Connected as @{conn.username}. Renew by {fmt_date(conn.expires_at)} "
                   "(Epilog will remind you a week before).")
                if conn.missing_scopes:
                    say(f"  Note: the token is missing {', '.join(sorted(conn.missing_scopes))}. "
                        "It works for now, but regenerate it with all permissions if anything fails.")
                return True
        if not confirm("Try again with a new token?"):
            return False


def _choose_page(pages: list[dict]) -> dict:
    say("\nThis token can see several Pages with Instagram accounts:")
    for n, p in enumerate(pages, 1):
        say(f"  {n}. {p['name']} → @{p['instagram_business_account'].get('username')}")
    while True:
        choice = ask("Which one is yours", "1")
        if choice.isdigit() and 1 <= int(choice) <= len(pages):
            return pages[int(choice) - 1]


def step_gmail() -> bool:
    heading(2, "Connect Gmail")
    cfg = load_config()
    if cfg.gmail_ready and check_gmail(cfg.gmail_address, cfg.gmail_app_password) is None:
        ok(f"Sending from {cfg.gmail_address}.")
        if not confirm("Change it?", default=False):
            return True

    say("Epilog sends the digest from your own Gmail, to yourself, and reads your replies to it.\n"
        "It signs in with an app password — a separate password just for Epilog that you can revoke anytime.\n")
    say(f"{BOLD}1.{RESET} App passwords need 2-Step Verification turned on for your Google account.")
    open_page(TWO_STEP, "Google 2-Step Verification settings")
    say(f"{BOLD}2.{RESET} Create an app password named “Epilog” and copy the 16-letter code.")
    open_page(APP_PASSWORDS, "Google app passwords")
    say("")

    while True:
        address = ask("Your Gmail address", cfg.gmail_address).lower()
        password = ask_secret("App password").replace(" ", "")
        say("Checking…")
        issue = check_gmail(address, password)
        if issue is None:
            update_env({"GMAIL_ADDRESS": address, "GMAIL_APP_PASSWORD": password})
            ok(f"Gmail works. Digests will come from and go to {address}.")
            return True
        problem(issue)
        say("  Common causes: the app password was made while signed in to a different Google account,\n"
            "  or it was mistyped. Creating a fresh one usually fixes it.")
        if not confirm("Try again?"):
            return False


def step_schedule() -> bool:
    heading(3, "Choose a delivery time")
    cfg = load_config()
    if not schedule.supported():
        say("Automatic scheduling works on macOS and Linux. On other systems, schedule\n"
            f"`{sys.executable} -m epilog run` daily and `… -m epilog inbox` every 15 minutes yourself.")
        return True

    installed = all(schedule.status().values())
    current = schedule.format_time(*schedule.parse_time(cfg.digest_time))
    if installed:
        ok(f"Your digest is scheduled daily at {current}.")
        if not confirm("Change the time?", default=False):
            return True

    while True:
        answer = ask("What time should your daily digest arrive", current)
        try:
            hour, minute = schedule.parse_time(answer)
            break
        except ValueError as e:
            problem(str(e))
    update_env({"DIGEST_TIME": f"{hour:02d}:{minute:02d}"})
    try:
        schedule.install(f"{hour:02d}:{minute:02d}")
    except (schedule.ScheduleError, OSError) as e:
        problem(f"Couldn't set up the schedule: {e}")
        return False
    ok(f"Scheduled: your digest daily at {schedule.format_time(hour, minute)}, "
       "and a check for your email replies every 15 minutes.")
    if sys.platform == "darwin":
        say(f"  {DIM}Your Mac needs to be awake; if it's asleep at that time, the digest arrives when it wakes.{RESET}")
    return True


def step_accounts() -> bool:
    heading(4, "Pick accounts to follow")
    cfg, state = load_config(), State()
    accounts = read_accounts()
    if accounts:
        ok(f"You're following {len(accounts)} account{'' if len(accounts) == 1 else 's'}. "
           "Reply to any digest to add more, or “remove @handle”.")
        return True
    if state.welcome_pending and not confirm("A welcome email was already sent. Send it again?", default=False):
        return True
    try:
        send_welcome(cfg, state)
    except Exception as e:  # noqa: BLE001
        problem(f"Couldn't send the welcome email: {e}")
        return False
    ok(f"Sent a welcome email to {cfg.digest_to}.")
    say(f"\n  {BOLD}Reply to it with the Instagram accounts you want to follow{RESET}, one per line.\n"
        "  Within about 15 minutes you'll get a confirmation and a first digest of the past week.")
    return True


def run() -> int:
    say(f"\n{BOLD}⁕ Epilog setup{RESET}")
    say("Epilog emails you a daily digest of new posts from Instagram accounts you choose.")
    say(f"{DIM}Your settings are saved in {DATA_DIR}. You can stop anytime (Ctrl-C) and run setup again —\n"
        f"finished steps are remembered.{RESET}")
    try:
        for step in (step_instagram, step_gmail, step_schedule, step_accounts):
            if not step():
                say("\nSetup paused. Run it again to pick up where you left off.")
                return 1
    except (KeyboardInterrupt, EOFError):
        say("\n\nSetup paused. Run it again to pick up where you left off.")
        return 1
    say(f"\n{BOLD}All set.{RESET} Run setup again anytime to renew Instagram, change Gmail, or change the time.")
    return 0
