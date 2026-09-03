"""Append-only audit trail for SentinelOS.

Every confirmation, rejection, API call, risk block, and error is written
to ``audit_log.txt`` with a UTC timestamp. Secrets are never logged.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

AUDIT_LOG_PATH: Path = Path(__file__).resolve().parent.parent / "audit_log.txt"
_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def _format_fields(fields: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key, raw in fields.items():
        if raw is None:
            continue
        name = _one_line(key).replace("=", "_")
        if not name:
            continue
        lowered = name.lower()
        if any(token in lowered for token in ("secret", "api_key", "apikey", "token", "password")):
            continue
        parts.append(f"{name}={_one_line(raw)}")
    return " ".join(parts)


def audit(event_type: str, message: str, **fields: Any) -> None:
    """Append one audit line. Failures are swallowed so logging never blocks a trade abort."""
    kind = _one_line(event_type).upper().replace(" ", "_") or "EVENT"
    line = f"{_utc_now()} | {kind} | {_one_line(message)}"
    extra = _format_fields(fields)
    if extra:
        line = f"{line} | {extra}"
    try:
        with _LOCK:
            AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with AUDIT_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    except OSError:
        return


def read_recent(limit: int = 40) -> list[str]:
    """Return the newest *limit* audit lines (oldest-first within the window)."""
    if limit <= 0:
        return []
    if not AUDIT_LOG_PATH.is_file():
        return []
    try:
        data = AUDIT_LOG_PATH.read_bytes()
    except OSError:
        return []
    # Read from the tail so a large log does not stall the CLI.
    tail = data[-65536:] if len(data) > 65536 else data
    text = tail.decode("utf-8", errors="replace")
    lines = [row for row in text.splitlines() if row.strip()]
    if len(data) > 65536 and lines:
        lines = lines[1:]
    return lines[-limit:]
