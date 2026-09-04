"""Fail-closed helpers for multi-step Testnet workflow orchestration.

The CLI agent plans locally, then executes **only** MCP tools. Submit tools
are never part of a plan object. Percent sizing is arithmetic, not a
network call. Gemini planning is optional and allowlisted.
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Final

from core.audit import audit
from core.cli_parse import WorkflowIntent
from core.risk import MAX_EXPOSURE_FRACTION, MAX_NOTIONAL_USDT

_STABLE_POSITIVE: Final[tuple[str, ...]] = (
    "stable",
    "range-bound",
    "range bound",
    "low volatility",
    "quiet",
    "sideways",
    "consolidat",
    "balanced",
)
_STABLE_NEGATIVE: Final[tuple[str, ...]] = (
    "volatile",
    "unstable",
    "breakdown",
    "breakout",
    "crash",
    "spike",
    "whipsaw",
    "high volatility",
    "panic",
    "sell-off",
    "selloff",
    "parabolic",
)
_STABILITY_BAND_PCT: Final[Decimal] = Decimal("5")
_QTY_QUANTUM: Final[Decimal] = Decimal("0.00000001")

_LLM_PLAN_PROMPT = (
    "You are SentinelOS, a fail-closed Binance Testnet MCP orchestrator. "
    "Decompose the user request into JSON with this exact schema:\n"
    "{\n"
    '  "check_spot_balance": bool,\n'
    '  "check_futures_balance": bool,\n'
    '  "check_futures_positions": bool,\n'
    '  "analyze_symbol": string or null,\n'
    '  "require_stable": bool,\n'
    '  "trade_venue": "spot" or "futures" or null,\n'
    '  "trade_side": "BUY" or "SELL" or null,\n'
    '  "trade_symbol": string or null,\n'
    '  "percent_of_available": string or null,\n'
    '  "percent_asset": "USDT",\n'
    '  "stop_loss": string or null,\n'
    '  "take_profit": string or null\n'
    "}\n"
    "Rules: Testnet only. Never include a submit/execute step. Preview only. "
    "Human-in-the-loop is mandatory after preview. Do not invent symbols. "
    "percent_of_available is a percent number such as \"5\", not a fraction. "
    "Return ONLY JSON."
)


def available_quote(payload: dict[str, Any] | None, asset: str = "USDT") -> Decimal:
    """Read free/available quote units from a Testnet balance MCP payload."""
    if not payload:
        return Decimal("0")
    wanted = str(asset or "USDT").upper()
    venue = str(payload.get("venue", "spot")).lower()
    if venue == "futures":
        if wanted == "USDT":
            raw = payload.get("available_balance")
            if raw is not None:
                return _as_decimal(raw)
        for row in payload.get("assets") or []:
            if str(row.get("asset", "")).upper() == wanted:
                return _as_decimal(row.get("available_balance", "0"))
        return Decimal("0")
    for row in payload.get("balances") or []:
        if str(row.get("asset", "")).upper() == wanted:
            return _as_decimal(row.get("free", "0"))
    return Decimal("0")


def last_price_from_analysis(payload: dict[str, Any] | None, *, venue: str = "spot") -> Decimal:
    """Pull a positive Testnet last price from an analyze_symbol payload."""
    if not payload:
        return Decimal("0")
    market = payload.get("market") or {}
    preferred = "futures" if venue == "futures" else "spot"
    for key in (preferred, "spot", "futures"):
        row = market.get(key) or {}
        price = _as_decimal(row.get("last_price", "0"))
        if price > 0:
            return price
    return Decimal("0")


def max_abs_change_percent(payload: dict[str, Any] | None) -> Decimal | None:
    if not payload:
        return None
    market = payload.get("market") or {}
    moves: list[Decimal] = []
    for key in ("spot", "futures"):
        row = market.get(key) or {}
        raw = row.get("price_change_percent")
        if raw in {None, ""}:
            continue
        try:
            moves.append(abs(_as_decimal(raw)))
        except (InvalidOperation, ValueError):
            continue
    if not moves:
        return None
    return max(moves)


def assess_market_stability(payload: dict[str, Any] | None) -> tuple[bool, str]:
    """Heuristic stability gate. Fail-closed when evidence is missing."""
    analysis = str((payload or {}).get("analysis") or "").lower()
    negative = any(token in analysis for token in _STABLE_NEGATIVE)
    positive = any(token in analysis for token in _STABLE_POSITIVE)
    move = max_abs_change_percent(payload)

    if negative and not positive:
        return False, "Gemini reading flags instability; preview skipped (fail-closed)."
    if move is not None and move > _STABILITY_BAND_PCT:
        return (
            False,
            f"24h move {format(move, 'f')}% exceeds the {_STABILITY_BAND_PCT}% stability band; "
            "preview skipped (fail-closed).",
        )
    if move is not None and move <= _STABILITY_BAND_PCT:
        reason = f"24h move {format(move, 'f')}% is within the {_STABILITY_BAND_PCT}% stability band."
        if positive:
            reason += " Gemini reading is consistent with a stable market."
        return True, reason
    if positive:
        return True, "Gemini reading describes a stable market."
    return False, "Unable to confirm market stability from Testnet data; preview skipped (fail-closed)."


def quantity_from_percent(
    *,
    available_usdt: Decimal,
    percent: Decimal,
    last_price: Decimal,
) -> dict[str, Decimal | bool]:
    """Convert a percent of available USDT into a base-asset MARKET quantity.

    Applies min(10% of available, 1000 USDT) before dividing by last price.
    """
    if available_usdt <= 0:
        raise PermissionError(
            "SECURITY WARNING: No available USDT to size a Testnet order. No order was sent."
        )
    if last_price <= 0:
        raise RuntimeError("Cannot size a percent order without a positive Testnet last price.")
    if percent <= 0 or percent > 100:
        raise ValueError(f"Percent of available USDT must be in (0, 100], got {percent}.")

    requested_fraction = percent / Decimal("100")
    applied_fraction = min(requested_fraction, MAX_EXPOSURE_FRACTION)
    spend = available_usdt * applied_fraction
    if spend > MAX_NOTIONAL_USDT:
        spend = MAX_NOTIONAL_USDT
    quantity = (spend / last_price).quantize(_QTY_QUANTUM, rounding=ROUND_DOWN)
    if quantity <= 0:
        raise ValueError("Computed order quantity is zero after rounding.")
    return {
        "quantity": quantity,
        "notional_usdt": spend,
        "requested_fraction": requested_fraction,
        "applied_fraction": spend / available_usdt,
        "capped": applied_fraction < requested_fraction or (available_usdt * applied_fraction) > MAX_NOTIONAL_USDT,
    }


def format_quantity(quantity: Decimal) -> str:
    quantized = quantity.quantize(_QTY_QUANTUM, rounding=ROUND_DOWN)
    text = format(quantized.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def llm_plan_workflow(utterance: str) -> WorkflowIntent | None:
    """Optional Gemini planner. Allowlisted fields only; submit is impossible here."""
    text = str(utterance or "").strip()
    if not text:
        return None
    try:
        payload = _gemini_plan_json(text)
    except Exception as exc:
        audit("ERROR", "LLM workflow planner failed.", error=str(exc))
        return None
    return workflow_intent_from_plan(payload)


def workflow_intent_from_plan(payload: object) -> WorkflowIntent | None:
    """Validate an LLM/JSON plan. Unknown keys and submit steps are dropped."""
    if not isinstance(payload, dict):
        return None
    analyze = _optional_symbol(payload.get("analyze_symbol"))
    trade_symbol = _optional_symbol(payload.get("trade_symbol")) or analyze
    trade_side = _optional_side(payload.get("trade_side"))
    trade_venue = _optional_venue(payload.get("trade_venue"))
    if trade_side and trade_venue is None:
        trade_venue = "spot"
    percent = _optional_percent(payload.get("percent_of_available"))
    asset = str(payload.get("percent_asset") or "USDT").strip().upper() or "USDT"
    intent = WorkflowIntent(
        check_spot_balance=bool(payload.get("check_spot_balance")),
        check_futures_balance=bool(payload.get("check_futures_balance")),
        check_futures_positions=bool(payload.get("check_futures_positions")),
        analyze_symbol=analyze,
        require_stable=bool(payload.get("require_stable")),
        trade_venue=trade_venue if trade_side else None,
        trade_side=trade_side,
        trade_symbol=trade_symbol if trade_side else None,
        percent_of_available=percent,
        percent_asset=asset,
        stop_loss=_optional_price(payload.get("stop_loss")),
        take_profit=_optional_price(payload.get("take_profit")),
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


def _as_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _optional_symbol(value: object) -> str | None:
    if value in {None, ""}:
        return None
    normalized = str(value).strip().upper().replace("-", "").replace("_", "").replace("/", "")
    if not normalized.isalnum() or len(normalized) < 5:
        return None
    return normalized


def _optional_side(value: object) -> str | None:
    if value in {None, ""}:
        return None
    side = str(value).strip().upper()
    return side if side in {"BUY", "SELL"} else None


def _optional_venue(value: object) -> str | None:
    if value in {None, ""}:
        return None
    venue = str(value).strip().lower()
    return venue if venue in {"spot", "futures"} else None


def _optional_percent(value: object) -> str | None:
    if value in {None, ""}:
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if parsed <= 0 or parsed > 100:
        return None
    return format(parsed, "f")


def _optional_price(value: object) -> str | None:
    if value in {None, ""}:
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if parsed <= 0:
        return None
    return format(parsed, "f")


def _gemini_plan_json(utterance: str) -> dict[str, Any]:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types

    from core.config import get_settings
    from tools.market_analysis import (
        _call_ids_for_model,
        _extract_text,
        _is_model_missing,
        _resolve_model_candidates,
    )

    settings = get_settings()
    api_key = settings.llm_api_key.get_secret_value().strip()
    if not api_key:
        raise RuntimeError("LLM_API_KEY is empty.")

    config = types.GenerateContentConfig(
        system_instruction=_LLM_PLAN_PROMPT,
        temperature=0.0,
        max_output_tokens=512,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    user_content = f"User utterance:\n{utterance}\nReturn the SentinelOS workflow JSON now."
    last_error: Exception | None = None
    with genai.Client(api_key=api_key) as client:
        candidates = _resolve_model_candidates(client, settings.llm_model)
        for catalog_name in candidates:
            for model in _call_ids_for_model(catalog_name):
                try:
                    response = client.models.generate_content(
                        model=model,
                        contents=user_content,
                        config=config,
                    )
                except genai_errors.APIError as exc:
                    last_error = exc
                    if _is_model_missing(exc):
                        continue
                    raise
                except Exception as exc:
                    last_error = exc
                    if _is_model_missing(exc):
                        continue
                    raise
                text = _extract_text(response)
                parsed = _loads_json_object(text)
                if parsed is not None:
                    audit("WORKFLOW_PLAN", "LLM workflow plan accepted.", model=model)
                    return parsed
                last_error = RuntimeError(f"Gemini model {model} returned non-JSON.")
    raise RuntimeError(str(last_error) if last_error else "Gemini planner returned no JSON.")


def _loads_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None
