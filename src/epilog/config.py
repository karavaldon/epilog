import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
# Running from a downloaded/cloned folder keeps everything in that folder; an
# installed package (no pyproject.toml alongside) uses ~/.epilog instead.
DATA_DIR = Path(os.getenv("EPILOG_HOME") or (
    _SOURCE_ROOT if (_SOURCE_ROOT / "pyproject.toml").exists() else Path.home() / ".epilog"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

ENV_PATH = DATA_DIR / ".env"
ACCOUNTS_PATH = DATA_DIR / "accounts.txt"
STATE_PATH = DATA_DIR / "state.json"
PREVIEW_PATH = DATA_DIR / "preview.html"
LOG_DIR = DATA_DIR / "logs"

ACCOUNTS_HEADER = (
    "# Instagram accounts Epilog follows, one username per line (no @).\n"
    "# You can edit this file, or reply to any Epilog email with handles to add them.\n"
)


@dataclass
class Config:
    app_id: str
    app_secret: str
    ig_user_id: str
    ig_username: str
    access_token: str
    token_expires_at: datetime | None
    gmail_address: str
    gmail_app_password: str
    digest_to: str
    api_version: str
    digest_time: str  # "HH:MM", local time

    @property
    def instagram_ready(self) -> bool:
        return bool(self.ig_user_id and self.access_token)

    @property
    def gmail_ready(self) -> bool:
        return bool(self.gmail_address and self.gmail_app_password)


def load_config() -> Config:
    load_dotenv(ENV_PATH, override=True)
    expires = os.getenv("IG_TOKEN_EXPIRES_AT", "").strip()
    gmail = os.getenv("GMAIL_ADDRESS", "").strip()
    return Config(
        app_id=os.getenv("META_APP_ID", "").strip(),
        app_secret=os.getenv("META_APP_SECRET", "").strip(),
        ig_user_id=os.getenv("IG_USER_ID", "").strip(),
        ig_username=os.getenv("IG_USERNAME", "").strip(),
        access_token=os.getenv("IG_ACCESS_TOKEN", "").strip(),
        token_expires_at=(
            datetime.fromtimestamp(int(expires), tz=timezone.utc) if expires and expires != "0" else None
        ),
        gmail_address=gmail,
        gmail_app_password=os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", ""),
        digest_to=os.getenv("DIGEST_TO", "").strip() or gmail,
        api_version=os.getenv("GRAPH_API_VERSION", "").strip() or "v25.0",
        digest_time=os.getenv("DIGEST_TIME", "").strip() or "07:00",
    )


def update_env(values: dict[str, str]) -> None:
    """Set keys in .env, keeping every other line (and comments) as-is."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in remaining.items())
    ENV_PATH.write_text("\n".join(out) + "\n")
    ENV_PATH.chmod(0o600)


def add_accounts(usernames: list[str]) -> None:
    text = ACCOUNTS_PATH.read_text() if ACCOUNTS_PATH.exists() else ACCOUNTS_HEADER
    if text and not text.endswith("\n"):
        text += "\n"
    ACCOUNTS_PATH.write_text(text + "".join(f"{u}\n" for u in usernames))


def remove_accounts(usernames: set[str]) -> None:
    lines = ACCOUNTS_PATH.read_text().splitlines()
    kept = [l for l in lines if l.split("#", 1)[0].strip().lstrip("@").lower() not in usernames]
    ACCOUNTS_PATH.write_text("\n".join(kept) + "\n")


def read_accounts() -> list[str]:
    if not ACCOUNTS_PATH.exists():
        return []
    seen, accounts = set(), []
    for line in ACCOUNTS_PATH.read_text().splitlines():
        name = line.split("#", 1)[0].strip().lstrip("@").lower()
        if name and name not in seen:
            seen.add(name)
            accounts.append(name)
    return accounts
