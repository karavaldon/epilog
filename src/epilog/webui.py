"""Browser-based setup: a small local web server (127.0.0.1 only) that serves the
setup page and a JSON API over the same steps as the terminal wizard.

Every API call must carry a random per-session key, and the Host header must be
our own address, so other websites open in the browser can't talk to it."""

import json
import logging
import secrets
import socket
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from . import schedule
from .config import DATA_DIR, add_accounts, load_config, read_accounts, remove_accounts, update_env
from .graph import AuthError, Graph, GraphError, NotSupported
from .instagram import ChoosePage, ConnectError, check_connection, connect, pick_page
from .send import check_gmail
from .state import State
from .welcome import send_welcome

log = logging.getLogger(__name__)

PAGE = Path(__file__).parent / "templates" / "setup.html"
IDLE_TIMEOUT = timedelta(minutes=45)
FIRST_DIGEST_WINDOW = timedelta(days=7)
MAX_HANDLES = 60


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int):
        super().__init__(("127.0.0.1", port), Handler)
        self.key = secrets.token_urlsafe(24)
        self.host_ok = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.last_seen = time.monotonic()
        self.finished = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/?k={self.key}"


class Handler(BaseHTTPRequestHandler):
    server: SetupServer

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # ---------------------------------------------------------------- plumbing

    def _allowed(self) -> bool:
        return self.headers.get("Host", "") in self.server.host_ok

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        if not self._allowed():
            return self._send(403, b"Forbidden", "text/plain")
        self.server.last_seen = time.monotonic()
        if self.path.split("?")[0] == "/":
            html = PAGE.read_text().replace("__EPILOG_KEY__", self.server.key)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if self.path == "/api/state" and self.headers.get("X-Epilog-Key") == self.server.key:
            return self._json(demo_state() if DEMO.get("on") else state())
        self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        if not self._allowed() or self.headers.get("X-Epilog-Key") != self.server.key:
            return self._send(403, b"Forbidden", "text/plain")
        self.server.last_seen = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._json({"error": "Bad request."}, 400)

        routes = DEMO_ROUTES if DEMO.get("on") else ROUTES
        route = routes.get(self.path)
        if not route:
            return self._json({"error": "Not found."}, 404)
        try:
            result = route(body)
        except Exception as e:  # noqa: BLE001 — always answer the page with something readable
            log.exception("setup API error on %s", self.path)
            return self._json({"error": f"Something went wrong: {e}"}, 500)
        self._json(result)
        if self.path == "/api/finish":
            self.server.finished.set()


# ---------------------------------------------------------------- API

def state() -> dict:
    cfg = load_config()
    ig_issue = check_connection(cfg) if cfg.instagram_ready else "not set up"
    expires = cfg.token_expires_at
    jobs = schedule.status()
    return {
        "dataDir": str(DATA_DIR),
        "instagram": {
            "connected": ig_issue is None,
            "issue": None if ig_issue in (None, "not set up") else ig_issue,
            "username": cfg.ig_username,
            "renewBy": expires.astimezone().strftime("%b %-d, %Y") if expires else None,
            "expiringSoon": bool(expires and expires - datetime.now(timezone.utc) < timedelta(days=14)),
            "appId": cfg.app_id,
            "hasSecret": bool(cfg.app_secret),
        },
        "gmail": {
            "connected": cfg.gmail_ready,
            "address": cfg.gmail_address,
        },
        "schedule": {
            "supported": schedule.supported(),
            "installed": all(jobs.values()),
            "time": cfg.digest_time,
        },
        "accounts": read_accounts(),
        "welcomePending": State().welcome_pending,
    }


def api_instagram(body: dict) -> dict:
    cfg = load_config()
    app_id = (body.get("appId") or "").strip()
    app_secret = (body.get("appSecret") or "").strip() or cfg.app_secret
    token = (body.get("token") or "").strip()
    if not (app_id and app_secret and token):
        return {"error": "Fill in the App ID, App Secret and access token."}
    try:
        conn = connect(cfg, app_id, app_secret, token, choose=pick_page(body.get("pageId")))
    except ChoosePage as e:
        return {"choose": e.options}
    except ConnectError as e:
        return {"error": str(e)}
    return {
        "ok": True,
        "username": conn.username,
        "renewBy": conn.expires_at.astimezone().strftime("%b %-d, %Y") if conn.expires_at else None,
        "missingScopes": sorted(conn.missing_scopes),
    }


def api_gmail(body: dict) -> dict:
    address = (body.get("address") or "").strip().lower()
    password = (body.get("password") or "").replace(" ", "")
    if not address or "@" not in address:
        return {"error": "Enter your Gmail address."}
    if not password:
        cfg = load_config()
        if cfg.gmail_ready and cfg.gmail_address == address:
            password = cfg.gmail_app_password
        else:
            return {"error": "Paste the app password."}
    issue = check_gmail(address, password)
    if issue:
        return {"error": issue}
    update_env({"GMAIL_ADDRESS": address, "GMAIL_APP_PASSWORD": password})
    return {"ok": True, "address": address}


