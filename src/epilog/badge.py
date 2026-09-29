"""The little pill drawn over video previews, e.g. “▶ Play in new tab”."""

import io

from PIL import Image, ImageDraw, ImageFont

FONT_PATHS = ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf",
              "/Library/Fonts/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
SUPERSAMPLE = 4


def font(px: int) -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    return ImageFont.load_default(size=px)


def pill(text: str, font_px: int = 13) -> Image.Image:
    """A transparent RGBA pill: dark rounded rectangle, play triangle, label."""
    ss = SUPERSAMPLE
    fs = font_px * ss
    f = font(fs)
    left, top, right, bottom = f.getbbox(text)
    text_w, text_h = right - left, bottom - top
    tri, gap, pad_x, pad_y = fs * 0.7, fs * 0.45, fs * 0.85, fs * 0.6
    w = int(pad_x * 2 + tri + gap + text_w)
    h = int(pad_y * 2 + max(text_h, tri))

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h / 2, fill=(0, 0, 0, 165))
    cy = h / 2
    draw.polygon([(pad_x, cy - tri / 2), (pad_x, cy + tri / 2), (pad_x + tri * 0.87, cy)], fill="white")
    draw.text((pad_x + tri + gap, cy), text, font=f, fill="white", anchor="lm")
    return img.resize((w // ss, h // ss), Image.LANCZOS)


def pill_png(text: str, font_px: int = 13) -> bytes:
    out = io.BytesIO()
    pill(text, font_px).save(out, "PNG")
    return out.getvalue()
