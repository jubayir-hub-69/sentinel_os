"""Unit tests for fail-closed parser, risk ceilings, and audit logging."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cli_parse import parse_analyze_symbol, parse_history_limit, parse_trade_intent
from core.risk import (
    MAX_EXPOSURE_FRACTION,
    MAX_NOTIONAL_USDT,
    enforce_size_limits,
    validate_protective_prices,
)


def test_parse_spot_sl_tp() -> None:
    intent = parse_trade_intent("buy BTCUSDT 0.001 --sl 58000 --tp 62000")
    assert intent is not None
    assert intent.venue == "spot"
    assert intent.side == "BUY"
    assert intent.symbol == "BTCUSDT"
    assert intent.quantity == "0.001"
    assert intent.stop_loss == "58000"
    assert intent.take_profit == "62000"


def test_parse_futures_flags_reversed() -> None:
    intent = parse_trade_intent("futures sell ETHUSDT 0.01 --tp 2800 --sl 3500")
    assert intent is not None
    assert intent.venue == "futures"
    assert intent.side == "SELL"
    assert intent.stop_loss == "3500"
    assert intent.take_profit == "2800"


def test_parse_rejects_trailing_junk() -> None:
    assert parse_trade_intent("buy BTCUSDT 0.001 leverage 20") is None


def test_parse_analyze_and_history() -> None:
    assert parse_analyze_symbol("analyze BTCUSDT") == "BTCUSDT"
    assert parse_history_limit("history") == 40
    assert parse_history_limit("history 12") == 12


def test_size_limit_hard_cap() -> None:
    try:
        enforce_size_limits(
            notional_usdt=Decimal("1000.01"),
            portfolio_usdt=Decimal("50000"),
            symbol="BTCUSDT",
            venue="spot",
        )
        raise AssertionError("expected PermissionError")
    except PermissionError as exc:
        assert "SECURITY WARNING" in str(exc)


def test_size_limit_ten_percent() -> None:
    try:
        enforce_size_limits(
            notional_usdt=Decimal("250"),
            portfolio_usdt=Decimal("2000"),
            symbol="BTCUSDT",
            venue="futures",
        )
        raise AssertionError("expected PermissionError")
    except PermissionError as exc:
        assert "SECURITY WARNING" in str(exc)


def test_size_limit_allows_within_cap() -> None:
    cap = enforce_size_limits(
        notional_usdt=Decimal("50"),
        portfolio_usdt=Decimal("2000"),
        symbol="BTCUSDT",
        venue="spot",
    )
    assert cap == Decimal("200")  # 10% of 2000 is below the $1000 hard cap
    assert MAX_EXPOSURE_FRACTION == Decimal("0.10")
    assert MAX_NOTIONAL_USDT == Decimal("1000")


def test_sl_tp_buy_rules() -> None:
    validate_protective_prices(
        side="BUY",
        last_price=Decimal("60000"),
        stop_loss=Decimal("58000"),
        take_profit=Decimal("62000"),
    )
    try:
        validate_protective_prices(
            side="BUY",
            last_price=Decimal("60000"),
            stop_loss=Decimal("61000"),
            take_profit=None,
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_select_flash_models_from_catalog() -> None:
    from types import SimpleNamespace

    from tools.market_analysis import select_flash_models_from_catalog

    picked = select_flash_models_from_catalog(
        [
            SimpleNamespace(name="models/gemini-embedding-001", supported_actions=["embedContent"]),
            SimpleNamespace(
                name="models/gemini-2.0-flash",
                supported_actions=["generateContent", "countTokens"],
            ),
            SimpleNamespace(
                name="models/gemini-1.5-flash-latest",
                supported_actions=["generateContent"],
            ),
            SimpleNamespace(name="models/imagen-3.0-generate", supported_actions=["generateContent"]),
            SimpleNamespace(name="models/gemini-pro", supported_actions=["generateContent"]),
        ]
    )
    assert picked[0] == "models/gemini-1.5-flash-latest"
    assert "models/gemini-2.0-flash" in picked
    assert all("embed" not in name and "imagen" not in name for name in picked)
    assert all("pro" not in name.split("flash")[0] or "flash" in name for name in picked)


if __name__ == "__main__":
    tests = [
        test_parse_spot_sl_tp,
        test_parse_futures_flags_reversed,
        test_parse_rejects_trailing_junk,
        test_parse_analyze_and_history,
        test_size_limit_hard_cap,
        test_size_limit_ten_percent,
        test_size_limit_allows_within_cap,
        test_sl_tp_buy_rules,
        test_select_flash_models_from_catalog,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} passed")
