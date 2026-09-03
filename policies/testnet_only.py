"""Fail-closed Testnet host enforcement for SentinelOS.

Any Binance URL whose hostname is not an official Testnet host is rejected
with RuntimeError before a client is allowed to connect.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlparse

ALLOWED_TESTNET_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "testnet.binance.vision",
        "testnet.binancefuture.com",
    }
)


def verify_testnet_environment(url: str) -> str:
    """Return the verified Testnet hostname, or raise RuntimeError.

    Accepts a full URL or a bare hostname. Production and unknown hosts
    fail closed with the same error so callers cannot bypass the guard.
    """
    if url is None or not str(url).strip():
        raise RuntimeError("Blocked non-Testnet Binance host.")

    raw = str(url).strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    hostname = (parsed.hostname or "").lower().rstrip(".")

    if hostname not in ALLOWED_TESTNET_HOSTS:
        raise RuntimeError("Blocked non-Testnet Binance host.")

    return hostname
