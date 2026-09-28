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


PLAY_LABEL = "Play in app"
PLAY_FONT_PX = 13  # display size
FONT_PATHS = ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf",
              "/Library/Fonts/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def _font(px: int) -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    return ImageFont.load_default(size=px)


def _add_play_icon(img: Image.Image) -> Image.Image:
    """Bakes a "▶ Play in app" pill into the thumbnail — email clients can't layer elements reliably."""
    ss = 4  # draw big, then downsample for smooth edges
    fs = PLAY_FONT_PX * RETINA * ss
    font = _font(fs)
    left, top, right, bottom = font.getbbox(PLAY_LABEL)
    text_w, text_h = right - left, bottom - top
    tri, gap, pad_x, pad_y = fs * 0.7, fs * 0.45, fs * 0.85, fs * 0.6
    w = int(pad_x * 2 + tri + gap + text_w)
    h = int(pad_y * 2 + max(text_h, tri))

    pill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(pill)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h / 2, fill=(0, 0, 0, 165))
    cy = h / 2
    draw.polygon([(pad_x, cy - tri / 2), (pad_x, cy + tri / 2), (pad_x + tri * 0.87, cy)], fill="white")
    draw.text((pad_x + tri + gap, cy), PLAY_LABEL, font=font, fill="white", anchor="lm")

    pill = pill.resize((w // ss, h // ss), Image.LANCZOS)
    if pill.width > img.width * 0.9:  # very narrow thumbnails: shrink to fit
        k = img.width * 0.9 / pill.width
        pill = pill.resize((int(pill.width * k), int(pill.height * k)), Image.LANCZOS)
    base = img.convert("RGBA")
    base.alpha_composite(pill, ((img.width - pill.width) // 2, (img.height - pill.height) // 2))
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

    def add(key: str, img: Image.Image, data: bytes | None = None) -> int:
        nonlocal spent
        data = data or _jpeg(img)
        if inline_images:
            src = "data:image/jpeg;base64," + base64.b64encode(data).decode()
        else:
            cid = f"{key}@epilog"
            images[cid] = data
            src = f"cid:{cid}"
        imgs[key] = Img(src, round(img.width / RETINA), round(img.height / RETINA))
        spent += len(data)
        return len(data)

    def post_images(p) -> list[tuple[str, Image.Image, bytes, bool]]:
        """Loads and resizes every photo of a post, encoded once so its weight is known.
        Photos already prepared for an earlier render come straight from the cache."""
        from . import cache

        out = []
        for i, item in enumerate(p.items):
            key = f"post-{p.id}-{i}"
            if data := cache.image(key):
                out.append((key, Image.open(io.BytesIO(data)), data, item.is_video))
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
            out.append((key, im, data, item.is_video))
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
            weight = sum(len(data) for _, _, data, _ in loaded)
            # keep whole posts together: start a new email rather than split one
            # (count > 0 so a single huge post still goes out rather than looping)
            if count and budget_bytes and (spent + weight > budget_bytes or count + len(loaded) > MAX_IMAGES):
                full = True
                spill.append(p)
                continue
            media[p.id] = []
            for key, im, data, is_video in loaded:
                add(key, im, data)
                count += 1
                i = int(key.rsplit("-", 1)[1])
                href = f"{p.permalink}?img_index={i + 1}" if p.kind == "carousel" else p.permalink
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
