"""Gemini-backed Testnet market analysis for SentinelOS.

Uses the official Google GenAI SDK (``google-genai``) and ``LLM_API_KEY``.
The generate model is resolved dynamically from ``client.models.list()``:
the first catalog entry whose name contains ``flash`` and that supports
``generateContent``. Automatic function calling is disabled.
"""

from __future__ import annotations

import json
from typing import Any, Final, Iterable

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from core.audit import audit
from core.config import get_settings
from core.kill_switch import get_kill_switch
from tools.binance_futures_testnet_tools import get_futures_testnet_ticker
from tools.binance_testnet_tools import get_spot_testnet_ticker

FALLBACK_GEMINI_MODELS: Final[tuple[str, ...]] = (
    "gemini-1.5-flash-latest",
    "gemini-1.5-flash-001",
)
_SKIP_NAME_TOKENS: Final[tuple[str, ...]] = (
    "embed",
    "imagen",
    "image",
    "tts",
    "audio",
    "live",
    "robotics",
    "veo",
)
_MAX_LISTED_MODELS: Final[int] = 250
_CACHED_FLASH_MODELS: list[str] | None = None

_SYSTEM_PROMPT = (
    "You are SentinelOS, a fail-closed risk-defense agent on Binance Testnet. "
    "Using only the supplied Testnet market snapshot, write a short technical "
    "and sentiment reading. Do not give financial advice. Do not promote any "
    "asset. Do not recommend a trade size or guarantee an outcome. State that "
    "the data is from Binance Testnet and may differ from production. "
    "Respond with 4-8 concise bullet points covering trend, 24h momentum, "
    "volatility, notable levels, and a risk note. Keep it under 180 words."
)


def analyze_symbol(symbol: str) -> dict[str, Any]:
    """Fetch live Testnet tickers and produce a Gemini reading via LLM_API_KEY."""
    get_kill_switch().raise_if_tripped()
    normalized = str(symbol).strip().upper().replace("-", "").replace("_", "").replace("/", "")
    if not normalized.isalnum() or len(normalized) < 5:
        raise ValueError(f"Invalid symbol for analysis: {symbol!r}.")

    errors: list[str] = []
    spot: dict[str, Any] | None = None
    futures: dict[str, Any] | None = None
    try:
        spot = get_spot_testnet_ticker(normalized)
    except Exception as exc:
        errors.append(f"spot: {exc}")
        audit("API_ERROR", "Spot ticker failed during analyze.", symbol=normalized, error=str(exc))
    try:
        futures = get_futures_testnet_ticker(normalized)
    except Exception as exc:
        errors.append(f"futures: {exc}")
        audit("API_ERROR", "Futures ticker failed during analyze.", symbol=normalized, error=str(exc))

    if spot is None and futures is None:
        raise RuntimeError(
            "Unable to fetch Testnet market data for "
            f"{normalized}. {'; '.join(errors) or 'No ticker source responded.'}"
        )

    snapshot = {
        "environment": "testnet",
        "symbol": normalized,
        "spot": _public_ticker(spot) if spot else None,
        "futures": _public_ticker(futures) if futures else None,
        "fetch_errors": errors,
    }
    audit(
        "ANALYSIS",
        "Fetched Testnet market snapshot.",
        symbol=normalized,
        has_spot=spot is not None,
        has_futures=futures is not None,
    )

    llm_text: str | None = None
    llm_error: str | None = None
    used_model = "auto"
    try:
        llm_text, used_model = _gemini_reading(normalized, snapshot)
        audit("ANALYSIS", "Gemini market reading completed.", symbol=normalized, model=used_model)
    except Exception as exc:
        llm_error = str(exc)
        audit("API_ERROR", "Gemini analysis failed.", symbol=normalized, error=llm_error)

    return {
        "environment": "testnet",
        "status": "analysis",
        "symbol": normalized,
        "provider": "google-gemini",
        "model": used_model,
        "market": snapshot,
        "analysis": llm_text,
        "analysis_error": llm_error,
        "disclaimer": (
            "Informational Testnet reading only. Not financial advice. "
            "All trades still require explicit Y confirmation and remain on Testnet."
        ),
    }


def _public_ticker(payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "venue",
        "base_url",
        "symbol",
        "last_price",
        "price_change_percent",
        "open_price",
        "high_price",
        "low_price",
        "volume",
        "quote_volume",
        "weighted_avg_price",
        "bid_price",
        "ask_price",
        "trade_count",
        "hourly_closes",
    )
    return {key: payload.get(key) for key in keys if key in payload}


def _normalize_model_id(name: str) -> str:
    raw = str(name or "").strip()
    if raw.lower().startswith("models/"):
        raw = raw[7:]
    return raw.strip()


def _supports_generate_content(model: object) -> bool:
    actions = getattr(model, "supported_actions", None)
    if not actions:
        return True
    for action in actions:
        compact = str(action).replace("_", "").replace("-", "").lower()
        if "generatecontent" in compact:
            return True
    return False


