"""Connecting Epilog to Instagram: turn a Graph API Explorer token into a saved,
long-lived connection."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from .config import Config, update_env
from .graph import Graph, GraphError

REQUIRED_SCOPES = {"instagram_basic", "instagram_manage_insights", "pages_read_engagement", "pages_show_list"}


class ConnectError(Exception):
    """Something the person needs to fix on Meta's side; the message says what."""


@dataclass
class Connection:
    username: str
    ig_user_id: str
    expires_at: datetime | None
    missing_scopes: set[str]


def connect(cfg: Config, app_id: str, app_secret: str, short_token: str,
            choose: Callable[[list[dict]], dict]) -> Connection:
    """Exchanges the token, finds the linked Instagram account, saves it to .env
    and makes a test call. `choose` picks a Page when several are linked."""
    graph = Graph(short_token, cfg.api_version)
    try:
        token = graph.get("oauth/access_token", grant_type="fb_exchange_token", client_id=app_id,
                          client_secret=app_secret, fb_exchange_token=short_token,
                          access_token=None)["access_token"]
        info = graph.get("debug_token", input_token=token, access_token=f"{app_id}|{app_secret}")["data"]
        pages = Graph(token, cfg.api_version).get(
            "me/accounts", fields="name,instagram_business_account{id,username}")["data"]
    except GraphError as e:
        raise ConnectError(
            f"Meta rejected the details: {e}\n"
            "Check the App ID and App Secret (App settings → Basic), and that the token was "
            "generated for this app in Graph API Explorer in the last hour."
        ) from e
    except requests.RequestException as e:
        raise ConnectError(f"Couldn't reach Meta: {e}") from e

    linked = [p for p in pages if p.get("instagram_business_account")]
    if not linked:
        raise ConnectError(
            "No Facebook Page with a linked Instagram account was found for this token.\n"
            "Check that your Instagram is a Business or Creator account, that it's linked to a "
            "Facebook Page, and that you selected that Page when generating the token."
        )
    page = linked[0] if len(linked) == 1 else choose(linked)
    ig = page["instagram_business_account"]

    try:
        Graph(token, cfg.api_version).business_discovery(ig["id"], ig.get("username", ""), limit=1)
    except (GraphError, requests.RequestException) as e:
        raise ConnectError(
            f"Connected, but a test read failed: {e}\n"
            "This usually means a permission is missing — regenerate the token with all five "
            "permissions ticked."
        ) from e

    expires_at = int(info.get("expires_at") or 0)
    update_env({
        "META_APP_ID": app_id,
        "META_APP_SECRET": app_secret,
        "IG_USER_ID": ig["id"],
        "IG_USERNAME": ig.get("username", ""),
        "IG_ACCESS_TOKEN": token,
        "IG_TOKEN_EXPIRES_AT": str(expires_at),
    })
    return Connection(
        username=ig.get("username", ""),
        ig_user_id=ig["id"],
        expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc) if expires_at else None,
        missing_scopes=REQUIRED_SCOPES - set(info.get("scopes", [])),
    )


def check_connection(cfg: Config) -> str | None:
    """Returns None if the saved connection works, otherwise why it doesn't."""
    if not cfg.instagram_ready:
        return "not set up"
    if cfg.token_expires_at and cfg.token_expires_at <= datetime.now(timezone.utc):
        return "expired"
    try:
        Graph(cfg.access_token, cfg.api_version).business_discovery(cfg.ig_user_id, cfg.ig_username, limit=1)
    except (GraphError, requests.RequestException) as e:
        return str(e)
    return None
