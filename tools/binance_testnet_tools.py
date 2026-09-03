"""Official Binance Spot Testnet tools for SentinelOS (Track A).

All network calls go through ``binance-connector`` against
``https://testnet.binance.vision`` only. Production hosts are rejected
with RuntimeError. State-changing orders require ``human_confirmed=True``.
"""

from __future__ import annotations

import inspect
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Mapping
from urllib.parse import urlparse

from binance.spot import Spot

from core.config import SPOT_BASE_URL, Settings, get_settings
from policies.testnet_only import verify_testnet_environment

logger = logging.getLogger(__name__)

MAX_EXPOSURE_FRACTION: Final[Decimal] = Decimal("0.10")
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
STABLE_ASSETS: Final[frozenset[str]] = frozenset({"USDT", "USDC", "FDUSD", "BUSD", "TUSD"})


def create_spot_testnet_client() -> Spot:
    """Build a ``binance-connector`` Spot client pinned to Testnet credentials."""
    settings = get_settings()
    _assert_testnet_settings(settings)
    api_key = settings.spot_api_key.get_secret_value()
    api_secret = settings.spot_api_secret.get_secret_value()
    client = Spot(**_spot_constructor_kwargs(api_key, api_secret, settings.spot_base_url))
    _assert_testnet_client(client, settings)
    return client


def get_spot_testnet_balance(client: Spot) -> dict[str, Any]:
    """Fetch Spot Testnet account balances. Read-only; no order is placed."""
    settings = _guard_spot_client(client)
    raw = _invoke(client, "account")
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


def preview_spot_testnet_order(
    symbol: str,
    side: str,
    quantity: Decimal | float | int | str,
) -> dict[str, Any]:
    """Return a structured mock order preview. Does not contact the matching engine."""
    settings = get_settings()
    _assert_testnet_settings(settings)
    normalized_symbol = _normalize_symbol(symbol)
    normalized_side = _normalize_side(side)
    normalized_quantity = _normalize_quantity(quantity)
    quote_asset = _quote_asset(normalized_symbol)
    return {
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
            "quantity": format(normalized_quantity, "f"),
            "quote_asset": quote_asset,
        },
        "risk": {
            "max_exposure_fraction": format(MAX_EXPOSURE_FRACTION, "f"),
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


def submit_spot_testnet_order(
    client: Spot,
    symbol: str,
    side: str,
    quantity: Decimal | float | int | str,
    human_confirmed: bool = False,
) -> dict[str, Any]:
    """Submit a Spot Testnet MARKET order only after explicit human confirmation."""
    if human_confirmed is not True:
        raise PermissionError(
            "submit_spot_testnet_order requires explicit human confirmation. "
            "human_confirmed=False is treated as denial. No order was sent."
        )

    settings = _guard_spot_client(client)
    normalized_symbol = _normalize_symbol(symbol)
    normalized_side = _normalize_side(side)
    normalized_quantity = _normalize_quantity(quantity)

    _enforce_exposure_limit(
        client,
        symbol=normalized_symbol,
        side=normalized_side,
        quantity=normalized_quantity,
    )

    logger.info(
        "Submitting Spot Testnet MARKET order symbol=%s side=%s quantity=%s",
        normalized_symbol,
        normalized_side,
        format(normalized_quantity, "f"),
    )
    raw = _invoke(
        client,
        "new_order",
        symbol=normalized_symbol,
        side=normalized_side,
        type="MARKET",
        quantity=format(normalized_quantity, "f"),
    )
    exchange_response = _as_mapping(raw)
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
            "quantity": format(normalized_quantity, "f"),
        },
        "exchange_response": exchange_response,
    }


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
    settings = get_settings()
    _assert_testnet_settings(settings)
    _assert_testnet_client(client, settings)
    return settings


def _invoke(client: Spot, method_name: str, **kwargs: Any) -> Any:
    method = getattr(client, method_name, None)
    if not callable(method):
        rest_api = getattr(client, "rest_api", None)
        method = getattr(rest_api, method_name, None) if rest_api is not None else None
    if not callable(method):
        raise RuntimeError(
            f"binance-connector Spot client does not expose {method_name}()."
        )
    response = method(**kwargs) if kwargs else method()
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
    try:
        value = Decimal(str(quantity).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise ValueError(f"Invalid order quantity: {quantity!r}.") from exc
    if value <= 0:
        raise ValueError("Order quantity must be greater than zero.")
    return value.normalize()


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


def _enforce_exposure_limit(
    client: Spot,
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
) -> None:
    quote_asset = _quote_asset(symbol)
    base_asset = _base_asset(symbol, quote_asset)
    last_price = _last_price(client, symbol)
    notional = quantity * last_price
    account = _as_mapping(_invoke(client, "account"))
    balances = _balance_map(account.get("balances", []))
    portfolio = _portfolio_quote_value(balances, base_asset, quote_asset, last_price)
    if portfolio <= 0:
        raise PermissionError(
            "Cannot size a Testnet order against an empty portfolio. No order was sent."
        )
    max_notional = portfolio * MAX_EXPOSURE_FRACTION
    if notional > max_notional:
        raise PermissionError(
            f"Order notional {format(notional, 'f')} {quote_asset} exceeds the "
            f"{format(MAX_EXPOSURE_FRACTION * 100, 'f')}% portfolio cap "
            f"({format(max_notional, 'f')} {quote_asset} of "
            f"{format(portfolio, 'f')} {quote_asset}). No order was sent."
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