def _is_text_flash_name(name: str) -> bool:
    lowered = name.lower()
    if "flash" not in lowered:
        return False
    return not any(token in lowered for token in _SKIP_NAME_TOKENS)


def _flash_rank(name: str) -> tuple[int, int, int, str]:
    lowered = name.lower()
    return (
        int("exp" in lowered or "preview" in lowered),
        int("lite" in lowered),
        int("latest" not in lowered),
        lowered,
    )


def select_flash_models_from_catalog(models: Iterable[object]) -> list[str]:
    """Return flash models that support generateContent, stable ids first."""
    discovered: list[str] = []
    seen: set[str] = set()
    for model in models:
        name = str(getattr(model, "name", None) or getattr(model, "display_name", "") or "").strip()
        if not name or not _is_text_flash_name(name) or not _supports_generate_content(model):
            continue
        if name in seen:
            continue
        seen.add(name)
        discovered.append(name)
    discovered.sort(key=_flash_rank)
    return discovered


def _discover_flash_models(client: genai.Client) -> list[str]:
    global _CACHED_FLASH_MODELS
    if _CACHED_FLASH_MODELS is not None:
        return list(_CACHED_FLASH_MODELS)

    catalog: list[object] = []
    try:
        for index, model in enumerate(client.models.list()):
            catalog.append(model)
            if index + 1 >= _MAX_LISTED_MODELS:
                break
    except Exception as exc:
        audit("API_ERROR", "Gemini models.list() failed; using fallbacks.", error=str(exc))
        return []

    discovered = select_flash_models_from_catalog(catalog)
    _CACHED_FLASH_MODELS = discovered
    audit(
        "ANALYSIS",
        "Resolved Gemini flash models from catalog.",
        count=len(discovered),
        first=discovered[0] if discovered else None,
    )
    return list(discovered)


def _resolve_model_candidates(client: genai.Client, configured: str) -> list[str]:
    discovered = _discover_flash_models(client)
    preferred = _normalize_model_id(configured)
    ordered: list[str] = []

    def _add(name: str) -> None:
        if name and name not in ordered:
            ordered.append(name)

    if preferred and preferred.lower() not in {"auto", "dynamic", "latest"}:
        # Prefer an exact catalog match (including models/ prefix).
        for item in discovered:
            if _normalize_model_id(item).lower() == preferred.lower() or item.lower() == preferred.lower():
                _add(item)
                break
        else:
            _add(preferred)

    for item in discovered:
        _add(item)
    for item in FALLBACK_GEMINI_MODELS:
        _add(item)
    return ordered


def _call_ids_for_model(name: str) -> list[str]:
    """Try the catalog name first, then the bare Gemini id."""
    exact = name.strip()
    ids = [exact]
    bare = _normalize_model_id(exact)
    if bare and bare not in ids:
        ids.append(bare)
    return ids


def _is_model_missing(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    status = str(getattr(exc, "status", "") or "")
    message = str(getattr(exc, "message", None) or exc).lower()
    if code == 404:
        return True
    if "not_found" in status.lower() or "not found" in message:
        return True
    if "404" in message and "model" in message:
        return True
    return False


def _extract_text(response: object) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    chunks: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text.strip():
                chunks.append(part_text.strip())
    return "\n".join(chunks).strip()


def _gemini_reading(symbol: str, snapshot: dict[str, Any]) -> tuple[str, str]:
    """Call Gemini with a plain text prompt. Returns (text, model_used)."""
    settings = get_settings()
    api_key = settings.llm_api_key.get_secret_value().strip()
    if not api_key:
        raise RuntimeError("LLM_API_KEY is empty. Set a Google Gemini API key in .env.")

    user_content = (
        f"Symbol: {symbol}\n"
        f"Testnet market snapshot (JSON):\n{json.dumps(snapshot, indent=2, default=str)}\n"
        "Produce the SentinelOS reading now."
    )
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        temperature=0.2,
        max_output_tokens=512,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    last_error: Exception | None = None
    with genai.Client(api_key=api_key) as client:
        candidates = _resolve_model_candidates(client, settings.llm_model)
        for catalog_name in candidates:
            get_kill_switch().raise_if_tripped()
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
                    raise RuntimeError(
                        f"Gemini API error ({getattr(exc, 'code', '?')}): "
                        f"{getattr(exc, 'message', None) or exc}"
                    ) from exc
                except Exception as exc:
                    last_error = exc
                    if _is_model_missing(exc):
                        continue
                    raise RuntimeError(f"Gemini request failed: {exc}") from exc

                text = _extract_text(response)
                if text:
                    return text, model
                last_error = RuntimeError(f"Gemini model {model} returned an empty analysis.")

    detail = str(last_error) if last_error else "no Gemini model responded"
    raise RuntimeError(f"Gemini analysis failed after dynamic model discovery. {detail}")
