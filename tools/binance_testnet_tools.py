"""Official Binance Spot Testnet tools for SentinelOS (Track A).

All network calls go through ``binance-connector`` against
``https://testnet.binance.vision`` only. Production hosts are rejected
with RuntimeError. State-changing orders require ``human_confirmed=True``.
"""

from __future__ import annotations

import inspect
import logging
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Final, Mapping
from urllib.parse import urlparse

from binance.spot import Spot

from core.audit import audit
from core.config import SPOT_BASE_URL, Settings, get_settings
from core.kill_switch import get_kill_switch
from core.risk import (
    MAX_EXPOSURE_FRACTION,
    MAX_NOTIONAL_USDT,
    STABLE_ASSETS,
    enforce_size_limits,
    parse_positive_decimal,
    validate_protective_prices,
)
from policies.testnet_only import verify_testnet_environment

logger = logging.getLogger(__name__)

ALLOWED_SIDES: Final[frozenset[str]] = frozenset({"BUY", "SELL"})
QUOTE_ASSETS: Final[tuple[str, ...]] = (
    "USDT",
    "USDC",
    "FDUSD",
    "BUSD",
    "TUSD",
    "BTC",
    "ETH",
    "BNB",
    "EUR",
    "TRY",
)
_FILTER_CACHE: dict[str, dict[str, Decimal]] = {}


def create_spot_testnet_client() -> Spot:
    """Build a ``binance-connector`` Spot client pinned to Testnet credentials."""
    get_kill_switch().raise_if_tripped()
    settings = get_settings()
    _assert_testnet_settings(settings)
    api_key = settings.spot_api_key.get_secret_value()
    api_secret = settings.spot_api_secret.get_secret_value()
    client = Spot(**_spot_constructor_kwargs(api_key, api_secret, settings.spot_base_url))
    _assert_testnet_client(client, settings)
    audit("API_CALL", "Created Spot Testnet client.", venue="spot", host=settings.spot_base_url)
    return client


def get_spot_testnet_balance(client: Spot) -> dict[str, Any]:
    """Fetch Spot Testnet account balances. Read-only; no order is placed."""
    settings = _guard_spot_client(client)
    audit("API_CALL", "GET Spot Testnet account.", venue="spot", method="account")
    try:
        raw = _invoke(client, "account")
    except Exception as exc:
        audit("API_ERROR", "Spot account fetch failed.", venue="spot", error=str(exc))
        raise
    account = _as_mapping(raw)
    balances = _nonzero_balances(account.get("balances", []))
    return {
        "environment": "testnet",
        "venue": "spot",
        "base_url": settings.spot_base_url,
        "account_type": account.get("accountType", "SPOT"),
        "can_trade": bool(account.get("canTrade", False)),
        "can_withdraw": bool(account.get("canWithdraw", False)),
        "can_deposit": bool(account.get("canDeposit", False)),
        "update_time": account.get("updateTime"),
        "balances": balances,
        "raw_account": account,
    }


