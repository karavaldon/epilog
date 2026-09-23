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
MAX_IMAGES = 120  # keeps the email well under Gmail's 25 MB limit


@dataclass
class Digest:
    accounts: list[Account]  # only accounts with new posts
    unsupported: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now().astimezone())

    @property
    def post_count(self) -> int:
        return sum(len(a.posts) for a in self.accounts)

    @property
    def subject(self) -> str:
        return f"⁕ {self.generated_at:%A, %b %-d}"

    @property
    def preheader(self) -> str:
        """The gray preview line inboxes show under the subject."""
        if not self.post_count:
            return self.notices[0] if self.notices else "No new posts today"
        return f"{_plural(self.post_count, 'post')} from {_plural(len(self.accounts), 'account')}"


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
    img.save(out, "JPEG", quality=82, optimize=True, progressive=True)
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


def render(digest: Digest, inline_images: bool) -> tuple[str, str, dict[str, bytes]]:
    """Returns (html, text, images). With inline_images=True the images are
    data: URIs (for the browser preview); otherwise cid: references to attach."""
    images: dict[str, bytes] = {}
    imgs: dict[str, Img] = {}

    def add(key: str, img: Image.Image | None) -> None:
        if img is None:
            return
        data = _jpeg(img)
        if inline_images:
            src = "data:image/jpeg;base64," + base64.b64encode(data).decode()
        else:
            cid = f"{key}@epilog"
            images[cid] = data
            src = f"cid:{cid}"
        imgs[key] = Img(src, round(img.width / RETINA), round(img.height / RETINA))

    budget = MAX_IMAGES
    media: dict[str, list[Img]] = {}  # post id → one image per carousel item
    for a in digest.accounts:
        if avatar := _load(a.avatar_url):
            avatar = _square(avatar)
            avatar.thumbnail((AVATAR_SIZE * RETINA,) * 2, Image.LANCZOS)
            add(f"avatar-{a.username}", avatar)
        for p in a.posts:
            media[p.id] = []
            for i, item in enumerate(p.items):
                if budget <= 0 or not (im := _load(item.url)):
                    continue
                budget -= 1
                if item.is_video:
                    im.thumbnail((POST_WIDTH * RETINA, VIDEO_HEIGHT * RETINA), Image.LANCZOS)
                    im = _add_play_icon(im)
                else:
                    im.thumbnail((POST_WIDTH * RETINA, POST_WIDTH * RETINA * 5 // 4), Image.LANCZOS)
                key = f"post-{p.id}-{i}"
                add(key, im)
                href = f"{p.permalink}?img_index={i + 1}" if p.kind == "carousel" else p.permalink
                media[p.id].append(replace(imgs[key], is_video=item.is_video, href=href))

    env = template_env()
    ctx = dict(d=digest, img=imgs, media=media, post_width=POST_WIDTH, gap=CAROUSEL_GAP, version=VERSION)
    html = env.get_template("digest.html.j2").render(**ctx)
    text = env.get_template("digest.txt.j2").render(**ctx)
    return html, text, images
