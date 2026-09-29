import base64
import io
import logging
from dataclasses import dataclass, field, replace
from importlib import metadata
from datetime import datetime
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from PIL import Image, ImageDraw, ImageFont

from .graph import Account

log = logging.getLogger(__name__)

try:
    VERSION = metadata.version("epilog")
except metadata.PackageNotFoundError:  # running straight from a source folder
    VERSION = "dev"

POST_WIDTH = 400  # display px; small enough that a 4:5 post fits on screen with its caption
VIDEO_HEIGHT = 260  # display px, max height for video thumbnails
CAROUSEL_GAP = 4  # px between stacked carousel images
AVATAR_SIZE = 36
RETINA = 2  # images are stored at 2x their display size
JPEG_QUALITY = 78
# Phone mail apps labour over very large messages, so a digest fills up to this
# much and then continues in a second email rather than growing without limit.
# (Base64 adds about a third on top, so ~5 MB of photos ≈ a 7 MB email.)
IMAGE_BUDGET_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 80  # per email


@dataclass
class Digest:
    accounts: list[Account]  # only accounts with new posts
    unsupported: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    part: int = 1        # a heavy day is split across several emails
    parts: int = 1
    update_available: str = ""   # tag of a newer release, if there is one

    @property
    def entries(self) -> list[tuple[Account, "object"]]:
        """Every post, newest first, each paired with the account that posted it —
        so the email reads as a timeline rather than grouped by account."""
        pairs = [(a, p) for a in self.accounts for p in a.posts]
        return sorted(pairs, key=lambda ap: ap[1].timestamp, reverse=True)

    @property
    def has_video_links(self) -> bool:
        return any(p.video_hours for a in self.accounts for p in a.posts)

    @property
    def post_count(self) -> int:
        return sum(len(a.posts) for a in self.accounts)

    @property
    def subject(self) -> str:
        day = f"⁕ Your {self.generated_at:%A} epilog, {self.generated_at:%b %-d}"
        return day if self.parts == 1 else f"{day} ({self.part} of {self.parts})"

    @property
    def preheader(self) -> str:
        """The gray preview line inboxes show under the subject:
        “7 new posts from @honeysatx, @marthas.atx and 3 more”."""
        if not self.post_count:
            return self.notices[0] if self.notices else "No new posts today"
        named = [f"@{a.username}" for a in self.accounts[:3]]
        rest = len(self.accounts) - len(named)
        who = ", ".join(named[:-1]) + f" and {named[-1]}" if len(named) > 1 and not rest else ", ".join(named)
        if rest:
            who += f" and {rest} more"
        line = f"{_plural(self.post_count, 'new post')} from {who}"
        return line if self.parts == 1 else f"Part {self.part} of {self.parts} · {line}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


@dataclass
class Img:
    src: str
    width: int  # display size
    height: int
    is_video: bool = False
    href: str = ""


def _load(url: str | None) -> Image.Image | None:
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as e:  # noqa: BLE001 — a missing image shouldn't sink the digest
        log.warning("Couldn't load image %s: %s", url[:80], e)
        return None


def _square(img: Image.Image) -> Image.Image:
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    return img.crop((left, top, left + side, top + side))


PLAY_LABEL = "Play"


