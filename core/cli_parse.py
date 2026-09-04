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
ANALYZE_IN_TEXT_PATTERN = re.compile(
    r"\banalyze(?:\s+market)?\s+([A-Za-z0-9/_-]+)",
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
POSITIONS_PATTERN = re.compile(
    r"^\s*(?:(?:show|check|list|get|fetch)\s+)?"
    r"(?:(?:my|the|open)\s+)*"
    r"(?:futures\s+)?"
    r"positions"
    r"(?:\s+([A-Za-z0-9/_-]+))?"
    r"\s*$",
    re.IGNORECASE,
)
SIMPLE_BALANCE_PATTERN = re.compile(
    r"^\s*(?:spot\s+)?(balance|balances|account|portfolio|wallet)\s*$",
    re.IGNORECASE,
)
PERCENT_OF_AVAILABLE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*%(?:\s+of\s+(?:my\s+)?available(?:\s+(USDT|USDC))?)?",
    re.IGNORECASE,
)
STABLE_GATE_PATTERN = re.compile(
    r"\bif\s+(?:the\s+)?(?:market|it)\s+looks\s+stable\b|\bif\s+stable\b",
    re.IGNORECASE,
)
SPOT_PAIR_PATTERN = re.compile(r"\b([A-Za-z]{2,10}USDT)\b")
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


@dataclass(frozen=True)
class PositionsIntent:
    """Read-only Futures Testnet open-positions query."""

    symbol: str | None = None


@dataclass(frozen=True)
class WorkflowIntent:
    """Multi-step natural-language plan. Submit is never part of this object."""

    check_spot_balance: bool = False
    check_futures_balance: bool = False
    check_futures_positions: bool = False
    analyze_symbol: str | None = None
    require_stable: bool = False
    trade_venue: str | None = None
    trade_side: str | None = None
    trade_symbol: str | None = None
    percent_of_available: str | None = None
    percent_asset: str = "USDT"
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


def parse_positions_intent(text: str) -> PositionsIntent | None:
    """Parse ``[futures] positions [SYMBOL]`` and close synonyms."""
    match = POSITIONS_PATTERN.match(text.strip())
    if match is None:
        return None
    raw = match.group(1)
    symbol = raw.strip().upper().replace("-", "").replace("_", "").replace("/", "") if raw else None
    return PositionsIntent(symbol=symbol or None)


def looks_like_compound_workflow(text: str) -> bool:
    """True when the utterance needs chained MCP tools rather than one keyword."""
    stripped = text.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered in EXIT_COMMANDS | HELP_COMMANDS | KILL_COMMANDS:
        return False
    if parse_history_limit(stripped) is not None:
        return False
    if parse_trade_intent(stripped) is not None:
        return False
    if parse_analyze_symbol(stripped) is not None:
        return False
    if FUTURES_BALANCE_PATTERN.match(stripped):
        return False
    if parse_positions_intent(stripped) is not None:
        return False
    if SIMPLE_BALANCE_PATTERN.match(stripped):
        return False

    has_balance = bool(BALANCE_PATTERN.search(stripped))
    has_analyze = bool(re.search(r"\banalyze\b", stripped, re.IGNORECASE))
    has_trade = bool(re.search(r"\b(buy|sell|order|prepare|preview)\b", stripped, re.IGNORECASE))
    has_percent = bool(PERCENT_OF_AVAILABLE_PATTERN.search(stripped))
    has_gate = bool(STABLE_GATE_PATTERN.search(stripped))
    has_and = bool(re.search(r"\b(and|then|,)\b", stripped, re.IGNORECASE))
    distinct = sum([has_balance, has_analyze, has_trade or has_percent])
    return distinct >= 2 or (has_percent and (has_trade or has_analyze)) or (has_gate and has_trade) or (
        has_and and distinct >= 2
    )


def parse_workflow_intent(text: str) -> WorkflowIntent | None:
    """Extract a multi-step Testnet plan. Submit is never encoded here."""
    stripped = text.strip()
    if not looks_like_compound_workflow(stripped):
        return None

    flags = {
        item.group(1).lower(): item.group(2)
        for item in FLAG_PATTERN.finditer(stripped)
    }
    stop_loss = flags.get("sl") or flags.get("stop-loss")
    take_profit = flags.get("tp") or flags.get("take-profit")

    check_futures_balance = bool(
        re.search(
            r"\bfutures\b.{0,40}\b(balance|wallet|account|portfolio)\b|"
            r"\b(balance|wallet|account|portfolio)\b.{0,40}\bfutures\b",
            stripped,
            re.IGNORECASE,
        )
    )
    check_spot_balance = bool(BALANCE_PATTERN.search(stripped)) and not check_futures_balance
    if re.search(r"\bspot\b.{0,40}\b(balance|wallet)\b|\b(balance|wallet)\b.{0,40}\bspot\b", stripped, re.IGNORECASE):
        check_spot_balance = True
    check_futures_positions = bool(
        re.search(r"\b(open\s+)?positions\b", stripped, re.IGNORECASE)
    )

    analyze_match = ANALYZE_IN_TEXT_PATTERN.search(stripped)
    analyze_symbol = (
        analyze_match.group(1).strip().upper().replace("-", "").replace("_", "").replace("/", "")
        if analyze_match
        else None
    )

    percent_match = PERCENT_OF_AVAILABLE_PATTERN.search(stripped)
    percent_of_available = percent_match.group(1) if percent_match else None
    percent_asset = (percent_match.group(2) or "USDT").upper() if percent_match else "USDT"

    trade_venue: str | None = None
    if re.search(r"\bfutures\s+(order|buy|sell)\b|\b(order|buy|sell).{0,20}\bfutures\b", stripped, re.IGNORECASE):
        trade_venue = "futures"
    elif re.search(r"\bspot\s+(order|buy|sell)\b|\b(order|buy|sell).{0,20}\bspot\b", stripped, re.IGNORECASE):
        trade_venue = "spot"
    elif percent_of_available or re.search(r"\b(prepare|preview|buy|sell)\b", stripped, re.IGNORECASE):
        trade_venue = "spot"

    trade_side: str | None = None
    if re.search(r"\bbuy\b", stripped, re.IGNORECASE):
        trade_side = "BUY"
    elif re.search(r"\bsell\b", stripped, re.IGNORECASE):
        trade_side = "SELL"

    trade_symbol = analyze_symbol
    if trade_symbol is None:
        pair = SPOT_PAIR_PATTERN.search(stripped.upper())
        if pair:
            trade_symbol = pair.group(1).upper()

    require_stable = bool(STABLE_GATE_PATTERN.search(stripped))

    intent = WorkflowIntent(
        check_spot_balance=check_spot_balance,
        check_futures_balance=check_futures_balance,
        check_futures_positions=check_futures_positions,
        analyze_symbol=analyze_symbol,
        require_stable=require_stable,
        trade_venue=trade_venue if trade_side else None,
        trade_side=trade_side,
        trade_symbol=trade_symbol if trade_side else None,
        percent_of_available=percent_of_available,
        percent_asset=percent_asset,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    if not any(
        [
            intent.check_spot_balance,
            intent.check_futures_balance,
            intent.check_futures_positions,
            intent.analyze_symbol,
            intent.trade_side,
        ]
    ):
        return None
    return intent
