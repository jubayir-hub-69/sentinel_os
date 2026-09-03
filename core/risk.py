"""Hardcoded position/size guardrails for SentinelOS (Track A).

These ceilings cannot be relaxed via environment variables. An order that
breaches either limit is blocked before it reaches Binance Testnet.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Final

from core.audit import audit

MAX_EXPOSURE_FRACTION: Final[Decimal] = Decimal("0.10")
MAX_NOTIONAL_USDT: Final[Decimal] = Decimal("1000")
STABLE_ASSETS: Final[frozenset[str]] = frozenset(
    {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"}
)


def parse_positive_decimal(value: object, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}.") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be greater than zero.")
    return parsed


def validate_protective_prices(
    *,
    side: str,
    last_price: Decimal,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
) -> None:
    """Reject SL/TP levels that would trigger immediately or contradict the side."""
    if last_price <= 0:
        raise ValueError("Last price must be greater than zero to validate SL/TP.")
    if stop_loss is not None and stop_loss <= 0:
        raise ValueError("Stop-loss price must be greater than zero.")
    if take_profit is not None and take_profit <= 0:
        raise ValueError("Take-profit price must be greater than zero.")
    if (
        stop_loss is not None
        and take_profit is not None
        and stop_loss == take_profit
    ):
        raise ValueError("Stop-loss and take-profit must be different prices.")

    if side == "BUY":
        if stop_loss is not None and stop_loss >= last_price:
            raise ValueError(
                f"BUY stop-loss {format(stop_loss, 'f')} must be strictly below "
                f"last price {format(last_price, 'f')}."
            )
        if take_profit is not None and take_profit <= last_price:
            raise ValueError(
                f"BUY take-profit {format(take_profit, 'f')} must be strictly above "
                f"last price {format(last_price, 'f')}."
            )
        return

    if stop_loss is not None and stop_loss <= last_price:
        raise ValueError(
            f"SELL stop-loss {format(stop_loss, 'f')} must be strictly above "
            f"last price {format(last_price, 'f')}."
        )
    if take_profit is not None and take_profit <= last_price:
        raise ValueError(
            f"SELL take-profit {format(take_profit, 'f')} must be strictly below "
            f"last price {format(last_price, 'f')}."
        )


def enforce_size_limits(
    *,
    notional_usdt: Decimal,
    portfolio_usdt: Decimal,
    symbol: str,
    venue: str,
) -> Decimal:
    """Block orders above 10% of portfolio or the $1000 hard ceiling.

    Returns the effective cap that was applied.
    """
    if notional_usdt <= 0:
        raise ValueError("Order notional must be greater than zero.")
    if portfolio_usdt <= 0:
        message = (
            "SECURITY WARNING: Cannot size a Testnet order against an empty portfolio. "
            "No order was sent."
        )
        audit(
            "RISK_BLOCK",
            message,
            symbol=symbol,
            venue=venue,
            notional_usdt=format(notional_usdt, "f"),
        )
        raise PermissionError(message)

    fraction_cap = portfolio_usdt * MAX_EXPOSURE_FRACTION
    effective_cap = min(fraction_cap, MAX_NOTIONAL_USDT)
    if notional_usdt > effective_cap:
        message = (
            "SECURITY WARNING: Order blocked by SentinelOS risk guardrail. "
            f"Notional {format(notional_usdt, 'f')} USDT exceeds the allowed ceiling "
            f"{format(effective_cap, 'f')} USDT "
            f"(10% of portfolio {format(portfolio_usdt, 'f')} USDT = "
            f"{format(fraction_cap, 'f')} USDT; hard cap "
            f"{format(MAX_NOTIONAL_USDT, 'f')} USDT). No order was sent."
        )
        audit(
            "RISK_BLOCK",
            message,
            symbol=symbol,
            venue=venue,
            notional_usdt=format(notional_usdt, "f"),
            portfolio_usdt=format(portfolio_usdt, "f"),
            fraction_cap_usdt=format(fraction_cap, "f"),
            hard_cap_usdt=format(MAX_NOTIONAL_USDT, "f"),
        )
        raise PermissionError(message)
    return effective_cap