def get_spot_testnet_ticker(symbol: str, client: Spot | None = None) -> dict[str, Any]:
    """Fetch last price and 24h ticker from Spot Testnet (read-only)."""
    own_client = client is None
    if client is None:
        client = create_spot_testnet_client()
    try:
        settings = _guard_spot_client(client)
        normalized = _normalize_symbol(symbol)
        audit("API_CALL", "GET Spot Testnet ticker.", venue="spot", symbol=normalized)
        try:
            price_raw = _as_mapping(_invoke(client, "ticker_price", symbol=normalized))
            stats_raw = _invoke(client, "ticker_24hr", symbol=normalized)
            stats = _as_mapping(stats_raw) if not isinstance(stats_raw, list) else _as_mapping(stats_raw[0])
            klines: list[Any] = []
            try:
                kline_raw = _invoke(client, "klines", normalized, "1h", limit=12)
                if isinstance(kline_raw, list):
                    klines = kline_raw
            except Exception:
                klines = []
        except Exception as exc:
            audit("API_ERROR", "Spot ticker fetch failed.", venue="spot", symbol=normalized, error=str(exc))
            raise
        last_price = Decimal(str(price_raw.get("price") or stats.get("lastPrice") or "0"))
        hourly_closes = []
        for row in klines:
            if isinstance(row, (list, tuple)) and len(row) > 4:
                hourly_closes.append(str(row[4]))
        return {
            "environment": "testnet",
            "venue": "spot",
            "base_url": settings.spot_base_url,
            "symbol": normalized,
            "last_price": format(last_price, "f") if last_price > 0 else str(price_raw.get("price", "")),
            "price_change_percent": str(stats.get("priceChangePercent", "")),
            "open_price": str(stats.get("openPrice", "")),
            "high_price": str(stats.get("highPrice", "")),
            "low_price": str(stats.get("lowPrice", "")),
            "volume": str(stats.get("volume", "")),
            "quote_volume": str(stats.get("quoteVolume", "")),
            "weighted_avg_price": str(stats.get("weightedAvgPrice", "")),
            "bid_price": str(stats.get("bidPrice", "")),
            "ask_price": str(stats.get("askPrice", "")),
            "trade_count": stats.get("count"),
            "hourly_closes": hourly_closes,
        }
    finally:
        if own_client:
            _close_spot_session(client)


def preview_spot_testnet_order(
    symbol: str,
    side: str,
    quantity: Decimal | float | int | str,
    stop_loss: Decimal | float | int | str | None = None,
    take_profit: Decimal | float | int | str | None = None,
    client: Spot | None = None,
) -> dict[str, Any]:
    """Return a structured order preview. Does not contact the matching engine."""
    get_kill_switch().raise_if_tripped()
    settings = get_settings()
    _assert_testnet_settings(settings)
    normalized_symbol = _normalize_symbol(symbol)
    normalized_side = _normalize_side(side)
    normalized_quantity = _normalize_quantity(quantity)
    sl = _optional_price(stop_loss, "stop-loss")
    tp = _optional_price(take_profit, "take-profit")
    quote_asset = _quote_asset(normalized_symbol)

    own_client = client is None
    if client is None:
        client = create_spot_testnet_client()
    settings = _guard_spot_client(client)
    last_price = _last_price(client, normalized_symbol)
    if sl is not None or tp is not None:
        validate_protective_prices(
            side=normalized_side,
            last_price=last_price,
            stop_loss=sl,
            take_profit=tp,
        )
    risk = _enforce_exposure_limit(
        client,
        symbol=normalized_symbol,
        side=normalized_side,
        quantity=normalized_quantity,
    )
    if own_client:
        _close_spot_session(client)

    preview = {
        "environment": "testnet",
        "status": "order_preview",
        "venue": "spot",
        "base_url": SPOT_BASE_URL,
        "executed": False,
        "dry_run": True,
        "order": {
            "symbol": normalized_symbol,
            "side": normalized_side,
            "type": "MARKET",
            "quantity": _fmt(normalized_quantity),
            "quote_asset": quote_asset,
            "stop_loss": _fmt(sl) if sl is not None else None,
            "take_profit": _fmt(tp) if tp is not None else None,
            "estimated_last_price": _fmt(last_price),
            "estimated_notional": _fmt(normalized_quantity * last_price),
        },
        "risk": {
            "max_exposure_fraction": _fmt(MAX_EXPOSURE_FRACTION),
            "max_notional_usdt": _fmt(MAX_NOTIONAL_USDT),
            "effective_cap_usdt": _fmt(risk["effective_cap_usdt"]),
            "portfolio_usdt": _fmt(risk["portfolio_usdt"]),
            "notional_usdt": _fmt(risk["notional_usdt"]),
            "human_confirmation_required": True,
            "policy": "policies/execution_guardrails.md",
        },
        "next_step": (
            "Present this preview to the user. Submit only after explicit confirmation "
            "by calling submit_spot_testnet_order(..., human_confirmed=True)."
        ),
        "message": (
            "Preview only. No Testnet order was sent and no production host was contacted."
        ),
    }
    audit(
        "TRADE_PREVIEW",
        "Spot order preview built.",
        venue="spot",
        symbol=normalized_symbol,
        side=normalized_side,
        quantity=_fmt(normalized_quantity),
        stop_loss=_fmt(sl) if sl is not None else None,
        take_profit=_fmt(tp) if tp is not None else None,
    )
    return preview


