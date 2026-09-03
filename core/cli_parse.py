"""CLI intent parsing for SentinelOS (keyword router)."""

from __future__ import annotations

import re
from dataclasses import dataclass

TRADE_HEAD_PATTERN = re.compile(
    r"^\s*(?:(futures)\s+)?(buy|sell)\s+([A-Za-z0-9/_-]+)\s+"
    r"([0-9]+(?:\.[0-9]+)?)\s*(.*)$",
    re.IGNORECASE,
)
FLAG_PATTERN = re.compile(
    r"--(sl|tp|stop-loss|take-profit)\s+([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
ANALYZE_PATTERN = re.compile(
    r"^\s*analyze(?:\s+market)?\s+([A-Za-z0-9/_-]+)\s*$",
    re.IGNORECASE,
)
HISTORY_PATTERN = re.compile(
    r"^\s*(?:history|audit|logs)(?:\s+(\d+))?\s*$",
    re.IGNORECASE,
)
BALANCE_PATTERN = re.compile(
    r"\b(balance|balances|account|portfolio|wallet)\b",
    re.IGNORECASE,
)
FUTURES_BALANCE_PATTERN = re.compile(
    r"^\s*futures\s+(balance|balances|account|portfolio|wallet)\s*$",
    re.IGNORECASE,
)
EXIT_COMMANDS = frozenset({"exit", "quit", "q", ":q"})
HELP_COMMANDS = frozenset({"help", "h", "?", "commands"})
KILL_COMMANDS = frozenset({"kill", "killswitch", "kill-switch", "kill_switch"})
AFFIRMATIVE = frozenset({"Y"})


@dataclass(frozen=True)
class TradeIntent:
    venue: str
    side: str
    symbol: str
    quantity: str
    stop_loss: str | None = None
    take_profit: str | None = None


def parse_trade_intent(text: str) -> TradeIntent | None:
    """Parse ``[futures] buy|sell SYMBOL QTY [--sl PRICE] [--tp PRICE]``."""
    match = TRADE_HEAD_PATTERN.match(text.strip())
    if match is None:
        return None

    venue_token, side, symbol, quantity, remainder = match.groups()
    flags = {
        item.group(1).lower(): item.group(2)
        for item in FLAG_PATTERN.finditer(remainder)
    }
    leftover = FLAG_PATTERN.sub("", remainder).strip()
    if leftover:
        return None

    stop_loss = flags.get("sl") or flags.get("stop-loss")
    take_profit = flags.get("tp") or flags.get("take-profit")
    return TradeIntent(
        venue="futures" if venue_token else "spot",
        side=side.upper(),
        symbol=symbol,
        quantity=quantity,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


def parse_analyze_symbol(text: str) -> str | None:
    match = ANALYZE_PATTERN.match(text.strip())
    if match is None:
        return None
    return match.group(1)


def parse_history_limit(text: str, default: int = 40) -> int | None:
    match = HISTORY_PATTERN.match(text.strip())
    if match is None:
        return None
    raw = match.group(1)
    if raw is None:
        return default
    limit = int(raw)
    return max(1, min(limit, 500))
