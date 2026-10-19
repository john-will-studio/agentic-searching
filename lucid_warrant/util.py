"""Small shared helpers: timestamps, file-safe IDs and terminal detection."""

from __future__ import annotations

import re
import secrets
import sys
from datetime import datetime, timezone

_ID_RE = re.compile(r"^[a-z]+-\d{8}-\d{6}-[0-9a-f]{6}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{utc_now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def check_id(value: str, prefix: str) -> str:
    """Reject anything that isn't a generated ID, so IDs can't traverse paths."""
    if not isinstance(value, str) or not _ID_RE.match(value) or not value.startswith(prefix + "-"):
        raise ValueError(f"not a valid {prefix} id: {value!r}")
    return value


def stdin_is_terminal() -> bool:
    """Whether a person can type at stdin. On Windows the NUL device also reports isatty(), and getpass reads the
    console directly, so an agent whose stdin is NUL would wait forever at a passphrase prompt: ask the console."""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        if sys.platform != "win32":
            return True
        import ctypes
        import msvcrt

        mode = ctypes.c_ulong()
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
    except (OSError, ValueError, AttributeError):
        return False


def replace_file(src, dst, attempts: int = 6) -> None:
    """os.replace, retried briefly when Windows reports the destination as locked (WinError 5 or 32): an antivirus
    scanner or the search indexer can hold a file for a moment just after it's written. Up to about a second in
    all; other errors, and a lock that doesn't clear, are raised."""
    import os
    import time

    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32) or attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