def submit_spot_testnet_order(
    client: Spot,
    symbol: str,
    side: str,
    quantity: Decimal | float | int | str,
    human_confirmed: bool = False,
    stop_loss: Decimal | float | int | str | None = None,
    take_profit: Decimal | float | int | str | None = None,
) -> dict[str, Any]:
    """Submit a Spot Testnet MARKET order only after explicit human confirmation."""
    get_kill_switch().raise_if_tripped()
    if human_confirmed is not True:
        audit(
            "TRADE_REJECTED",
            "Spot submit denied: human_confirmed is not True.",
            venue="spot",
            symbol=symbol,
            side=side,
        )
        raise PermissionError(
            "submit_spot_testnet_order requires explicit human confirmation. "
            "human_confirmed=False is treated as denial. No order was sent."
        )

    settings = _guard_spot_client(client)
    normalized_symbol = _normalize_symbol(symbol)
    normalized_side = _normalize_side(side)
    normalized_quantity = _normalize_quantity(quantity)
    sl = _optional_price(stop_loss, "stop-loss")
    tp = _optional_price(take_profit, "take-profit")

    last_price = _last_price(client, normalized_symbol)
    if sl is not None or tp is not None:
        validate_protective_prices(
            side=normalized_side,
            last_price=last_price,
            stop_loss=sl,
            take_profit=tp,
        )
    _enforce_exposure_limit(
        client,
        symbol=normalized_symbol,
        side=normalized_side,
        quantity=normalized_quantity,
    )

    qty_str = _fmt(_quantize_quantity(client, normalized_symbol, normalized_quantity))
    logger.info(
        "Submitting Spot Testnet MARKET order symbol=%s side=%s quantity=%s sl=%s tp=%s",
        normalized_symbol,
        normalized_side,
        qty_str,
        _fmt(sl) if sl is not None else None,
        _fmt(tp) if tp is not None else None,
    )
    audit(
        "API_CALL",
        "POST Spot Testnet MARKET new_order.",
        venue="spot",
        symbol=normalized_symbol,
        side=normalized_side,
        quantity=qty_str,
    )
    try:
        raw = _invoke(
            client,
            "new_order",
            symbol=normalized_symbol,
            side=normalized_side,
            type="MARKET",
            quantity=qty_str,
        )
    except Exception as exc:
        audit(
            "API_ERROR",
            "Spot MARKET order failed.",
            venue="spot",
            symbol=normalized_symbol,
            error=str(exc),
        )
        raise
    exchange_response = _as_mapping(raw)
    filled_qty = _filled_quantity(exchange_response, Decimal(qty_str))

    protection: dict[str, Any] | None = None
    protection_error: str | None = None
    if sl is not None or tp is not None:
        try:
            protection = _place_spot_protection(
                client,
                symbol=normalized_symbol,
                entry_side=normalized_side,
                quantity=filled_qty,
                stop_loss=sl,
                take_profit=tp,
            )
        except Exception as exc:
            protection_error = str(exc)
            audit(
                "API_ERROR",
                "Spot SL/TP protection failed after entry fill.",
                venue="spot",
                symbol=normalized_symbol,
                error=protection_error,
            )

    audit(
        "TRADE_CONFIRMED",
        "Spot Testnet MARKET order submitted.",
        venue="spot",
        symbol=normalized_symbol,
        side=normalized_side,
        quantity=qty_str,
        order_id=exchange_response.get("orderId"),
        stop_loss=_fmt(sl) if sl is not None else None,
        take_profit=_fmt(tp) if tp is not None else None,
        protection_ok=protection_error is None,
    )
    return {
        "environment": "testnet",
        "status": "submitted",
        "venue": "spot",
        "base_url": settings.spot_base_url,
        "human_confirmed": True,
        "order": {
            "symbol": normalized_symbol,
            "side": normalized_side,
            "type": "MARKET",
            "quantity": qty_str,
            "stop_loss": _fmt(sl) if sl is not None else None,
            "take_profit": _fmt(tp) if tp is not None else None,
        },
        "exchange_response": exchange_response,
        "protection": protection,
        "protection_error": protection_error,
    }


