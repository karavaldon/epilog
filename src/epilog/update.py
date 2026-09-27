"""`epilog update`: fetch the newest release and replace the program files in
place, leaving settings, accounts, history and the schedule alone."""

import io
import logging
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import requests

from .config import DATA_DIR

log = logging.getLogger(__name__)

REPO = "karavaldon/epilog"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
# what an update replaces; everything else in the folder is the person's own
PROGRAM_FILES = ["src", "pyproject.toml", "uv.lock", "README.md", "LICENSE",
                 "setup.sh", "Setup Epilog.command", ".env.example", "docs"]
BACKUP = DATA_DIR / "cache" / "previous-version"


class UpdateError(Exception):
    pass


def _version_tuple(text: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", text)) or (0,)


def latest_release() -> dict:
    try:
        resp = requests.get(LATEST, timeout=30, headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise UpdateError(f"Couldn't reach GitHub: {e}") from e


def available(current: str) -> tuple[bool, str]:
    """Returns (is_newer, tag) for the published release."""
    tag = latest_release().get("tag_name", "")
    return _version_tuple(tag) > _version_tuple(current), tag


def _download(release: dict) -> bytes:
    assets = release.get("assets") or []
    url = next((a["browser_download_url"] for a in assets if a["name"].endswith(".zip")),
               release.get("zipball_url"))
    if not url:
        raise UpdateError("That release has no download.")
    try:
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise UpdateError(f"Couldn't download the update: {e}") from e
    return resp.content


def _unpack(data: bytes, into: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(into)
    # the zip holds a single top folder; find the one with the program in it
    for candidate in [into, *into.iterdir()]:
        if (candidate / "pyproject.toml").exists() and (candidate / "src").is_dir():
            return candidate
    raise UpdateError("The download didn't look like Epilog.")


def install(release: dict) -> str:
    """Replaces the program files with the release's. Returns the folder updated."""
    if not (DATA_DIR / "pyproject.toml").exists():
        raise UpdateError(f"{DATA_DIR} isn't an Epilog folder that can update itself.")

    staging = DATA_DIR / "cache" / "update"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    source = _unpack(_download(release), staging)

    shutil.rmtree(BACKUP, ignore_errors=True)
    BACKUP.mkdir(parents=True, exist_ok=True)
    for name in PROGRAM_FILES:
        old, new = DATA_DIR / name, source / name
        if not new.exists():
            continue
        if old.exists():                      # keep a copy in case the update misbehaves
            (shutil.copytree if old.is_dir() else shutil.copy2)(old, BACKUP / name)
            if old.is_dir():
                shutil.rmtree(old)
            else:
                old.unlink()
        (shutil.copytree if new.is_dir() else shutil.copy2)(new, old)
        if old.suffix in ("", ".sh", ".command") and old.is_file():
            old.chmod(0o755)
    shutil.rmtree(staging, ignore_errors=True)

    if uv := shutil.which("uv"):              # refresh dependencies and the version metadata
        subprocess.run([uv, "sync", "--quiet"], cwd=DATA_DIR, capture_output=True)
    else:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", "."],
                       cwd=DATA_DIR, capture_output=True)
    return str(DATA_DIR)
