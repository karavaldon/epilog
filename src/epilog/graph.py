"""Minimal client for the Instagram Graph API (Facebook Login flavour)."""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://graph.facebook.com"
RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}
AUTH_CODES = {102, 190}
PERMISSION_CODES = {10, 200, 299}
NETWORK_RETRY_WAITS = [5, 20]  # seconds before the 2nd and 3rd attempts

MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,"
    "children{media_type,media_url,thumbnail_url}"
)


class GraphError(Exception):
    def __init__(self, message: str, code: int | None = None, subcode: int | None = None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode


class AuthError(GraphError):
    """Token expired, revoked or missing permissions — needs `epilog setup-token`."""


class NotSupported(GraphError):
    """Username isn't a Business/Creator account (or doesn't exist)."""


class RateLimited(GraphError):
    pass


@dataclass
class Media:
    url: str | None  # image, or the thumbnail for a video
    is_video: bool = False


@dataclass
class Post:
    id: str
    permalink: str
    caption: str
    timestamp: datetime
    kind: str  # "image" | "video" | "carousel"
    items: list[Media]  # one per photo/video; several for a carousel

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass
class Account:
    username: str
    name: str
    avatar_url: str | None
    posts: list[Post] = field(default_factory=list)


class Graph:
    def __init__(self, token: str, version: str):
        self.token = token
        self.version = version
        self.session = requests.Session()

    def get(self, path: str, **params) -> dict:
        params.setdefault("access_token", self.token)
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(2):
            resp = self._request(f"{BASE_URL}/{self.version}/{path.lstrip('/')}", params)
            try:
                data = resp.json()
            except ValueError:
                raise GraphError(f"HTTP {resp.status_code}: non-JSON response") from None
            self._respect_usage(resp.headers)
            if "error" not in data:
                return data
            err = _classify(data["error"])
            if isinstance(err, RateLimited) and attempt == 0:
                log.warning("Rate limited (%s); waiting 5 minutes", err)
                time.sleep(300)
                continue
            raise err
        raise AssertionError("unreachable")

    def _request(self, url: str, params: dict) -> requests.Response:
        """GET with retries for dropped connections and timeouts (e.g. the Mac waking up)."""
        for wait in NETWORK_RETRY_WAITS + [None]:
            try:
                return self.session.get(url, params=params, timeout=30)
            except (requests.ConnectionError, requests.Timeout) as e:
                if wait is None:
                    raise
                log.info("Network error (%s); retrying in %ss", type(e).__name__, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def business_discovery(self, ig_user_id: str, username: str, limit: int = 10) -> Account:
        fields = (
            f"business_discovery.username({username})"
            f"{{username,name,profile_picture_url,media.limit({limit}){{{MEDIA_FIELDS}}}}}"
        )
        bd = self.get(ig_user_id, fields=fields)["business_discovery"]
        return Account(
            username=bd.get("username", username),
            name=bd.get("name") or bd.get("username", username),
            avatar_url=bd.get("profile_picture_url"),
            posts=[p for p in map(_parse_post, bd.get("media", {}).get("data", [])) if p],
        )

    def _respect_usage(self, headers) -> None:
        """Back off when Meta reports we're close to the rate limit."""
        pct = 0
        for header in ("X-App-Usage", "X-Business-Use-Case-Usage"):
            raw = headers.get(header)
            if not raw:
                continue
            try:
                usage = json.loads(raw)
            except ValueError:
                continue
            buckets = [usage] if header == "X-App-Usage" else [b for v in usage.values() for b in v]
            for b in buckets:
                pct = max(pct, b.get("call_count", 0), b.get("total_cputime", 0), b.get("total_time", 0))
                wait_min = b.get("estimated_time_to_regain_access", 0)
                if wait_min:
                    log.warning("Meta says wait %s min to regain access", wait_min)
                    time.sleep(min(wait_min, 30) * 60)
        if pct >= 80:
            log.warning("API usage at %s%%; pausing 60s", pct)
            time.sleep(60)


def _classify(error: dict) -> GraphError:
    msg = error.get("error_user_msg") or error.get("message", "Unknown Graph API error")
    code, sub = error.get("code"), error.get("error_subcode")
    if code in AUTH_CODES or code in PERMISSION_CODES:
        return AuthError(msg, code, sub)
    if code in RATE_LIMIT_CODES:
        return RateLimited(msg, code, sub)
    # Business Discovery returns code 110 / subcode 2207013 for personal or unknown accounts.
    if code == 110 or sub == 2207013 or "business_discovery" in msg.lower():
        return NotSupported(msg, code, sub)
    return GraphError(msg, code, sub)


def _parse_post(m: dict) -> Post | None:
    try:
        ts = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
    except (KeyError, ValueError):
        return None
    media_type = m.get("media_type")
    children = m.get("children", {}).get("data", [])
    if media_type == "CAROUSEL_ALBUM":
        kind, items = "carousel", [_media_of(c) for c in children] or [Media(None)]
    else:
        kind, items = ("video" if media_type == "VIDEO" else "image"), [_media_of(m)]
    return Post(
        id=m["id"],
        permalink=m.get("permalink", ""),
        caption=m.get("caption", "") or "",
        timestamp=ts,
        kind=kind,
        items=items,
    )


def _media_of(item: dict) -> Media:
    if item.get("media_type") == "VIDEO":
        return Media(item.get("thumbnail_url"), is_video=True)
    return Media(item.get("media_url"))