def _place_spot_protection(
    client: Spot,
    *,
    symbol: str,
    entry_side: str,
    quantity: Decimal,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
) -> dict[str, Any]:
    """Attach OCO or standalone STOP_LOSS / TAKE_PROFIT after a filled MARKET entry."""
    protect_side = "SELL" if entry_side == "BUY" else "BUY"
    qty = _fmt(_quantize_quantity(client, symbol, quantity))
    sl_price = _quantize_price(client, symbol, stop_loss) if stop_loss is not None else None
    tp_price = _quantize_price(client, symbol, take_profit) if take_profit is not None else None
    attempts: list[dict[str, Any]] = []

    if sl_price is not None and tp_price is not None:
        oco = _try_spot_oco(
            client,
            symbol=symbol,
            side=protect_side,
            quantity=qty,
            stop_loss=sl_price,
            take_profit=tp_price,
        )
        if oco is not None and oco.get("response") is not None:
            return {
                "mode": oco["mode"],
                "exchange_response": oco["response"],
                "attempts": oco.get("attempts", []),
            }
        attempts.extend((oco or {}).get("attempts") or [])

    responses: dict[str, Any] = {}
    if sl_price is not None:
        responses["stop_loss"] = _place_spot_stop(
            client, symbol=symbol, side=protect_side, quantity=qty, stop_price=sl_price, kind="STOP_LOSS"
        )
    if tp_price is not None:
        responses["take_profit"] = _place_spot_stop(
            client, symbol=symbol, side=protect_side, quantity=qty, stop_price=tp_price, kind="TAKE_PROFIT"
        )
    return {"mode": "standalone_exits", "exchange_response": responses, "attempts": attempts}


def _try_spot_oco(
    client: Spot,
    *,
    symbol: str,
    side: str,
    quantity: str,
    stop_loss: Decimal,
    take_profit: Decimal,
) -> dict[str, Any] | None:
    sl_str = _fmt(stop_loss)
    tp_str = _fmt(take_profit)
    sl_limit = _fmt(_quantize_price(client, symbol, _stop_limit_price(side, stop_loss)))
    attempts: list[str] = []

    list_kwargs = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "aboveType": "TAKE_PROFIT",
        "belowType": "STOP_LOSS",
        "aboveStopPrice": tp_str if side == "SELL" else sl_str,
        "belowStopPrice": sl_str if side == "SELL" else tp_str,
    }
    if side == "BUY":
        list_kwargs["aboveType"] = "STOP_LOSS"
        list_kwargs["belowType"] = "TAKE_PROFIT"
        list_kwargs["aboveStopPrice"] = sl_str
        list_kwargs["belowStopPrice"] = tp_str

    for method_name, kwargs in (
        ("new_order_list_oco", list_kwargs),
        (
            "new_oco_order",
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": tp_str,
                "stopPrice": sl_str,
                "stopLimitPrice": sl_limit,
                "stopLimitTimeInForce": "GTC",
            },
        ),
    ):
        if not _has_method(client, method_name):
            attempts.append(f"{method_name}:not_available")
            continue
        try:
            audit("API_CALL", f"POST Spot {method_name}.", venue="spot", symbol=symbol, method=method_name)
            response = _as_mapping(_invoke(client, method_name, **kwargs))
            return {"mode": method_name, "response": response, "attempts": attempts}
        except Exception as exc:
            attempts.append(f"{method_name}:{exc}")
            continue
    return {"mode": None, "response": None, "attempts": attempts}


