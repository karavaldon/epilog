"""Keeps the last digest on disk — its posts and its resized photos — so it can
be re-rendered (`epilog run --rebuild`) without asking Instagram again."""

import json
import logging
from dataclasses import asdict
from datetime import datetime
from hashlib import blake2b
from pathlib import Path

from .config import DATA_DIR
from .graph import Account, Media, Post
from .render import Digest

log = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR / "cache"
IMAGE_DIR = CACHE_DIR / "images"
LAST_DIGEST = CACHE_DIR / "last-digest.json"


# ---------------------------------------------------------------- images

def _image_path(key: str) -> Path:
    return IMAGE_DIR / (blake2b(key.encode(), digest_size=16).hexdigest() + ".jpg")


def image(key: str) -> bytes | None:
    try:
        return _image_path(key).read_bytes()
    except OSError:
        return None


def store_image(key: str, data: bytes) -> None:
    try:
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        _image_path(key).write_bytes(data)
    except OSError as e:
        log.debug("Couldn't cache image %s: %s", key, e)


def forget_images(keep: set[str]) -> None:
    """Drops cached photos that the newest digest doesn't use."""
    wanted = {_image_path(k).name for k in keep}
    for path in IMAGE_DIR.glob("*.jpg") if IMAGE_DIR.exists() else []:
        if path.name not in wanted:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------- the digest

def save(digest: Digest) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "generated_at": digest.generated_at.isoformat(),
            "unsupported": digest.unsupported,
            "failed": digest.failed,
            "notices": digest.notices,
            "accounts": [
                {"username": a.username, "name": a.name, "avatar_url": a.avatar_url,
                 "posts": [{**asdict(p), "timestamp": p.timestamp.isoformat()} for p in a.posts]}
                for a in digest.accounts
            ],
        }
        LAST_DIGEST.write_text(json.dumps(data, indent=1))
        forget_images({f"avatar-{a.username}" for a in digest.accounts} |
                      {f"post-{p.id}-{i}" for a in digest.accounts for p in a.posts
                       for i in range(len(p.items))})
    except OSError as e:
        log.warning("Couldn't save the digest for rebuilding: %s", e)


def load() -> Digest | None:
    try:
        data = json.loads(LAST_DIGEST.read_text())
    except (OSError, ValueError):
        return None
    accounts = [
        Account(a["username"], a["name"], a["avatar_url"], [
            Post(id=p["id"], permalink=p["permalink"], caption=p["caption"],
                 timestamp=datetime.fromisoformat(p["timestamp"]), kind=p["kind"],
                 items=[Media(m["url"], m["is_video"], m.get("video_url")) for m in p["items"]])
            for p in a["posts"]])
        for a in data["accounts"]
    ]
    return Digest(accounts=accounts, unsupported=data["unsupported"],
                  failed=[tuple(f) for f in data["failed"]], notices=data["notices"],
                  generated_at=datetime.fromisoformat(data["generated_at"]))