def _add_play_icon(img: Image.Image) -> Image.Image:
    """Bakes the pill into a still — email clients can't layer elements reliably."""
    from .badge import pill

    badge = pill(PLAY_LABEL)
    if badge.width > img.width * 0.9:          # very narrow thumbnails: shrink to fit
        k = img.width * 0.9 / badge.width
        badge = badge.resize((int(badge.width * k), int(badge.height * k)), Image.LANCZOS)
    base = img.convert("RGBA")
    base.alpha_composite(badge, ((img.width - badge.width) // 2, (img.height - badge.height) // 2))
    return base.convert("RGB")


def _jpeg(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return out.getvalue()


def _nl2br(text: str) -> Markup:
    return Markup("<br>").join(escape(line) for line in text.strip().splitlines())


def _when(ts: datetime) -> str:
    local = ts.astimezone()
    return local.strftime("%a %-I:%M %p").replace("AM", "am").replace("PM", "pm")


def template_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(nl2br=_nl2br, when=_when)
    return env


def _collect(digest: Digest, inline_images: bool, budget_bytes: int | None):
    """Fetches the photos for as much of the digest as fits in `budget_bytes`.

    Returns the assets, the digest trimmed to what fits, and whatever is left
    over (to become the next email). `budget_bytes=None` means no limit."""
    images: dict[str, bytes] = {}
    imgs: dict[str, Img] = {}
    spent = 0

    def add(key: str, img: Image.Image, data: bytes | None = None, retina: bool = True) -> int:
        nonlocal spent
        data = data or _jpeg(img)
        if inline_images:
            src = "data:image/jpeg;base64," + base64.b64encode(data).decode()
        else:
            cid = f"{key}@epilog"
            images[cid] = data
            src = f"cid:{cid}"
        scale = RETINA if retina else 1
        imgs[key] = Img(src, round(img.width / scale), round(img.height / scale))
        spent += len(data)
        return len(data)

    def post_images(p) -> list[tuple[str, Image.Image, bytes, bool, bool]]:
        """Prepares every frame of a post: photos as JPEGs, videos as a short looping
        GIF where one can be made (falling back to the still with a play badge).
        Anything prepared for an earlier render comes straight from the cache."""
        from . import cache, clips

        out = []
        for i, item in enumerate(p.items):
            key = f"post-{p.id}-{i}"
            if item.is_video and item.video_url:
                # how long Instagram's link lasts, cached preview or not
                if hours := clips.link_hours(item.video_url):
                    p.video_hours = hours
            if data := cache.image(key):
                im = Image.open(io.BytesIO(data))
                out.append((key, im, data, item.is_video, data[:3] == b"GIF"))
                continue

            if item.is_video and (gif := clips.preview(item.video_url)):
                cache.store_image(key, gif)
                out.append((key, Image.open(io.BytesIO(gif)), gif, True, True))
                continue

            if not (im := _load(item.url)):
                continue
            if item.is_video:
                im.thumbnail((POST_WIDTH * RETINA, VIDEO_HEIGHT * RETINA), Image.LANCZOS)
                im = _add_play_icon(im)
            else:
                im.thumbnail((POST_WIDTH * RETINA, POST_WIDTH * RETINA * 5 // 4), Image.LANCZOS)
            data = _jpeg(im)
            cache.store_image(key, data)
            out.append((key, im, data, item.is_video, False))
        return out

    media: dict[str, list[Img]] = {}  # post id → one image per carousel item
    kept: list[Account] = []
    leftover: list[Account] = []
    count = 0
    full = False

    for a in digest.accounts:
        if full:                                   # everything from here is the next email
            leftover.append(a)
            continue
        from . import cache
        key = f"avatar-{a.username}"
        if data := cache.image(key):
            add(key, Image.open(io.BytesIO(data)), data)
        elif avatar := _load(a.avatar_url):
            avatar = _square(avatar)
            avatar.thumbnail((AVATAR_SIZE * RETINA,) * 2, Image.LANCZOS)
            cache.store_image(key, _jpeg(avatar))
            add(key, avatar)

        here, spill = [], []
        for p in a.posts:
            if full:
                spill.append(p)
                continue
            loaded = post_images(p)
            weight = sum(len(data) for _, _, data, _, _ in loaded)
            # keep whole posts together: start a new email rather than split one
            # (count > 0 so a single huge post still goes out rather than looping)
            if count and budget_bytes and (spent + weight > budget_bytes or count + len(loaded) > MAX_IMAGES):
                full = True
                spill.append(p)
                continue
            media[p.id] = []
            for key, im, data, is_video, is_gif in loaded:
                add(key, im, data, retina=not is_gif)
                count += 1
                i = int(key.rsplit("-", 1)[1])
                if is_gif:            # tapping the preview plays the video itself
                    href = p.items[i].video_url or p.permalink
                elif p.kind == "carousel":
                    href = f"{p.permalink}?img_index={i + 1}"
                else:
                    href = p.permalink
                media[p.id].append(replace(imgs[key], is_video=is_video, href=href))
            here.append(p)

        if here:
            kept.append(replace(a, posts=here))
        if spill:
            leftover.append(replace(a, posts=spill))

    rest = None
    if leftover:
        # the unsupported/failed notes ride along with the final part only
        rest = replace(digest, accounts=leftover)
        digest = replace(digest, accounts=kept, unsupported=[], failed=[])
    return imgs, media, images, digest, rest


def _render_html(digest: Digest, imgs: dict[str, Img], media: dict[str, list[Img]]) -> tuple[str, str]:
    env = template_env()
    ctx = dict(d=digest, img=imgs, media=media, post_width=POST_WIDTH, gap=CAROUSEL_GAP, version=VERSION)
    return (env.get_template("digest.html.j2").render(**ctx),
            env.get_template("digest.txt.j2").render(**ctx))


def render(digest: Digest, inline_images: bool) -> tuple[str, str, dict[str, bytes]]:
    """One unlimited render — used for the browser preview."""
    imgs, media, images, digest, _ = _collect(digest, inline_images, budget_bytes=None)
    html, text = _render_html(digest, imgs, media)
    return html, text, images


def render_parts(digest: Digest, budget_bytes: int = IMAGE_BUDGET_BYTES
                 ) -> list[tuple[Digest, str, str, dict[str, bytes]]]:
    """Splits a heavy digest across several emails, each within the budget.
    Part numbering is filled in once the total is known."""
    collected = []
    remaining = digest
    while remaining is not None:
        imgs, media, images, part, remaining = _collect(remaining, False, budget_bytes)
        collected.append((part, imgs, media, images))

    out = []
    for i, (part, imgs, media, images) in enumerate(collected, start=1):
        part = replace(part, part=i, parts=len(collected))
        html, text = _render_html(part, imgs, media)
        out.append((part, html, text, images))
    return out
