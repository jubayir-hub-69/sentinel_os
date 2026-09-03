"""Binance USDⓈ-M Futures Testnet tools for SentinelOS (Track A).

Signed REST calls go only to ``https://testnet.binancefuture.com``. A custom
HMAC client is used so the Spot ``binance-connector`` package is not
overwritten by a conflicting ``binance`` namespace. Production hosts are
rejected with RuntimeError. State-changing orders require
``human_confirmed=True``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Final, Mapping
from urllib.parse import urlencode, urlparse

import httpx

from core.audit import audit
from core.config import FUTURES_BASE_URL, Settings, get_settings
from core.kill_switch import get_kill_switch
from core.risk import (
    MAX_EXPOSURE_FRACTION,
    MAX_NOTIONAL_USDT,
    enforce_size_limits,
    parse_positive_decimal,
    validate_protective_prices,
)
from policies.testnet_only import verify_testnet_environment

logger = logging.getLogger(__name__)

ALLOWED_SIDES: Final[frozenset[str]] = frozenset({"BUY", "SELL"})
_ALLOWED_HOST: Final[str] = "testnet.binancefuture.com"
_RECV_WINDOW: Final[int] = 5000
_FILTER_CACHE: dict[str, dict[str, Decimal]] = {}


class FuturesAPIError(RuntimeError):
    """Binance Futures Testnet API error (fail-closed)."""

    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload
        if isinstance(payload, Mapping):
            self.code = payload.get("code")
            self.msg = str(payload.get("msg", payload))
        else:
            self.code = None
            self.msg = str(payload)
        super().__init__(
            f"Futures Testnet API error HTTP {status} code={self.code}: {self.msg}"
        )


class FuturesTestnetClient:
    """HMAC-signed USDⓈ-M Futures client pinned to the official Testnet host."""

    def __init__(self, settings: Settings | None = None) -> None:
        get_kill_switch().raise_if_tripped()
        self.settings = settings or get_settings()
        _assert_futures_settings(self.settings)
        self.base_url = self.settings.futures_base_url.rstrip("/")
        self._api_key = self.settings.futures_api_key.get_secret_value()
        self._api_secret = self.settings.futures_api_secret.get_secret_value()
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(15.0),
            follow_redirects=False,
            headers={"X-MBX-APIKEY": self._api_key, "Accept": "application/json"},
        )
        self._assert_host()
        audit("API_CALL", "Created Futures Testnet client.", venue="futures", host=self.base_url)

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            return

    def _assert_host(self) -> None:
        host = (urlparse(str(self._http.base_url)).hostname or "").lower().rstrip(".")
        if host != _ALLOWED_HOST:
            raise RuntimeError("Blocked non-Testnet Binance host.")
        self.settings.require_testnet_endpoint(self.base_url)
        verify_testnet_environment(self.base_url)

    def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        get_kill_switch().raise_if_tripped()
        self._assert_host()
        payload: dict[str, str] = {}
        if params:
            for key, value in params.items():
                if value is None:
                    continue
                payload[key] = _stringify(value)
        if signed:
            payload["timestamp"] = str(int(time.time() * 1000))
            payload["recvWindow"] = str(_RECV_WINDOW)
            payload["signature"] = _sign(payload, self._api_secret)

        audit(
            "API_CALL",
            f"{method.upper()} {path}",
            venue="futures",
            method=method.upper(),
            path=path,
            symbol=payload.get("symbol"),
        )
        try:
            response = self._http.request(method.upper(), path, params=payload)
        except httpx.HTTPError as exc:
            audit("API_ERROR", "Futures HTTP failure.", venue="futures", path=path, error=str(exc))
            raise RuntimeError(f"Futures Testnet request failed: {exc}") from exc

        body: object
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = response.text

        if response.status_code >= 400:
            audit("API_ERROR", "Futures API HTTP error.", venue="futures", path=path, error=str(body))
            raise FuturesAPIError(response.status_code, body)
        if isinstance(body, Mapping):
            code = body.get("code")
            try:
                numeric = int(code) if code is not None else 0
            except (TypeError, ValueError):
                numeric = 0
            if numeric < 0:
                audit("API_ERROR", "Futures API business error.", venue="futures", path=path, error=str(body))
                raise FuturesAPIError(response.status_code, body)
        return body

    def public(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.request(method, path, params=params, signed=False)

    def signed(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.request(method, path, params=params, signed=True)


def create_futures_testnet_client() -> FuturesTestnetClient:
    """Build a Futures Testnet client pinned to Testnet credentials and host."""
    return FuturesTestnetClient()


def get_futures_testnet_balance(client: FuturesTestnetClient) -> dict[str, Any]:
    """Fetch USDⓈ-M Futures Testnet wallet balances. Read-only."""
    _guard_futures_client(client)
    account = _as_mapping(_account(client))
    assets = _nonzero_futures_assets(account.get("assets", []))
    return {
        "environment": "testnet",
        "venue": "futures",
        "base_url": client.base_url,
        "total_wallet_balance": str(account.get("totalWalletBalance", account.get("totalMarginBalance", "0"))),
        "available_balance": str(account.get("availableBalance", "0")),
        "total_unrealized_profit": str(account.get("totalUnrealizedProfit", "0")),
        "assets": assets,
        "can_trade": bool(account.get("canTrade", True)),
        "raw_account": account,
    }


def get_futures_testnet_ticker(symbol: str, client: FuturesTestnetClient | None = None) -> dict[str, Any]:
    """Fetch last price and 24h ticker from Futures Testnet (read-only)."""
    own = client is None
    if client is None:
        client = create_futures_testnet_client()
    try:
        _guard_futures_client(client)
        normalized = _normalize_symbol(symbol)
        price_raw = _as_mapping(client.public("GET", "/fapi/v1/ticker/price", {"symbol": normalized}))
        stats_raw = client.public("GET", "/fapi/v1/ticker/24hr", {"symbol": normalized})
        stats = _as_mapping(stats_raw if not isinstance(stats_raw, list) else stats_raw[0])
        last_price = Decimal(str(price_raw.get("price") or stats.get("lastPrice") or "0"))
        return {
            "environment": "testnet",
            "venue": "futures",
            "base_url": client.base_url,
            "symbol": normalized,
            "last_price": _fmt(last_price) if last_price > 0 else str(price_raw.get("price", "")),
            "price_change_percent": str(stats.get("priceChangePercent", "")),
            "open_price": str(stats.get("openPrice", "")),
            "high_price": str(stats.get("highPrice", "")),
            "low_price": str(stats.get("lowPrice", "")),
            "volume": str(stats.get("volume", "")),
            "quote_volume": str(stats.get("quoteVolume", "")),
            "weighted_avg_price": str(stats.get("weightedAvgPrice", "")),
        }
    finally:
        if own:
            client.close()


def preview_futures_testnet_order(
    symbol: str,
    side: str,
    quantity: Decimal | float | int | str,
    stop_loss: Decimal | float | int | str | None = None,
    take_profit: Decimal | float | int | str | None = None,
    client: FuturesTestnetClient | None = None,
) -> dict[str, Any]:
    """Return a structured Futures order preview. Does not place an order."""
    get_kill_switch().raise_if_tripped()
    own = client is None
    if client is None:
        client = create_futures_testnet_client()
    try:
        _guard_futures_client(client)
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
        risk = _enforce_futures_exposure(
            client,
            symbol=normalized_symbol,
            quantity=normalized_quantity,
            last_price=last_price,
        )
        preview = {
            "environment": "testnet",
            "status": "order_preview",
            "venue": "futures",
            "base_url": FUTURES_BASE_URL,
            "executed": False,
            "dry_run": True,
            "order": {
                "symbol": normalized_symbol,
                "side": normalized_side,
                "type": "MARKET",
                "quantity": _fmt(normalized_quantity),
                "quote_asset": "USDT",
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
                "available_balance": _fmt(risk["available_balance"]),
                "human_confirmation_required": True,
                "policy": "policies/execution_guardrails.md",
            },
            "next_step": (
                "Present this preview to the user. Submit only after explicit confirmation "
                "by calling submit_futures_testnet_order(..., human_confirmed=True)."
            ),
            "message": (
                "Preview only. No Testnet order was sent and no production host was contacted."
            ),
        }
        audit(
            "TRADE_PREVIEW",
            "Futures order preview built.",
            venue="futures",
            symbol=normalized_symbol,
            side=normalized_side,
            quantity=_fmt(normalized_quantity),
            stop_loss=_fmt(sl) if sl is not None else None,
            take_profit=_fmt(tp) if tp is not None else None,
        )
        return preview
    finally:
        if own:
            client.close()


def submit_futures_testnet_order(
    client: FuturesTestnetClient,
    symbol: str,
    side: str,
    quantity: Decimal | float | int | str,
    human_confirmed: bool = False,
    stop_loss: Decimal | float | int | str | None = None,
    take_profit: Decimal | float | int | str | None = None,
) -> dict[str, Any]:
    """Submit a USDⓈ-M Futures Testnet MARKET order only after HITL confirmation."""
    get_kill_switch().raise_if_tripped()
    if human_confirmed is not True:
        audit(
            "TRADE_REJECTED",
            "Futures submit denied: human_confirmed is not True.",
            venue="futures",
            symbol=symbol,
            side=side,
        )
        raise PermissionError(
            "submit_futures_testnet_order requires explicit human confirmation. "
            "human_confirmed=False is treated as denial. No order was sent."
        )

    _guard_futures_client(client)
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
    _enforce_futures_exposure(
        client,
        symbol=normalized_symbol,
        quantity=normalized_quantity,
        last_price=last_price,
    )

    qty = _quantize_quantity(client, normalized_symbol, normalized_quantity)
    order_params: dict[str, Any] = {
        "symbol": normalized_symbol,
        "side": normalized_side,
        "type": "MARKET",
        "quantity": _fmt(qty),
        "newOrderRespType": "RESULT",
    }
    position_side = _position_side(client, normalized_side)
    if position_side is not None:
        order_params["positionSide"] = position_side

    logger.info(
        "Submitting Futures Testnet MARKET order symbol=%s side=%s quantity=%s",
        normalized_symbol,
        normalized_side,
        _fmt(qty),
    )
    try:
        exchange_response = _as_mapping(client.signed("POST", "/fapi/v1/order", order_params))
    except Exception as exc:
        audit(
            "API_ERROR",
            "Futures MARKET order failed.",
            venue="futures",
            symbol=normalized_symbol,
            error=str(exc),
        )
        raise

    filled_qty = _filled_quantity(exchange_response, qty)
    protection: dict[str, Any] | None = None
    protection_error: str | None = None
    if sl is not None or tp is not None:
        try:
            protection = _place_futures_protection(
                client,
                symbol=normalized_symbol,
                entry_side=normalized_side,
                quantity=filled_qty,
                stop_loss=sl,
                take_profit=tp,
                position_side=position_side,
            )
        except Exception as exc:
            protection_error = str(exc)
            audit(
                "API_ERROR",
                "Futures SL/TP protection failed after entry fill.",
                venue="futures",
                symbol=normalized_symbol,
                error=protection_error,
            )

    audit(
        "TRADE_CONFIRMED",
        "Futures Testnet MARKET order submitted.",
        venue="futures",
        symbol=normalized_symbol,
        side=normalized_side,
        quantity=_fmt(qty),
        order_id=exchange_response.get("orderId"),
        stop_loss=_fmt(sl) if sl is not None else None,
        take_profit=_fmt(tp) if tp is not None else None,
        protection_ok=protection_error is None,
    )
    return {
        "environment": "testnet",
        "status": "submitted",
        "venue": "futures",
        "base_url": client.settings.futures_base_url,
        "human_confirmed": True,
        "order": {
            "symbol": normalized_symbol,
            "side": normalized_side,
            "type": "MARKET",
            "quantity": _fmt(qty),
            "stop_loss": _fmt(sl) if sl is not None else None,
            "take_profit": _fmt(tp) if tp is not None else None,
            "position_side": position_side,
        },
        "exchange_response": exchange_response,
        "protection": protection,
        "protection_error": protection_error,
    }


def _place_futures_protection(
    client: FuturesTestnetClient,
    *,
    symbol: str,
    entry_side: str,
    quantity: Decimal,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
    position_side: str | None,
) -> dict[str, Any]:
    protect_side = "SELL" if entry_side == "BUY" else "BUY"
    qty = _fmt(_quantize_quantity(client, symbol, quantity))
    placed: dict[str, Any] = {}

    def _conditional(kind: str, stop_price: Decimal) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": protect_side,
            "type": kind,
            "stopPrice": _fmt(_quantize_price(client, symbol, stop_price)),
            "quantity": qty,
            "workingType": "CONTRACT_PRICE",
            "reduceOnly": "true",
        }
        if position_side is not None:
            params["positionSide"] = position_side
            params.pop("reduceOnly", None)
        try:
            return _as_mapping(client.signed("POST", "/fapi/v1/order", params))
        except FuturesAPIError:
            params.pop("reduceOnly", None)
            return _as_mapping(client.signed("POST", "/fapi/v1/order", params))

    if stop_loss is not None:
        placed["stop_loss"] = _conditional("STOP_MARKET", stop_loss)
    if take_profit is not None:
        placed["take_profit"] = _conditional("TAKE_PROFIT_MARKET", take_profit)
    return {"mode": "reduce_only_exits", "exchange_response": placed}


def _assert_futures_settings(settings: Settings) -> None:
    if settings.binance_env != "testnet":
        raise RuntimeError(
            "BINANCE_ENV must equal 'testnet'. SentinelOS refuses production operation."
        )
    settings.require_testnet_endpoint(settings.futures_base_url)
    verify_testnet_environment(settings.futures_base_url)
    host = (urlparse(settings.futures_base_url).hostname or "").lower()
    if host != _ALLOWED_HOST:
        raise RuntimeError("Blocked non-Testnet Binance host.")


def _guard_futures_client(client: FuturesTestnetClient) -> Settings:
    get_kill_switch().raise_if_tripped()
    if client is None:
        raise RuntimeError("A Futures Testnet client is required.")
    settings = get_settings()
    _assert_futures_settings(settings)
    client._assert_host()
    return settings


def _sign(params: Mapping[str, str], secret: str) -> str:
    query = urlencode(params, doseq=True)
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _as_mapping(payload: object) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    raise TypeError(f"Expected a JSON object from Futures Testnet, got {type(payload)!r}.")


def _normalize_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().upper().replace("-", "").replace("_", "").replace("/", "")
    if not normalized.isalnum() or len(normalized) < 5:
        raise ValueError(f"Invalid Futures symbol: {symbol!r}.")
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


def _last_price(client: FuturesTestnetClient, symbol: str) -> Decimal:
    payload = _as_mapping(client.public("GET", "/fapi/v1/ticker/price", {"symbol": symbol}))
    price = Decimal(str(payload.get("price", "0") or "0"))
    if price <= 0:
        raise RuntimeError(f"Invalid Futures Testnet ticker price for {symbol}.")
    return price


def _account(client: FuturesTestnetClient) -> dict[str, Any]:
    last_error: Exception | None = None
    for path in ("/fapi/v2/account", "/fapi/v3/account", "/fapi/v1/account"):
        try:
            return _as_mapping(client.signed("GET", path))
        except FuturesAPIError as exc:
            last_error = exc
            if exc.status in {404, 405} or exc.code in {-4046, -1121}:
                continue
            if exc.status == 404:
                continue
            raise
    if last_error is not None:
        raise last_error
    raise RuntimeError("Unable to load Futures Testnet account.")


def _wallet_usdt(account: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    total = Decimal(str(account.get("totalWalletBalance") or account.get("totalMarginBalance") or "0"))
    available = Decimal(str(account.get("availableBalance") or "0"))
    if total == 0:
        for item in account.get("assets") or []:
            row = item if isinstance(item, Mapping) else {}
            if str(row.get("asset", "")).upper() != "USDT":
                continue
            total = Decimal(str(row.get("walletBalance") or row.get("marginBalance") or "0"))
            available = Decimal(str(row.get("availableBalance") or available))
            break
    return total, available


def _nonzero_futures_assets(assets: object) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not isinstance(assets, list):
        return rows
    for item in assets:
        row = item if isinstance(item, Mapping) else {}
        asset = str(row.get("asset", "")).upper()
        wallet = Decimal(str(row.get("walletBalance") or "0"))
        available = Decimal(str(row.get("availableBalance") or "0"))
        if wallet == 0 and available == 0:
            continue
        rows.append(
            {
                "asset": asset,
                "wallet_balance": format(wallet, "f"),
                "available_balance": format(available, "f"),
                "unrealized_profit": str(row.get("unrealizedProfit", "0")),
            }
        )
    return rows


def _position_side(client: FuturesTestnetClient, side: str) -> str | None:
    try:
        payload = _as_mapping(client.signed("GET", "/fapi/v1/positionSide/dual"))
    except Exception:
        return None
    dual = payload.get("dualSidePosition")
    if str(dual).lower() in {"true", "1"} or dual is True:
        return "LONG" if side == "BUY" else "SHORT"
    return None


def _symbol_filters(client: FuturesTestnetClient, symbol: str) -> dict[str, Decimal]:
    cached = _FILTER_CACHE.get(symbol)
    if cached is not None:
        return cached
    payload = _as_mapping(client.public("GET", "/fapi/v1/exchangeInfo"))
    tick = Decimal("0.01")
    step = Decimal("0.001")
    for item in payload.get("symbols") or []:
        row = item if isinstance(item, Mapping) else {}
        if str(row.get("symbol", "")).upper() != symbol:
            continue
        for filt in row.get("filters") or []:
            spec = filt if isinstance(filt, Mapping) else {}
            kind = str(spec.get("filterType", ""))
            if kind == "PRICE_FILTER":
                tick = Decimal(str(spec.get("tickSize") or tick))
            elif kind in {"LOT_SIZE", "MARKET_LOT_SIZE"}:
                step = Decimal(str(spec.get("stepSize") or step))
        break
    filters = {
        "tick_size": tick if tick > 0 else Decimal("0.01"),
        "step_size": step if step > 0 else Decimal("0.001"),
    }
    _FILTER_CACHE[symbol] = filters
    return filters


def _quantize(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    quantized = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
    exponent = max(0, -step.as_tuple().exponent)
    return quantized.quantize(Decimal("1").scaleb(-exponent))


def _quantize_price(client: FuturesTestnetClient, symbol: str, price: Decimal) -> Decimal:
    return _quantize(price, _symbol_filters(client, symbol)["tick_size"])


def _quantize_quantity(client: FuturesTestnetClient, symbol: str, quantity: Decimal) -> Decimal:
    stepped = _quantize(quantity, _symbol_filters(client, symbol)["step_size"])
    if stepped <= 0:
        raise ValueError(f"Quantity {format(quantity, 'f')} is below the LOT_SIZE step for {symbol}.")
    return stepped


def _filled_quantity(exchange_response: Mapping[str, Any], fallback: Decimal) -> Decimal:
    for key in ("executedQty", "origQty", "cumQty"):
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


def _enforce_futures_exposure(
    client: FuturesTestnetClient,
    *,
    symbol: str,
    quantity: Decimal,
    last_price: Decimal,
) -> dict[str, Decimal]:
    notional = quantity * last_price
    account = _account(client)
    total, available = _wallet_usdt(account)
    portfolio_usdt = available if available > 0 else total
    effective_cap = enforce_size_limits(
        notional_usdt=notional,
        portfolio_usdt=portfolio_usdt,
        symbol=symbol,
        venue="futures",
    )
    return {
        "effective_cap_usdt": effective_cap,
        "portfolio_usdt": portfolio_usdt,
        "notional_usdt": notional,
        "available_balance": available,
    }