def api_schedule(body: dict) -> dict:
    try:
        hour, minute = schedule.parse_time(body.get("time") or "07:00")
    except ValueError as e:
        return {"error": str(e)}
    value = f"{hour:02d}:{minute:02d}"
    update_env({"DIGEST_TIME": value})
    if not schedule.supported():
        return {"ok": True, "time": value, "note": "Automatic scheduling only works on macOS and Linux."}
    try:
        schedule.install(value)
    except (schedule.ScheduleError, OSError) as e:
        return {"error": f"Couldn't set up the schedule: {e}"}
    return {"ok": True, "time": value, "label": schedule.format_time(hour, minute)}


def api_check_accounts(body: dict) -> dict:
    from .inbox import parse_handles  # same parsing as email replies: @, links, commas…

    adds, _ = parse_handles(str(body.get("text") or ""))
    adds = adds[:MAX_HANDLES]
    cfg = load_config()
    graph = Graph(cfg.access_token, cfg.api_version)
    following = set(read_accounts())
    results = []
    for i, handle in enumerate(adds):
        if i:
            time.sleep(0.4)
        item = {"handle": handle, "following": handle in following}
        try:
            acct = graph.business_discovery(cfg.ig_user_id, handle, limit=1)
        except NotSupported:
            item["status"] = "personal"
        except AuthError:
            return {"error": "Instagram rejected Epilog's connection — reconnect Instagram in step 1."}
        except (GraphError, requests.RequestException) as e:
            item.update(status="error", detail=str(e))
        else:
            item.update(status="ok", name=acct.name, avatar=acct.avatar_url,
                        latest=acct.posts[0].timestamp.astimezone().strftime("%b %-d") if acct.posts else None)
        results.append(item)
    return {"results": results}


def api_save_accounts(body: dict) -> dict:
    handles = [h for h in body.get("add", []) if isinstance(h, str)]
    current = set(read_accounts())
    new = [h for h in dict.fromkeys(handles) if h not in current]
    if new:
        add_accounts(new)
    remove = {h for h in body.get("remove", []) if isinstance(h, str)}
    if remove:
        remove_accounts(remove)
    return {"ok": True, "accounts": read_accounts()}


def api_welcome(body: dict) -> dict:
    cfg = load_config()
    send_welcome(cfg, State())
    return {"ok": True, "to": cfg.digest_to}


def api_first_digest(body: dict) -> dict:
    from .digest import collect, deliver, save_seen

    cfg, st = load_config(), State()
    accounts = read_accounts()
    if not accounts:
        return {"error": "Add some accounts first."}
    try:
        digest, newest = collect(cfg, accounts, st, since=FIRST_DIGEST_WINDOW)
    except AuthError:
        return {"error": "Instagram rejected Epilog's connection — reconnect Instagram in step 1."}
    if digest.post_count:
        deliver(cfg, digest)
    st.welcome_pending = False
    save_seen(st, newest)
    return {"ok": True, "posts": digest.post_count, "accounts": len(digest.accounts), "to": cfg.digest_to}


def api_finish(body: dict) -> dict:
    return {"ok": True}


# ---------------------------------------------------------------- demo mode
# `epilog setup --demo` walks the same pages with invented answers: nothing is
# saved, sent or scheduled. Useful for a look around, screenshots or a video.

DEMO: dict = {"on": False}
DEMO_PERSONAL = {"artist.run.club", "a_friend", "another.friend", "phillip_niemeyer"}
DEMO_NAMES = {"flitchcoffee": "Flitch Coffee", "marthastewart": "Martha Stewart",
              "sillygooseceramics": "silly goose ceramics", "nytcooking": "NYT Cooking",
              "northernsouthern": "Northern-Southern", "ftlonesome": "FT. LONESOME"}


def _demo_reset() -> None:
    DEMO.update(on=True, username="", gmail="", time="07:00", scheduled=False,
                accounts=[], welcome=False)


def demo_state() -> dict:
    renew = (datetime.now() + timedelta(days=60)).strftime("%b %-d, %Y")
    return {
        "demo": True,
        "dataDir": "(demo — nothing is saved)",
        "instagram": {"connected": bool(DEMO["username"]), "issue": None, "username": DEMO["username"],
                      "renewBy": renew if DEMO["username"] else None, "expiringSoon": False,
                      "appId": "", "hasSecret": False},
        "gmail": {"connected": bool(DEMO["gmail"]), "address": DEMO["gmail"]},
        "schedule": {"supported": True, "installed": DEMO["scheduled"], "time": DEMO["time"]},
        "accounts": DEMO["accounts"],
        "welcomePending": DEMO["welcome"],
    }


