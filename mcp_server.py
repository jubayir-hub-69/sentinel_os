"""SentinelOS local MCP server (Track A).

This is a real Model Context Protocol server built with the official
``mcp`` Python SDK (``MCPServer``). It is the only permitted wrapper
for trade-related Testnet actions.

Binance Agent OS requires a **local MCP tool wrapper/server** for
trade-related actions. The hosted endpoint
``https://agent.binance.com/mcp/agentic`` is used for capability
discovery documentation only and is never a bypass around these tools.

Transports:
- ``python mcp_server.py`` — stdio JSON-RPC (Claude Desktop, Inspector, any MCP host)
- In-process ``Client(mcp)`` — same protocol, used by the SentinelOS CLI

Every tool payload includes ``"environment": "testnet"``. Guardrails
(Testnet-only hosts, 10% / 1000 USDT cap, HITL ``human_confirmed``,
kill-switch, audit log) stay inside the wrapped tools and are not
relaxed by this server.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from core.audit import AUDIT_LOG_PATH, audit, read_recent
from core.config import FUTURES_BASE_URL, SPOT_BASE_URL, get_settings
from core.kill_switch import KillSwitchActivated, get_kill_switch
from core.risk import MAX_EXPOSURE_FRACTION, MAX_NOTIONAL_USDT
from policies.testnet_only import verify_testnet_environment
from tools.binance_futures_testnet_tools import (
    create_futures_testnet_client,
    get_futures_testnet_balance as _get_futures_balance,
    get_futures_testnet_positions as _get_futures_positions,
    preview_futures_testnet_order as _preview_futures,
    submit_futures_testnet_order as _submit_futures,
)
from tools.binance_testnet_tools import (
    create_spot_testnet_client,
    get_spot_testnet_balance as _get_spot_balance,
    preview_spot_testnet_order as _preview_spot,
    submit_spot_testnet_order as _submit_spot,
)
from tools.market_analysis import analyze_symbol as _analyze_symbol

logger = logging.getLogger("sentinelos.mcp")

ROOT = Path(__file__).resolve().parent
_GUARDRAILS_PATH = ROOT / "policies" / "execution_guardrails.md"
_SKILL_PATH = ROOT / "skills" / "binance" / "SKILL.md"

_spot_client: Any = None
_futures_client: Any = None

mcp = MCPServer(
    name="sentinelos-testnet-mcp",
    title="SentinelOS Testnet MCP",
    version="1.1.0",
    instructions=(
        "SentinelOS local MCP server for Binance Agent OS Track A. "
        "Environment is strictly Binance Testnet (Spot + USDⓈ-M Futures). "
        "Official hosted MCP (https://agent.binance.com/mcp/agentic) is discovery-only. "
        "ALL trade-related actions MUST go through these local tools. "
        "Never call api.binance.com or fapi.binance.com. "
        "Multi-step natural language MUST be orchestrated as chained tools/call: "
        "read-only first (get_spot_testnet_balance / get_futures_testnet_balance / "
        "get_futures_testnet_positions / analyze_symbol), then compute any percent "
        "size locally, then preview_*_testnet_order, then STOP for an explicit human Y. "
        "NEVER call submit_* until the human types Y in this turn. "
        "Preview first. Submit tools require human_confirmed=true after an explicit "
        "human Y. human_confirmed defaults to false (denial). "
        "Hard cap: min(10% of Testnet equity, 1000 USDT notional). "
        "Every JSON result includes environment=testnet."
    ),
    website_url="https://developers.binance.com/en/docs/agent-native/overview",
)


def _close_exchange_clients() -> None:
    global _spot_client, _futures_client
    if _futures_client is not None:
        closer = getattr(_futures_client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        _futures_client = None
    if _spot_client is not None:
        session = getattr(_spot_client, "session", None)
        closer = getattr(session, "close", None) if session is not None else None
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        _spot_client = None


get_kill_switch().register_shutdown_hook(_close_exchange_clients)


def shutdown_mcp_server() -> None:
    """Close cached Testnet clients. Safe to call from the CLI host on exit."""
    _close_exchange_clients()


def _spot() -> Any:
    global _spot_client
    get_kill_switch().raise_if_tripped()
    if _spot_client is None:
        _spot_client = create_spot_testnet_client()
    return _spot_client


def _futures() -> Any:
    global _futures_client
    get_kill_switch().raise_if_tripped()
    if _futures_client is None:
        _futures_client = create_futures_testnet_client()
    return _futures_client


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _stamp_testnet(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {"result": payload}
    payload.setdefault("environment", "testnet")
    if payload.get("environment") != "testnet":
        raise ToolError(
            "Blocked non-Testnet payload. SentinelOS MCP tools are fail-closed to testnet."
        )
    return payload


def _invoke_guarded(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run a wrapped Testnet tool and map failures onto MCP ToolError."""
    get_kill_switch().raise_if_tripped()
    try:
        return _stamp_testnet(fn(*args, **kwargs))
    except KillSwitchActivated:
        raise
    except ToolError:
        raise
    except (PermissionError, RuntimeError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        logger.exception("MCP tool failed")
        raise ToolError(str(exc) or exc.__class__.__name__) from exc


def _deny_unconfirmed(tool_name: str, human_confirmed: bool) -> None:
    """HITL fail-closed: only an exact True proceeds. Default is denial."""
    if human_confirmed is True:
        return
    audit(
        "TRADE_REJECTED",
        f"{tool_name} denied: human_confirmed is not True.",
        via="mcp",
    )
    raise ToolError(
        f"{tool_name} requires explicit human confirmation. "
        "human_confirmed=false is treated as denial. No order was sent."
    )


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


@mcp.tool(
    title="Spot Testnet balance",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
)
def get_spot_testnet_balance() -> dict[str, Any]:
    """Fetch Spot Testnet wallet balances. Read-only. Testnet only.

    Returns JSON with environment=testnet. Does not place an order.
    """
    audit("MCP_TOOL", "get_spot_testnet_balance", venue="spot")
    return _invoke_guarded(_get_spot_balance, _spot())


@mcp.tool(
    title="Futures Testnet balance",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
)
def get_futures_testnet_balance() -> dict[str, Any]:
    """Fetch USDⓈ-M Futures Testnet wallet balances. Read-only. Testnet only.

    Returns JSON with environment=testnet. Does not place an order.
    """
    audit("MCP_TOOL", "get_futures_testnet_balance", venue="futures")
    return _invoke_guarded(_get_futures_balance, _futures())


@mcp.tool(
    title="Futures Testnet open positions",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
)
def get_futures_testnet_positions(symbol: str | None = None) -> dict[str, Any]:
    """Fetch currently open USDⓈ-M Futures Testnet positions. Read-only.

    Returns symbol, position side, entry price, unrealized PnL, and position
    size. JSON includes environment=testnet. Does not place or cancel an order.
    Optional symbol filters to one contract; omit it to list every open position.
    """
    audit("MCP_TOOL", "get_futures_testnet_positions", venue="futures", symbol=symbol or "")
    return _invoke_guarded(_get_futures_positions, _futures(), _optional(symbol))


@mcp.tool(
    title="Analyze Testnet symbol",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
)
def analyze_symbol(symbol: str) -> dict[str, Any]:
    """Live Spot + Futures Testnet tickers plus a Gemini flash reading.

    Does not place an order. Automatic function calling is disabled in Gemini.
    """
    audit("MCP_TOOL", "analyze_symbol", symbol=symbol)
    return _invoke_guarded(_analyze_symbol, symbol)


@mcp.tool(
    title="Read audit history",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
def read_audit_history(limit: int = 40) -> dict[str, Any]:
    """Return the newest N lines from the append-only audit log."""
    get_kill_switch().raise_if_tripped()
    capped = max(1, min(int(limit), 500))
    events = read_recent(capped)
    audit("MCP_TOOL", "read_audit_history", limit=capped)
    return {
        "environment": "testnet",
        "status": "audit_history",
        "limit": capped,
        "path": str(AUDIT_LOG_PATH),
        "events": events,
    }


@mcp.tool(
    title="Discover official Binance MCP (read-only)",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
def discover_official_binance_mcp() -> dict[str, Any]:
    """Document the official hosted Binance MCP endpoint for discovery only.

    Does not execute trades through agent.binance.com. Trade-related actions
    stay on this local Testnet MCP server.
    """
    audit("MCP_TOOL", "discover_official_binance_mcp")
    return {
        "environment": "testnet",
        "status": "discovery_only",
        "official_mcp": "https://agent.binance.com/mcp/agentic",
        "agent_native": "https://developers.binance.com/en/docs/agent-native/overview",
        "llms_txt": "https://developers.binance.com/en/docs/llms.txt",
        "skills_hub": "https://github.com/binance/binance-skills-hub",
        "mcp_standard": "https://modelcontextprotocol.io/",
        "policy": (
            "Official hosted MCP is for capability discovery only. "
            "All trade-related actions MUST use this local SentinelOS Testnet MCP server. "
            "Never a bypass around HITL, size caps, or Testnet host pinning."
        ),
        "local_server": "sentinelos-testnet-mcp",
        "spot_testnet": SPOT_BASE_URL,
        "futures_testnet": FUTURES_BASE_URL,
    }


# ---------------------------------------------------------------------------
# Dry-run previews (no fill)
# ---------------------------------------------------------------------------


@mcp.tool(
    title="Preview Spot Testnet order",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
)
def preview_spot_testnet_order(
    symbol: str,
    side: str,
    quantity: str,
    stop_loss: str | None = None,
    take_profit: str | None = None,
) -> dict[str, Any]:
    """Build a Spot Testnet MARKET preview. Dry-run only. Does not place an order.

    Enforces the 10% / 1000 USDT cap. Returns environment=testnet and
    status=order_preview. Submit only after an explicit human Y.
    """
    audit("MCP_TOOL", "preview_spot_testnet_order", venue="spot", symbol=symbol, side=side)
    return _invoke_guarded(
        _preview_spot,
        symbol,
        side,
        quantity,
        _optional(stop_loss),
        _optional(take_profit),
        _spot(),
    )


@mcp.tool(
    title="Preview Futures Testnet order",
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
)
def preview_futures_testnet_order(
    symbol: str,
    side: str,
    quantity: str,
    stop_loss: str | None = None,
    take_profit: str | None = None,
) -> dict[str, Any]:
    """Build a USDⓈ-M Futures Testnet MARKET preview. Dry-run only.

    Enforces the 10% / 1000 USDT cap. Returns environment=testnet and
    status=order_preview. Submit only after an explicit human Y.
    """
    audit("MCP_TOOL", "preview_futures_testnet_order", venue="futures", symbol=symbol, side=side)
    return _invoke_guarded(
        _preview_futures,
        symbol,
        side,
        quantity,
        _optional(stop_loss),
        _optional(take_profit),
        _futures(),
    )


# ---------------------------------------------------------------------------
# State-changing tools (HITL required)
# ---------------------------------------------------------------------------


@mcp.tool(
    title="Submit Spot Testnet order",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
)
def submit_spot_testnet_order(
    symbol: str,
    side: str,
    quantity: str,
    human_confirmed: bool = False,
    stop_loss: str | None = None,
    take_profit: str | None = None,
) -> dict[str, Any]:
    """Submit a Spot Testnet MARKET order. Requires human_confirmed=true.

    human_confirmed defaults to false (denial). The CLI may set true only after
    the operator types Y on the exact preview. Size cap is re-checked.
    """
    get_kill_switch().raise_if_tripped()
    _deny_unconfirmed("submit_spot_testnet_order", human_confirmed)
    audit("MCP_TOOL", "submit_spot_testnet_order", venue="spot", symbol=symbol, side=side)
    return _invoke_guarded(
        _submit_spot,
        _spot(),
        symbol,
        side,
        quantity,
        True,
        _optional(stop_loss),
        _optional(take_profit),
    )


@mcp.tool(
    title="Submit Futures Testnet order",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
)
def submit_futures_testnet_order(
    symbol: str,
    side: str,
    quantity: str,
    human_confirmed: bool = False,
    stop_loss: str | None = None,
    take_profit: str | None = None,
) -> dict[str, Any]:
    """Submit a USDⓈ-M Futures Testnet MARKET order. Requires human_confirmed=true.

    human_confirmed defaults to false (denial). After fill, reduce-only SL/TP
    algo orders are attached. Size cap is re-checked. Testnet only.
    """
    get_kill_switch().raise_if_tripped()
    _deny_unconfirmed("submit_futures_testnet_order", human_confirmed)
    audit("MCP_TOOL", "submit_futures_testnet_order", venue="futures", symbol=symbol, side=side)
    return _invoke_guarded(
        _submit_futures,
        _futures(),
        symbol,
        side,
        quantity,
        True,
        _optional(stop_loss),
        _optional(take_profit),
    )


@mcp.tool(
    title="Emergency kill-switch",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True),
)
def trip_kill_switch(reason: str = "mcp kill tool") -> dict[str, Any]:
    """Arm the process-wide kill-switch, close Testnet clients, and halt trading.

    There is no in-process resume. Subsequent tools fail closed.
    """
    get_kill_switch().trip(reason=reason or "mcp kill tool")
    return {
        "environment": "testnet",
        "status": "kill_switch_armed",
        "message": "Emergency kill-switch activated. All trading is halted. No order was sent.",
        "reason": reason or "mcp kill tool",
    }


# ---------------------------------------------------------------------------
# Resources (file-like context for the LLM)
# ---------------------------------------------------------------------------


@mcp.resource(
    "sentinel://environment",
    title="Testnet environment",
    mime_type="application/json",
    description="Pinned Testnet hosts, size cap, and MCP identity. Always environment=testnet.",
)
def resource_environment() -> str:
    settings = get_settings()
    verify_testnet_environment(settings.spot_base_url)
    verify_testnet_environment(settings.futures_base_url)
    payload = {
        "environment": "testnet",
        "binance_env": settings.binance_env,
        "spot_base_url": settings.spot_base_url,
        "futures_base_url": settings.futures_base_url,
        "max_exposure_fraction": format(MAX_EXPOSURE_FRACTION, "f"),
        "max_notional_usdt": format(MAX_NOTIONAL_USDT, "f"),
        "mcp_server": "sentinelos-testnet-mcp",
        "official_mcp_discovery_only": "https://agent.binance.com/mcp/agentic",
        "human_confirmation_required": True,
    }
    return json.dumps(payload, indent=2)


@mcp.resource(
    "sentinel://policy/execution-guardrails",
    title="Execution guardrails",
    mime_type="text/markdown",
    description="HITL, 10%/1000 USDT cap, Testnet-only, kill-switch, and audit policy.",
)
def resource_guardrails() -> str:
    if _GUARDRAILS_PATH.is_file():
        return _GUARDRAILS_PATH.read_text(encoding="utf-8")
    return "# Guardrails file missing\n"


@mcp.resource(
    "sentinel://skill/binance",
    title="Binance Testnet skill",
    mime_type="text/markdown",
    description="Agent OS skill: route all trading intents through this local MCP server.",
)
def resource_skill() -> str:
    if _SKILL_PATH.is_file():
        return _SKILL_PATH.read_text(encoding="utf-8")
    return "# Skill file missing\n"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt(title="Confirm a Testnet order")
def confirm_testnet_order(venue: str, symbol: str, side: str, quantity: str) -> str:
    """HITL prompt. Never set human_confirmed=true unless the user typed Y."""
    return (
        f"You are SentinelOS on Binance Testnet ({venue}). "
        f"Proposed order: {side} {quantity} {symbol}. "
        "Show the MCP preview JSON (environment=testnet, status=order_preview). "
        "Ask the human to type Y to confirm or N to abort. Default is N. "
        "Only if they type exactly Y may you call the matching submit_* MCP tool "
        "with human_confirmed=true. Anything else is denial. "
        "Do not call production hosts. Do not raise quantity after confirmation."
    )


@mcp.prompt(title="Orchestrate a multi-step Testnet workflow")
def orchestrate_testnet_workflow(utterance: str) -> str:
    """Chain local MCP tools for compound natural language. Preview, then HITL."""
    return (
        "You are SentinelOS, a fail-closed Binance Testnet MCP host. "
        f"User utterance: {utterance!r}. "
        "Orchestrate ONLY these local tools, in this order when the request needs them: "
        "1) get_spot_testnet_balance or get_futures_testnet_balance (read-only). "
        "2) analyze_symbol (read-only Testnet tickers + Gemini). "
        "3) get_futures_testnet_positions when the user asks about open Futures positions. "
        "4) Compute any percent-of-available size locally. Cap at min(10% of equity, 1000 USDT). "
        "5) If the user asked to prepare/preview an order, call preview_spot_testnet_order "
        "or preview_futures_testnet_order. "
        "6) STOP. Present the preview. Wait for an explicit human Y. "
        "Never call submit_spot_testnet_order or submit_futures_testnet_order unless the "
        "human typed Y against this exact preview. human_confirmed defaults to false (denial). "
        "Never call production hosts. Never skip the preview. Never invent confirmation."
    )


def main() -> None:
    """Run the MCP server on stdio (JSON-RPC). Do not write to stdout."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    audit("MCP_BOOT", "stdio MCP server starting.", server="sentinelos-testnet-mcp")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
