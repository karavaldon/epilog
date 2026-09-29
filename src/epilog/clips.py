"""Short looping previews of videos, for the epilog itself.

ffmpeg reads the first seconds straight from Instagram's CDN (it supports range
requests), so making a preview doesn't mean downloading the whole video."""

import logging
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .badge import pill_png

log = logging.getLogger(__name__)

PLAY_LABEL = "Play"

SECONDS = 2
TIMEOUT = 90
MAX_BYTES = 650 * 1024      # a preview shouldn't outweigh several photos
# tried in order until one comes in under the size limit: (fps, colours, width, height)
TIERS = [(5, 48, 300, 340), (4, 32, 240, 280)]


def ffmpeg_path() -> str | None:
    """The system ffmpeg if there is one, otherwise the copy that ships with us."""
    if found := shutil.which("ffmpeg"):
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001 — previews are optional
        log.debug("No ffmpeg available: %s", e)
        return None


def link_hours(video_url: str) -> int | None:
    """How long Instagram's own link keeps working: its signature carries the expiry."""
    from urllib.parse import parse_qs, urlparse
    try:
        expires = int(parse_qs(urlparse(video_url).query)["oe"][0], 16)
    except (KeyError, IndexError, ValueError):
        return None
    left = (datetime.fromtimestamp(expires) - datetime.now()).total_seconds() / 3600
    return max(0, round(left)) if left > 0 else None


def preview(video_url: str) -> bytes | None:
    """A silent, looping GIF of the opening seconds, or None if one can't be made
    small enough — in which case the caller falls back to the still thumbnail."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg or not video_url:
        return None

    data = None
    for fps, colors, w, h in TIERS:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "preview.gif"
            pill = Path(tmp) / "pill.png"
            pill.write_bytes(pill_png(PLAY_LABEL, font_px=16))
            filters = (f"[0:v]fps={fps},"
                       f"scale='min(iw,{w})':'min(ih,{h})':force_original_aspect_ratio=decrease[v];"
                       "[v][1:v]overlay=(W-w)/2:(H-h)/2[o];"          # pill centred on each frame
                       f"[o]split[a][b];[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
                       "[b][p]paletteuse=dither=none")
            try:
                result = subprocess.run(
                    [ffmpeg, "-loglevel", "error", "-y", "-ss", "0", "-t", str(SECONDS),
                     "-i", video_url, "-i", str(pill), "-filter_complex", filters,
                     "-loop", "0", str(out)],
                    capture_output=True, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                log.debug("Preview timed out")
                return None
            if result.returncode != 0 or not out.exists():
                log.debug("Preview failed: %s", result.stderr.decode()[:200])
                return None
            data = out.read_bytes()
            if len(data) <= MAX_BYTES:
                return data
            log.debug("Preview was %d KB, trying a smaller one", len(data) // 1024)
    return None
