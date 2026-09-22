"""Installs the two background jobs: the daily digest and the reply check.

macOS uses launchd (LaunchAgents); Linux uses the user's crontab."""

import os
import plistlib
import platform
import re
import subprocess
import sys
from pathlib import Path

from .config import DATA_DIR, LOG_DIR

JOBS = {
    # name: (epilog command, label)
    "digest": ("run", "com.epilog.digest"),
    "inbox": ("inbox", "com.epilog.inbox"),
}
INBOX_EVERY_MINUTES = 15
CRON_MARKER = "# epilog"


class ScheduleError(Exception):
    pass


def supported() -> bool:
    return platform.system() in ("Darwin", "Linux")


def parse_time(value: str) -> tuple[int, int]:
    """Accepts "7", "7:00", "07:30", "7am", "6:45 pm"."""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*", value.lower())
    if not m:
        raise ValueError(f"Couldn't read “{value}” as a time — try something like 7:00 or 6:30 am.")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError(f"“{value}” isn't a valid time.")
        hour = hour % 12 + (12 if ampm.startswith("p") else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"“{value}” isn't a valid time.")
    return hour, minute


def format_time(hour: int, minute: int) -> str:
    return f"{hour % 12 or 12}:{minute:02d} {'am' if hour < 12 else 'pm'}"


def install(digest_time: str) -> None:
    hour, minute = parse_time(digest_time)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        _launchd_install(hour, minute)
    elif platform.system() == "Linux":
        _cron_install(hour, minute)
    else:
        raise ScheduleError("Automatic scheduling works on macOS and Linux only.")


def uninstall() -> None:
    if platform.system() == "Darwin":
        for _, label in JOBS.values():
            _launchctl("bootout", f"gui/{os.getuid()}/{label}", check=False)
            _plist_path(label).unlink(missing_ok=True)
    elif platform.system() == "Linux":
        _write_crontab([l for l in _read_crontab() if CRON_MARKER not in l])


def status() -> dict[str, bool]:
    """Which jobs are installed."""
    if platform.system() == "Darwin":
        return {name: _plist_path(label).exists() for name, (_, label) in JOBS.items()}
    if platform.system() == "Linux":
        lines = [l for l in _read_crontab() if CRON_MARKER in l]
        return {name: any(f"'epilog' '{cmd}'" in l for l in lines) for name, (cmd, _) in JOBS.items()}
    return {name: False for name in JOBS}


def _command(cmd: str) -> list[str]:
    # The interpreter of the environment Epilog is running in right now, so the
    # jobs keep working without uv or a particular PATH.
    return [sys.executable, "-m", "epilog", cmd]


# ---------------------------------------------------------------- macOS

def _plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _launchctl(*args: str, check: bool = True) -> None:
    result = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise ScheduleError(f"launchctl {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")


def _launchd_install(hour: int, minute: int) -> None:
    domain = f"gui/{os.getuid()}"
    for name, (cmd, label) in JOBS.items():
        plist = {
            "Label": label,
            "ProgramArguments": _command(cmd),
            "WorkingDirectory": str(DATA_DIR),
            "EnvironmentVariables": {"EPILOG_HOME": str(DATA_DIR), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            "StandardOutPath": str(LOG_DIR / f"{name}.log"),
            "StandardErrorPath": str(LOG_DIR / f"{name}.log"),
        }
        if name == "digest":  # missed while asleep → runs on wake
            plist["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
        else:
            plist["StartInterval"] = INBOX_EVERY_MINUTES * 60
        path = _plist_path(label)
        path.parent.mkdir(parents=True, exist_ok=True)
        _launchctl("bootout", f"{domain}/{label}", check=False)  # replace any previous version
        with path.open("wb") as f:
            plistlib.dump(plist, f)
        _launchctl("bootstrap", domain, str(path))


# ---------------------------------------------------------------- Linux

def _read_crontab() -> list[str]:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return result.stdout.splitlines() if result.returncode == 0 else []


def _write_crontab(lines: list[str]) -> None:
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


def _cron_install(hour: int, minute: int) -> None:
    def line(schedule: str, name: str, cmd: str) -> str:
        run = " ".join(f"'{a}'" for a in _command(cmd))
        return (f"{schedule} cd '{DATA_DIR}' && EPILOG_HOME='{DATA_DIR}' {run} "
                f">> '{LOG_DIR / (name + '.log')}' 2>&1 {CRON_MARKER}")

    lines = [l for l in _read_crontab() if CRON_MARKER not in l]
    lines.append(line(f"{minute} {hour} * * *", "digest", "run"))
    lines.append(line(f"*/{INBOX_EVERY_MINUTES} * * * *", "inbox", "inbox"))
    _write_crontab(lines)