def _place_spot_stop(
    client: Spot,
    *,
    symbol: str,
    side: str,
    quantity: str,
    stop_price: Decimal,
    kind: str,
) -> dict[str, Any]:
    stop_str = _fmt(stop_price)
    limit_price = _fmt(_quantize_price(client, symbol, _stop_limit_price(side, stop_price)))
    errors: list[str] = []
    for order_type, extra in (
        (kind, {"stopPrice": stop_str}),
        (
            f"{kind}_LIMIT",
            {"stopPrice": stop_str, "price": limit_price, "timeInForce": "GTC"},
        ),
    ):
        try:
            audit(
                "API_CALL",
                f"POST Spot {order_type} protection.",
                venue="spot",
                symbol=symbol,
                side=side,
                type=order_type,
            )
            return _as_mapping(
                _invoke(
                    client,
                    "new_order",
                    symbol=symbol,
                    side=side,
                    type=order_type,
                    quantity=quantity,
                    **extra,
                )
            )
        except Exception as exc:
            errors.append(f"{order_type}: {exc}")
            continue
    raise RuntimeError(
        f"Unable to place Spot {kind} protection on Testnet ({'; '.join(errors)})."
    )


def _stop_limit_price(side: str, stop_price: Decimal) -> Decimal:
    buffer = Decimal("0.0015")
    if side == "SELL":
        return stop_price * (Decimal("1") - buffer)
    return stop_price * (Decimal("1") + buffer)


def _spot_constructor_kwargs(api_key: str, api_secret: str, base_url: str) -> dict[str, str]:
    kwargs: dict[str, str] = {"base_url": base_url}
    parameters = inspect.signature(Spot.__init__).parameters
    if "api_key" in parameters:
        kwargs["api_key"] = api_key
        kwargs["api_secret"] = api_secret
    elif "key" in parameters:
        kwargs["key"] = api_key
        kwargs["secret"] = api_secret
    else:
        raise RuntimeError(
            "Unsupported binance-connector Spot constructor. "
            "Expected api_key/api_secret or key/secret."
        )
    return kwargs


def _assert_testnet_settings(settings: Settings) -> None:
    if settings.binance_env != "testnet":
        raise RuntimeError(
            "BINANCE_ENV must equal 'testnet'. SentinelOS refuses production operation."
        )
    settings.require_testnet_endpoint(settings.spot_base_url)
    verify_testnet_environment(settings.spot_base_url)
    if urlparse(settings.spot_base_url).hostname != "testnet.binance.vision":
        raise RuntimeError("Blocked non-Testnet Binance host.")


def _client_base_url(client: Spot) -> str:
    for attr in ("base_url", "baseUrl"):
        value = getattr(client, attr, None)
        if value:
            return str(value).rstrip("/")
    config = getattr(client, "config_rest_api", None)
    if config is not None:
        for attr in ("base_path", "base_url", "baseUrl"):
            value = getattr(config, attr, None)
            if value:
                return str(value).rstrip("/")
    raise RuntimeError(
        "Spot client has no base_url. SentinelOS will not send requests to an unknown host."
    )


def _assert_testnet_client(client: Spot, settings: Settings) -> None:
    if client is None:
        raise RuntimeError("A binance-connector Spot client is required.")
    base_url = _client_base_url(client)
    settings.require_testnet_endpoint(base_url)
    verify_testnet_environment(base_url)
    host = (urlparse(base_url).hostname or "").lower()
    if host != "testnet.binance.vision":
        raise RuntimeError("Blocked non-Testnet Binance host.")


def _guard_spot_client(client: Spot) -> Settings:
    get_kill_switch().raise_if_tripped()
    settings = get_settings()
    _assert_testnet_settings(settings)
    _assert_testnet_client(client, settings)
    return settings


def _has_method(client: Spot, method_name: str) -> bool:
    method = getattr(client, method_name, None)
    if callable(method):
        return True
    rest_api = getattr(client, "rest_api", None)
    return callable(getattr(rest_api, method_name, None)) if rest_api is not None else False


def _invoke(client: Spot, method_name: str, *args: Any, **kwargs: Any) -> Any:
    get_kill_switch().raise_if_tripped()
    method = getattr(client, method_name, None)
    if not callable(method):
        rest_api = getattr(client, "rest_api", None)
        method = getattr(rest_api, method_name, None) if rest_api is not None else None
    if not callable(method):
        raise RuntimeError(
            f"binance-connector Spot client does not expose {method_name}()."
        )
    response = method(*args, **kwargs) if (args or kwargs) else method()
    if hasattr(response, "data") and callable(response.data):
        return response.data()
    return response