def demo_instagram(body: dict) -> dict:
    time.sleep(1.2)
    if not (body.get("appId") or "").strip():
        return {"error": "Fill in the App ID, App Secret and access token."}
    token = (body.get("token") or "").strip()
    if len(token) < 6:
        return {"error": "Meta rejected the details: that doesn't look like an access token.\n"
                         "(Demo tip: type any six characters to continue.)"}
    DEMO["username"] = "demo.account"
    return {"ok": True, "username": DEMO["username"],
            "renewBy": (datetime.now() + timedelta(days=60)).strftime("%b %-d, %Y"), "missingScopes": []}


def demo_gmail(body: dict) -> dict:
    time.sleep(1.0)
    address = (body.get("address") or "").strip().lower()
    if "@" not in address:
        return {"error": "Enter your Gmail address."}
    if not (body.get("password") or "").strip():
        return {"error": "Gmail didn't accept that address and app password.\n"
                         "(Demo tip: type anything as the app password to continue.)"}
    DEMO["gmail"] = address
    return {"ok": True, "address": address}


def demo_schedule(body: dict) -> dict:
    time.sleep(0.6)
    try:
        hour, minute = schedule.parse_time(body.get("time") or "07:00")
    except ValueError as e:
        return {"error": str(e)}
    DEMO.update(time=f"{hour:02d}:{minute:02d}", scheduled=True)
    return {"ok": True, "time": DEMO["time"], "label": schedule.format_time(hour, minute)}


def demo_check_accounts(body: dict) -> dict:
    from .inbox import parse_handles

    adds, _ = parse_handles(str(body.get("text") or ""))
    results = []
    for i, handle in enumerate(adds[:MAX_HANDLES]):
        time.sleep(0.35)
        item = {"handle": handle, "following": handle in DEMO["accounts"]}
        if handle in DEMO_PERSONAL:
            item["status"] = "personal"
        else:
            item.update(status="ok", name=DEMO_NAMES.get(handle, handle.replace(".", " ").title()),
                        avatar=f"https://picsum.photos/seed/{handle}/200",
                        latest=(datetime.now() - timedelta(days=i % 5)).strftime("%b %-d"))
        results.append(item)
    return {"results": results}


def demo_save_accounts(body: dict) -> dict:
    for h in body.get("add", []):
        if h not in DEMO["accounts"]:
            DEMO["accounts"].append(h)
    DEMO["accounts"] = [h for h in DEMO["accounts"] if h not in set(body.get("remove", []))]
    return {"ok": True, "accounts": DEMO["accounts"]}


def demo_welcome(body: dict) -> dict:
    time.sleep(0.8)
    DEMO["welcome"] = True
    return {"ok": True, "to": DEMO["gmail"] or "you@gmail.com"}


def demo_first_digest(body: dict) -> dict:
    time.sleep(2.0)
    n = len(DEMO["accounts"])
    return {"ok": True, "posts": max(n * 2, 3), "accounts": n, "to": DEMO["gmail"] or "you@gmail.com"}


ROUTES = {
    "/api/instagram": api_instagram,
    "/api/gmail": api_gmail,
    "/api/schedule": api_schedule,
    "/api/accounts/check": api_check_accounts,
    "/api/accounts/save": api_save_accounts,
    "/api/welcome": api_welcome,
    "/api/first-digest": api_first_digest,
    "/api/finish": api_finish,
}

DEMO_ROUTES = {
    "/api/instagram": demo_instagram,
    "/api/gmail": demo_gmail,
    "/api/schedule": demo_schedule,
    "/api/accounts/check": demo_check_accounts,
    "/api/accounts/save": demo_save_accounts,
    "/api/welcome": demo_welcome,
    "/api/first-digest": demo_first_digest,
    "/api/finish": api_finish,
}


# ---------------------------------------------------------------- run

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(demo: bool = False) -> int:
    if demo:
        _demo_reset()
    server = SetupServer(_free_port())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opened = webbrowser.open(server.url)
    print("\n⁕ Epilog setup" + (" (demo — nothing is saved, sent or scheduled)" if demo else ""))
    print("Setup is open in your browser." if opened else "Open this link in your browser to set up Epilog:")
    print(f"  {server.url}\n")
    print("Keep this window open until you've finished. (Ctrl-C to stop.)")
    try:
        while not server.finished.wait(timeout=5):
            if time.monotonic() - server.last_seen > IDLE_TIMEOUT.total_seconds():
                print("\nSetup closed after a long pause. Run it again anytime — finished steps are remembered.")
                break
        else:
            print("\nSetup finished. You can close this window.")
    except KeyboardInterrupt:
        print("\nSetup stopped. Run it again anytime — finished steps are remembered.")
    finally:
        server.shutdown()
    return 0
