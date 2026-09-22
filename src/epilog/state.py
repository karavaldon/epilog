import json
from datetime import datetime

from .config import STATE_PATH

MAX_REPLY_IDS = 500


class State:
    """Remembers the newest post timestamp seen per account, which email
    replies have already been processed, and where onboarding stands."""

    def __init__(self) -> None:
        data = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        self.last_seen: dict[str, str] = data.get("last_seen", {})
        self.processed_replies: list[str] = data.get("processed_replies", [])
        # True between sending the welcome email and the first reply that adds accounts.
        self.welcome_pending: bool = data.get("welcome_pending", False)

    def get(self, username: str) -> datetime | None:
        value = self.last_seen.get(username)
        return datetime.fromisoformat(value) if value else None

    def mark(self, username: str, newest: datetime) -> None:
        current = self.get(username)
        if current is None or newest > current:
            self.last_seen[username] = newest.isoformat()

    def mark_reply(self, message_id: str) -> None:
        self.processed_replies = (self.processed_replies + [message_id])[-MAX_REPLY_IDS:]

    def save(self) -> None:
        data = {
            "last_seen": self.last_seen,
            "processed_replies": self.processed_replies,
            "welcome_pending": self.welcome_pending,
        }
        STATE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