def _as_mapping(payload: object) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if hasattr(payload, "to_dict") and callable(payload.to_dict):
        data = payload.to_dict()
        if isinstance(data, dict):
            return data
    if hasattr(payload, "model_dump") and callable(payload.model_dump):
        data = payload.model_dump()
        if isinstance(data, dict):
            return data
    raise TypeError(f"Expected a JSON object from Binance Testnet, got {type(payload)!r}.")


def _normalize_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().upper().replace("-", "").replace("_", "").replace("/", "")
    if not normalized.isalnum() or len(normalized) < 5:
        raise ValueError(f"Invalid Spot symbol: {symbol!r}.")
    return normalized


def _normalize_side(side: str) -> str:
    normalized = str(side).strip().upper()
    if normalized not in ALLOWED_SIDES:
        raise ValueError(f"Order side must be BUY or SELL, got {side!r}.")
    return normalized


def _normalize_quantity(quantity: Decimal | float | int | str) -> Decimal:
    return parse_positive_decimal(quantity, field="order quantity").normalize()


def _optional_price(value: object, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return parse_positive_decimal(value, field=field)


def _fmt(value: Decimal) -> str:
    return format(value, "f")


def _quote_asset(symbol: str) -> str:
    for quote in QUOTE_ASSETS:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return quote
    raise ValueError(
        f"Unable to determine quote asset for {symbol!r}. "
        f"Supported quotes: {', '.join(QUOTE_ASSETS)}."
    )


def _base_asset(symbol: str, quote_asset: str) -> str:
    return symbol[: -len(quote_asset)]


def _balance_map(balances: object) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    if not isinstance(balances, list):
        return result
    for item in balances:
        row = _as_mapping(item) if not isinstance(item, Mapping) else dict(item)
        asset = str(row.get("asset", "")).upper()
        if not asset:
            continue
        free = Decimal(str(row.get("free", "0") or "0"))
        locked = Decimal(str(row.get("locked", "0") or "0"))
        result[asset] = free + locked
    return result


def _nonzero_balances(balances: object) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not isinstance(balances, list):
        return rows
    for item in balances:
        row = _as_mapping(item) if not isinstance(item, Mapping) else dict(item)
        asset = str(row.get("asset", "")).upper()
        free = Decimal(str(row.get("free", "0") or "0"))
        locked = Decimal(str(row.get("locked", "0") or "0"))
        if free == 0 and locked == 0:
            continue
        rows.append(
            {
                "asset": asset,
                "free": format(free, "f"),
                "locked": format(locked, "f"),
                "total": format(free + locked, "f"),
            }
        )
    return rows


def _last_price(client: Spot, symbol: str) -> Decimal:
    raw = _invoke(client, "ticker_price", symbol=symbol)
    payload = _as_mapping(raw)
    price = Decimal(str(payload.get("price", "0") or "0"))
    if price <= 0:
        raise RuntimeError(f"Invalid Testnet ticker price for {symbol}.")
    return price


def _to_usdt(client: Spot, asset: str, amount: Decimal) -> Decimal:
    if amount == 0:
        return Decimal("0")
    if asset in STABLE_ASSETS:
        return amount
    pair = f"{asset}USDT"
    try:
        return amount * _last_price(client, pair)
    except Exception as exc:
        raise PermissionError(
            f"SECURITY WARNING: Cannot value {asset} in USDT for the "
            f"{_fmt(MAX_NOTIONAL_USDT)} USDT risk ceiling. No order was sent."
        ) from exc


def _portfolio_quote_value(
    balances: dict[str, Decimal],
    base_asset: str,
    quote_asset: str,
    last_price: Decimal,
) -> Decimal:
    quote_value = balances.get(quote_asset, Decimal("0"))
    quote_value += balances.get(base_asset, Decimal("0")) * last_price
    if quote_asset in STABLE_ASSETS:
        for stable in STABLE_ASSETS:
            if stable != quote_asset:
                quote_value += balances.get(stable, Decimal("0"))
    return quote_value


def _symbol_filters(client: Spot, symbol: str) -> dict[str, Decimal]:
    cached = _FILTER_CACHE.get(symbol)
    if cached is not None:
        return cached
    raw = _invoke(client, "exchange_info", symbol=symbol)
    payload = _as_mapping(raw)
    symbols = payload.get("symbols") or []
    tick = Decimal("0.00000001")
    step = Decimal("0.00000001")
    for item in symbols:
        row = item if isinstance(item, Mapping) else _as_mapping(item)
        if str(row.get("symbol", "")).upper() != symbol:
            continue
        for filt in row.get("filters") or []:
            spec = filt if isinstance(filt, Mapping) else _as_mapping(filt)
            kind = str(spec.get("filterType", ""))
            if kind == "PRICE_FILTER":
                tick = Decimal(str(spec.get("tickSize") or tick))
            elif kind in {"LOT_SIZE", "MARKET_LOT_SIZE"}:
                step = Decimal(str(spec.get("stepSize") or step))
        break
    filters = {"tick_size": tick if tick > 0 else Decimal("0.00000001"), "step_size": step if step > 0 else Decimal("0.00000001")}
    _FILTER_CACHE[symbol] = filters
    return filters


def _quantize(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    quantized = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
    exponent = max(0, -step.as_tuple().exponent)
    return quantized.quantize(Decimal("1").scaleb(-exponent))


def _quantize_price(client: Spot, symbol: str, price: Decimal) -> Decimal:
    return _quantize(price, _symbol_filters(client, symbol)["tick_size"])


def _quantize_quantity(client: Spot, symbol: str, quantity: Decimal) -> Decimal:
    stepped = _quantize(quantity, _symbol_filters(client, symbol)["step_size"])
    if stepped <= 0:
        raise ValueError(f"Quantity {format(quantity, 'f')} is below the LOT_SIZE step for {symbol}.")
    return stepped


def _filled_quantity(exchange_response: Mapping[str, Any], fallback: Decimal) -> Decimal:
    for key in ("executedQty", "origQty", "executed_qty"):
        raw = exchange_response.get(key)
        if raw is None:
            continue
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            continue
        if value > 0:
            return value
    return fallback


def _close_spot_session(client: Spot) -> None:
    session = getattr(client, "session", None)
    closer = getattr(session, "close", None) if session is not None else None
    if callable(closer):
        try:
            closer()
        except Exception:
            return


def _enforce_exposure_limit(
    client: Spot,
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
) -> dict[str, Decimal]:
    quote_asset = _quote_asset(symbol)
    base_asset = _base_asset(symbol, quote_asset)
    last_price = _last_price(client, symbol)
    notional = quantity * last_price
    account = _as_mapping(_invoke(client, "account"))
    balances = _balance_map(account.get("balances", []))
    portfolio = _portfolio_quote_value(balances, base_asset, quote_asset, last_price)
    portfolio_usdt = _to_usdt(client, quote_asset, portfolio)
    notional_usdt = _to_usdt(client, quote_asset, notional)
    effective_cap = enforce_size_limits(
        notional_usdt=notional_usdt,
        portfolio_usdt=portfolio_usdt,
        symbol=symbol,
        venue="spot",
    )
    if side == "SELL" and quantity > balances.get(base_asset, Decimal("0")):
        raise PermissionError(
            f"Insufficient Testnet {base_asset} balance to SELL "
            f"{format(quantity, 'f')}. No order was sent."
        )
    if side == "BUY" and notional > balances.get(quote_asset, Decimal("0")):
        raise PermissionError(
            f"Insufficient Testnet {quote_asset} balance to BUY "
            f"{format(quantity, 'f')} {base_asset}. No order was sent."
        )
    return {
        "effective_cap_usdt": effective_cap,
        "portfolio_usdt": portfolio_usdt,
        "notional_usdt": notional_usdt,
    }
