"""SentinelOS conversational orchestrator (Track A).

Fail-closed Spot + USDⓈ-M Futures Testnet CLI built on Binance Agent OS
and MCP (Model Context Protocol). Keyword routing maps utterances to **local MCP tools** via the official
``mcp.Client``. Compound natural language is orchestrated as a chained
MCP workflow (balance → analyze → local percent math → preview) that
always stops for an explicit Y. The agent never calls Binance REST
wrappers directly. State-changing orders always require an explicit Y
confirmation after a dry-run preview.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from pydantic import ValidationError
from rich import box
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.theme import Theme
from rich.traceback import install as install_rich_traceback

from core.audit import AUDIT_LOG_PATH, audit
from core.cli_parse import (
    AFFIRMATIVE,
    BALANCE_PATTERN,
    EXIT_COMMANDS,
    FUTURES_BALANCE_PATTERN,
    HELP_COMMANDS,
    KILL_COMMANDS,
    TradeIntent,
    WorkflowIntent,
    looks_like_compound_workflow,
    parse_analyze_symbol,
    parse_history_limit,
    parse_positions_intent,
    parse_trade_intent,
    parse_workflow_intent,
)
from core.config import FUTURES_BASE_URL, SPOT_BASE_URL
from core.kill_switch import KillSwitchActivated, get_kill_switch
from core.mcp_host import SentinelMcpHost, build_mcp_client
from core.planner import (
    assess_market_stability,
    available_quote,
    format_quantity,
    last_price_from_analysis,
    llm_plan_workflow,
    quantity_from_percent,
)
from core.risk import MAX_EXPOSURE_FRACTION, MAX_NOTIONAL_USDT

# Submit MCP tools are forbidden inside the planner. The only path to submit
# is tool_trade() after an explicit Y on the exact preview.
_WORKFLOW_FORBIDDEN_TOOLS = frozenset(
    {
        "submit_spot_testnet_order",
        "submit_futures_testnet_order",
    }
)

install_rich_traceback(show_locals=False)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

THEME = Theme(
    {
        "info": "cyan",
        "warn": "bold yellow",
        "error": "bold red",
        "ok": "bold green",
        "muted": "dim",
        "brand": "bold white",
    }
)
CONSOLE = Console(theme=THEME)


@dataclass
class AgentRuntime:
    """Process-wide CLI runtime. Exchange I/O happens only through MCP tools."""

    console: Console
    mcp: SentinelMcpHost | None = None
    ready: bool = False


RUNTIME = AgentRuntime(console=CONSOLE)


def render_banner() -> None:
    console = RUNTIME.console
    body = (
        "[brand]SentinelOS[/brand]  ·  Binance Agent OS & MCP (Model Context Protocol)  ·  Track A\n"
        "[ok]Environment: TESTNET[/ok]  ·  Spot: "
        f"[info]{SPOT_BASE_URL}[/info]  ·  Futures: [info]{FUTURES_BASE_URL}[/info]\n"
        "[muted]Local MCP server: sentinelos-testnet-mcp  ·  Fail-closed production block  ·  "
        "Human-in-the-loop required  ·  "
        f"Max {format(MAX_EXPOSURE_FRACTION * 100, 'f')}% of portfolio or "
        f"${format(MAX_NOTIONAL_USDT, 'f')} notional  ·  Kill-switch armed[/muted]\n\n"
        "Type [info]help[/info]  ·  [info]balance[/info]  ·  [info]positions[/info]  ·  "
        "[info]analyze BTCUSDT[/info]  ·  "
        "[info]buy BTCUSDT 0.001 --sl 58000 --tp 62000[/info]  ·  "
        "[info]futures buy BTCUSDT 0.001[/info]  ·  [info]history[/info]  ·  [info]kill[/info]\n"
        "[muted]Compound:[/muted] Check my spot balance, analyze ETHUSDT, and if the market looks "
        "stable, prepare a spot order to buy using 5% of my available USDT."
    )
    console.print(
        Panel(
            body,
            title="[brand]SENTINEL OS  ·  AGENT OS / MCP[/brand]",
            subtitle="[muted]Dry-Run / Testnet only  ·  Local MCP wrapper  ·  hosted MCP is discovery-only[/muted]",
            border_style="cyan",
            box=box.DOUBLE,
        )
    )


def render_help() -> None:
    table = Table(
        title="Local MCP tools (official mcp SDK · tools/call)",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Utterance", style="info", no_wrap=True)
    table.add_column("Tool")
    table.add_column("Notes")
    table.add_row("balance", "get_spot_testnet_balance", "Read-only Spot Testnet snapshot")
    table.add_row(
        "futures balance",
        "get_futures_testnet_balance",
        "Read-only USDⓈ-M Futures Testnet snapshot",
    )
    table.add_row(
        "analyze SYMBOL",
        "analyze_symbol",
        "Live Testnet ticker + Gemini flash (auto-selected, no order)",
    )
    table.add_row(
        "buy SYMBOL QTY [--sl P] [--tp P]",
        "preview_spot_testnet_order → submit_spot_testnet_order",
        "Preview first; submit only after Y",
    )
    table.add_row(
        "sell SYMBOL QTY [--sl P] [--tp P]",
        "preview_spot_testnet_order → submit_spot_testnet_order",
        "Preview first; submit only after Y",
    )
    table.add_row(
        "futures buy SYMBOL QTY [--sl P] [--tp P]",
        "preview_futures_testnet_order → submit_futures_testnet_order",
        "Futures Testnet; HITL Y/N required",
    )
    table.add_row(
        "futures sell SYMBOL QTY [--sl P] [--tp P]",
        "preview_futures_testnet_order → submit_futures_testnet_order",
        "Futures Testnet; HITL Y/N required",
    )
    table.add_row(
        "positions [SYMBOL]",
        "get_futures_testnet_positions",
        "Read-only open USDⓈ-M Futures Testnet positions",
    )
    table.add_row(
        "compound natural language",
        "chained MCP tools → preview → Y/N",
        "Balance + analyze + % size; submit never skipped",
    )
    table.add_row("history [N]", "read_audit_history", "Show the latest N audit events")
    table.add_row("kill", "trip_kill_switch", "Abort pending work and shut down")
    table.add_row("help", "—", "Show this table")
    table.add_row("exit", "—", "Leave the agent")
    RUNTIME.console.print(table)
    RUNTIME.console.print(
        "[muted]Architecture: Binance Agent OS & MCP. The CLI is an MCP host; "
        "every trade/balance/analyze/positions call is tools/call on sentinelos-testnet-mcp. "
        "Compound utterances are orchestrated as chained MCP tools, then a dry-run preview. "
        "Production hosts are rejected with RuntimeError. "
        "Unconfirmed orders are treated as denial. Submit is never part of a plan. "
        f"Size cap: {format(MAX_EXPOSURE_FRACTION * 100, 'f')}% of portfolio "
        f"or ${format(MAX_NOTIONAL_USDT, 'f')} USDT, whichever is smaller.[/muted]"
    )


def render_warning(title: str, message: str) -> None:
    RUNTIME.console.print(
        Panel(
            message,
            title=f"[warn]{title}[/warn]",
            border_style="yellow",
            box=box.ROUNDED,
        )
    )


def render_error(title: str, message: str) -> None:
    RUNTIME.console.print(
        Panel(
            message,
            title=f"[error]{title}[/error]",
            border_style="red",
            box=box.ROUNDED,
        )
    )


def render_success(title: str, message: str) -> None:
    RUNTIME.console.print(
        Panel(
            message,
            title=f"[ok]{title}[/ok]",
            border_style="green",
            box=box.ROUNDED,
        )
    )


def render_kill_switch() -> None:
    RUNTIME.console.print(
        Panel(
            "[error]Emergency kill-switch activated.[/error]\n"
            "Pending operations aborted. Spot and Futures Testnet connections closed.\n"
            "Audit event written. SentinelOS will shut down securely.",
            title="[error]KILL-SWITCH[/error]",
            border_style="red",
            box=box.HEAVY,
        )
    )


def handle_exception(exc: BaseException) -> None:
    """Surface policy, config, and connector failures as rich warnings."""
    message = str(exc).strip() or exc.__class__.__name__
    audit("ERROR", message, error_type=type(exc).__name__)
    if isinstance(exc, ValidationError):
        render_warning(
            "Missing or invalid configuration",
            "Could not load Testnet settings. Copy [info].env.example[/info] to "
            "[info].env[/info] and set Spot/Futures Testnet keys plus LLM_API_KEY.\n\n"
            f"[muted]{message}[/muted]",
        )
        return
    if isinstance(exc, PermissionError):
        title = "SECURITY WARNING · ORDER BLOCKED" if "SECURITY WARNING" in message else "Authorization / risk guardrail"
        render_warning(title, message)
        return
    if isinstance(exc, RuntimeError):
        render_warning("Testnet policy block", message)
        return
    if isinstance(exc, ValueError):
        render_warning("Invalid order parameters", message)
        return
    render_error(exc.__class__.__name__, message)


def _close_runtime() -> None:
    try:
        from mcp_server import shutdown_mcp_server

        shutdown_mcp_server()
    except Exception:
        pass
    RUNTIME.mcp = None
    RUNTIME.ready = False


def require_mcp() -> SentinelMcpHost:
    get_kill_switch().raise_if_tripped()
    if RUNTIME.mcp is None:
        raise RuntimeError(
            "Local MCP host is not connected. SentinelOS refuses to call Binance REST "
            "outside the MCP protocol. Restart the agent after fixing Testnet config."
        )
    return RUNTIME.mcp


async def bootstrap_mcp(host: SentinelMcpHost) -> None:
    """Discover local MCP tools. No standalone REST from the CLI."""
    get_kill_switch().raise_if_tripped()
    RUNTIME.mcp = host
    try:
        names = await host.discover()
        environment: dict[str, Any] = {}
        try:
            environment = await host.read_json("sentinel://environment")
        except Exception as exc:
            audit("MCP_ERROR", "Failed to read sentinel://environment.", error=str(exc))
        if environment and environment.get("environment") != "testnet":
            raise RuntimeError("Blocked non-Testnet Binance host.")
        RUNTIME.ready = True
        protocol = host.protocol_version or "mcp"
        server = host.server_name or "sentinelos-testnet-mcp"
        spot = environment.get("spot_base_url", SPOT_BASE_URL)
        futures = environment.get("futures_base_url", FUTURES_BASE_URL)
        render_success(
            "Local MCP server ready",
            f"Server=[info]{server}[/info]  protocol=[info]{protocol}[/info]\n"
            f"Discovered MCP tools: [info]{', '.join(names)}[/info]\n"
            f"Spot pinned to [info]{spot}[/info]. "
            f"Futures pinned to [info]{futures}[/info]. "
            "Hosted MCP is discovery-only. Trades stay on this local wrapper.",
        )
        audit(
            "BOOT",
            "MCP host connected to local Testnet server.",
            server=server,
            protocol=protocol,
            tools=",".join(names),
        )
    except (ValidationError, RuntimeError, PermissionError, OSError, ValueError) as exc:
        RUNTIME.ready = False
        handle_exception(exc)
        render_warning(
            "Degraded mode",
            "The CLI is up, but MCP tools will fail until Testnet configuration is valid. "
            "No production endpoint will be used. No standalone REST fallback exists.",
        )


def render_balances(payload: dict[str, Any]) -> None:
    venue = str(payload.get("venue", "spot"))
    title = (
        f"Spot Testnet balances  ·  {payload.get('base_url', SPOT_BASE_URL)}"
        if venue != "futures"
        else f"Futures Testnet balances  ·  {payload.get('base_url', FUTURES_BASE_URL)}"
    )
    table = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold cyan", expand=True)
    if venue == "futures":
        table.add_column("Asset", style="brand")
        table.add_column("Wallet", justify="right")
        table.add_column("Available", justify="right", style="ok")
        table.add_column("uPnL", justify="right")
        rows = payload.get("assets") or []
        if not rows:
            table.add_row("—", "0", "0", "0")
        else:
            for row in rows:
                table.add_row(
                    str(row.get("asset", "")),
                    str(row.get("wallet_balance", "0")),
                    str(row.get("available_balance", "0")),
                    str(row.get("unrealized_profit", "0")),
                )
    else:
        table.add_column("Asset", style="brand")
        table.add_column("Free", justify="right")
        table.add_column("Locked", justify="right")
        table.add_column("Total", justify="right", style="ok")
        rows = payload.get("balances") or []
        if not rows:
            table.add_row("—", "0", "0", "0")
        else:
            for row in rows:
                table.add_row(
                    str(row.get("asset", "")),
                    str(row.get("free", "0")),
                    str(row.get("locked", "0")),
                    str(row.get("total", "0")),
                )
    RUNTIME.console.print(table)
    flags = (
        f"environment={payload.get('environment')}  venue={payload.get('venue')}  "
        f"can_trade={payload.get('can_trade')}"
    )
    if venue == "futures":
        flags += (
            f"  wallet={payload.get('total_wallet_balance')}  "
            f"available={payload.get('available_balance')}"
        )
    RUNTIME.console.print(f"[muted]{flags}[/muted]")


def render_positions(payload: dict[str, Any]) -> None:
    rows = payload.get("positions") or []
    title = (
        f"Futures Testnet open positions  ·  {payload.get('base_url', FUTURES_BASE_URL)}"
    )
    table = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold cyan", expand=True)
    table.add_column("Symbol", style="brand")
    table.add_column("Side")
    table.add_column("Entry", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("uPnL", justify="right", style="ok")
    table.add_column("Mark", justify="right")
    table.add_column("Notional", justify="right")
    if not rows:
        table.add_row("—", "none", "0", "0", "0", "0", "0")
    else:
        for row in rows:
            table.add_row(
                str(row.get("symbol", "")),
                str(row.get("position_side") or row.get("direction") or "—"),
                str(row.get("entry_price", "0")),
                str(row.get("position_size", "0")),
                str(row.get("unrealized_pnl", "0")),
                str(row.get("mark_price", "—")),
                str(row.get("notional", "—")),
            )
    RUNTIME.console.print(table)
    RUNTIME.console.print(
        f"[muted]environment={payload.get('environment')}  venue={payload.get('venue')}  "
        f"read_only={payload.get('read_only')}  open_count={payload.get('open_count')}  "
        f"status={payload.get('status')}[/muted]"
    )


def render_workflow_plan(intent: WorkflowIntent) -> None:
    steps: list[str] = []
    index = 1
    if intent.check_spot_balance:
        steps.append(f"{index}. MCP [info]get_spot_testnet_balance[/info] (read-only)")
        index += 1
    if intent.check_futures_balance:
        steps.append(f"{index}. MCP [info]get_futures_testnet_balance[/info] (read-only)")
        index += 1
    if intent.check_futures_positions:
        steps.append(f"{index}. MCP [info]get_futures_testnet_positions[/info] (read-only)")
        index += 1
    if intent.analyze_symbol:
        steps.append(
            f"{index}. MCP [info]analyze_symbol[/info] [brand]{intent.analyze_symbol}[/brand]"
        )
        index += 1
    if intent.require_stable:
        steps.append(f"{index}. Local stability gate (fail-closed if unstable)")
        index += 1
    if intent.percent_of_available and intent.trade_side:
        steps.append(
            f"{index}. Local math: {intent.percent_of_available}% of available "
            f"{intent.percent_asset} → {intent.trade_side} {intent.trade_symbol}"
        )
        index += 1
    if intent.trade_side and intent.trade_symbol:
        preview_name = (
            "preview_futures_testnet_order"
            if intent.trade_venue == "futures"
            else "preview_spot_testnet_order"
        )
        steps.append(f"{index}. MCP [info]{preview_name}[/info] (dry-run, no fill)")
        index += 1
        steps.append(
            f"{index}. [warn]STOP[/warn] — Human-in-the-loop Y/N. "
            "Submit MCP tools are not called unless you type Y."
        )
    forbidden = ", ".join(sorted(_WORKFLOW_FORBIDDEN_TOOLS))
    body = "\n".join(steps) if steps else "No MCP steps planned."
    body += (
        f"\n\n[muted]Never auto-chained: {forbidden}. "
        "human_confirmed remains false until an explicit Y.[/muted]"
    )
    RUNTIME.console.print(
        Panel(
            body,
            title="[brand]AGENTIC WORKFLOW  ·  MCP ORCHESTRATION[/brand]",
            subtitle="[muted]Testnet only  ·  preview then HITL  ·  submit is never auto-chained[/muted]",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )


def render_preview(preview: dict[str, Any]) -> None:
    order = preview.get("order") or {}
    risk = preview.get("risk") or {}
    venue = str(preview.get("venue", "spot")).upper()
    summary = (
        f"[brand]Venue[/brand]     {venue} Testnet\n"
        f"[brand]Host[/brand]      {preview.get('base_url')}\n"
        f"[brand]Symbol[/brand]    {order.get('symbol')}\n"
        f"[brand]Side[/brand]      {order.get('side')}\n"
        f"[brand]Type[/brand]      {order.get('type')}\n"
        f"[brand]Quantity[/brand]  {order.get('quantity')}\n"
        f"[brand]Last[/brand]      {order.get('estimated_last_price')}\n"
        f"[brand]Notional[/brand]  {order.get('estimated_notional')} {order.get('quote_asset')}\n"
        f"[brand]Stop-loss[/brand] {order.get('stop_loss') or '—'}\n"
        f"[brand]Take-profit[/brand] {order.get('take_profit') or '—'}\n"
        f"[brand]Cap[/brand]       min({risk.get('max_exposure_fraction')} of portfolio, "
        f"{risk.get('max_notional_usdt')} USDT) → {risk.get('effective_cap_usdt')} USDT\n"
        f"[warn]Status[/warn]     {preview.get('status')}  ·  executed={preview.get('executed')}"
    )
    RUNTIME.console.print(
        Panel(
            summary,
            title="[warn]ORDER PREVIEW  ·  NO FILL[/warn]",
            subtitle="[muted]All irreversible actions require explicit user confirmation[/muted]",
            border_style="yellow",
            box=box.ROUNDED,
        )
    )
    RUNTIME.console.print(JSON.from_data(preview))


def render_submit_result(result: dict[str, Any]) -> None:
    order = result.get("order") or {}
    venue = str(result.get("venue", "spot")).upper()
    summary = (
        f"[ok]Submitted on {venue} Testnet only.[/ok]\n"
        f"symbol={order.get('symbol')}  side={order.get('side')}  "
        f"qty={order.get('quantity')}  sl={order.get('stop_loss') or '—'}  "
        f"tp={order.get('take_profit') or '—'}  "
        f"human_confirmed={result.get('human_confirmed')}"
    )
    if result.get("protection_error"):
        summary += f"\n[warn]Protection warning:[/warn] {result['protection_error']}"
    RUNTIME.console.print(
        Panel(
            summary,
            title=f"[ok]TESTNET {venue} ORDER SUBMITTED[/ok]",
            border_style="green",
            box=box.ROUNDED,
        )
    )
    RUNTIME.console.print(JSON.from_data(result))


def render_analysis(payload: dict[str, Any]) -> None:
    market = payload.get("market") or {}
    table = Table(
        title=f"Testnet market snapshot  ·  {payload.get('symbol')}",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Venue")
    table.add_column("Last", justify="right", style="ok")
    table.add_column("24h %", justify="right")
    table.add_column("High", justify="right")
    table.add_column("Low", justify="right")
    table.add_column("Quote volume", justify="right")
    for key in ("spot", "futures"):
        row = market.get(key)
        if not row:
            continue
        table.add_row(
            str(row.get("venue", key)),
            str(row.get("last_price", "—")),
            str(row.get("price_change_percent", "—")),
            str(row.get("high_price", "—")),
            str(row.get("low_price", "—")),
            str(row.get("quote_volume", "—")),
        )
    RUNTIME.console.print(table)
    body = payload.get("analysis") or (
        f"[warn]Gemini reading unavailable.[/warn] {payload.get('analysis_error') or 'No model output.'}"
    )
    RUNTIME.console.print(
        Panel(
            str(body),
            title=(
                f"[info]AI MARKET ANALYSIS  ·  {payload.get('symbol')}  ·  "
                f"{payload.get('model') or 'gemini'}  ·  TESTNET[/info]"
            ),
            subtitle=f"[muted]{payload.get('disclaimer')}[/muted]",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )


def render_history(lines: list[str]) -> None:
    if not lines:
        RUNTIME.console.print(
            f"[muted]No audit events yet. Log file: {AUDIT_LOG_PATH}[/muted]"
        )
        return
    table = Table(
        title=f"Audit history  ·  {AUDIT_LOG_PATH.name}  ·  last {len(lines)} events",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        expand=True,
        show_lines=False,
    )
    table.add_column("Time", style="muted", no_wrap=True, overflow="fold")
    table.add_column("Event", style="info", no_wrap=True)
    table.add_column("Detail")
    for line in lines:
        parts = [part.strip() for part in line.split("|", 2)]
        if len(parts) >= 3:
            table.add_row(parts[0], parts[1], parts[2])
        else:
            table.add_row("—", "RAW", line)
    RUNTIME.console.print(table)


async def prompt_line(label: str, default: str | None = None) -> str:
    kwargs: dict[str, Any] = {}
    if default is not None:
        kwargs["default"] = default
    return await asyncio.to_thread(Prompt.ask, label, **kwargs)


async def confirm_order(venue: str) -> bool:
    """Fail-closed Y/N. Only an exact Y proceeds. N, empty, and anything else abort."""
    label = (
        f"[bold yellow]Submit this {venue} Testnet order? "
        "Type Y to confirm or N to abort[/]"
    )
    answer = await prompt_line(label, default="N")
    confirmed = answer.strip().upper() in AFFIRMATIVE
    audit(
        "HITL_CONFIRMED" if confirmed else "HITL_DENIED",
        "Human confirmation " + ("accepted." if confirmed else "denied."),
        venue=venue.lower(),
        answer=answer.strip() or "N",
    )
    return confirmed


async def tool_balance(venue: str = "spot") -> dict[str, Any]:
    host = require_mcp()
    name = "get_futures_testnet_balance" if venue == "futures" else "get_spot_testnet_balance"
    with RUNTIME.console.status(f"[info]MCP tools/call {name}…[/info]"):
        payload = await host.call(name, {})
    render_balances(payload)
    return payload


async def tool_positions(symbol: str | None = None) -> dict[str, Any]:
    host = require_mcp()
    arguments: dict[str, Any] = {}
    if symbol:
        arguments["symbol"] = symbol
    with RUNTIME.console.status("[info]MCP tools/call get_futures_testnet_positions…[/info]"):
        payload = await host.call("get_futures_testnet_positions", arguments)
    render_positions(payload)
    return payload


async def tool_trade(intent: TradeIntent) -> None:
    get_kill_switch().raise_if_tripped()
    host = require_mcp()
    preview_name = (
        "preview_futures_testnet_order" if intent.venue == "futures" else "preview_spot_testnet_order"
    )
    submit_name = (
        "submit_futures_testnet_order" if intent.venue == "futures" else "submit_spot_testnet_order"
    )
    preview_args = {
        "symbol": intent.symbol,
        "side": intent.side,
        "quantity": intent.quantity,
        "stop_loss": intent.stop_loss,
        "take_profit": intent.take_profit,
    }
    with RUNTIME.console.status(f"[info]MCP tools/call {preview_name} (dry-run, no execution)…[/info]"):
        preview = await host.call(preview_name, preview_args)
    render_preview(preview)

    if preview.get("environment") != "testnet" or preview.get("status") != "order_preview":
        raise RuntimeError(
            "Preview is not a Testnet order_preview. Submit aborted (fail-closed)."
        )

    RUNTIME.console.print(
        "[warn]Human-in-the-loop:[/warn] no order will be sent unless you type [bold]Y[/bold]."
    )
    venue_label = "Futures" if intent.venue == "futures" else "Spot"
    if not await confirm_order(venue_label):
        render_success(
            "Aborted",
            "User declined confirmation. No Testnet order was sent. "
            "human_confirmed remains False. MCP submit tool was not called.",
        )
        return

    get_kill_switch().raise_if_tripped()
    order = preview.get("order") or {}
    submit_args = {
        "symbol": order.get("symbol", intent.symbol),
        "side": order.get("side", intent.side),
        "quantity": order.get("quantity", intent.quantity),
        "human_confirmed": True,
        "stop_loss": order.get("stop_loss", intent.stop_loss),
        "take_profit": order.get("take_profit", intent.take_profit),
    }
    with RUNTIME.console.status(
        f"[info]MCP tools/call {submit_name} (human_confirmed=true)…[/info]"
    ):
        result = await host.call(submit_name, submit_args)
    render_submit_result(result)


async def tool_analyze(symbol: str) -> dict[str, Any]:
    host = require_mcp()
    with RUNTIME.console.status(
        f"[info]MCP tools/call analyze_symbol for {symbol}…[/info]"
    ):
        payload = await host.call("analyze_symbol", {"symbol": symbol})
    render_analysis(payload)
    return payload


async def tool_workflow(intent: WorkflowIntent) -> None:
    """Execute a multi-step plan via MCP tools, then HITL. Never auto-submit."""
    get_kill_switch().raise_if_tripped()
    audit(
        "WORKFLOW_PLAN",
        "Executing agentic MCP workflow.",
        spot_balance=intent.check_spot_balance,
        futures_balance=intent.check_futures_balance,
        positions=intent.check_futures_positions,
        analyze=intent.analyze_symbol,
        require_stable=intent.require_stable,
        venue=intent.trade_venue,
        side=intent.trade_side,
        symbol=intent.trade_symbol,
        percent=intent.percent_of_available,
    )
    render_workflow_plan(intent)

    spot_balance: dict[str, Any] | None = None
    futures_balance: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None

    if intent.check_spot_balance:
        spot_balance = await tool_balance("spot")
    if intent.check_futures_balance:
        futures_balance = await tool_balance("futures")
    if intent.check_futures_positions:
        await tool_positions()

    analyze_symbol = intent.analyze_symbol or (intent.trade_symbol if intent.percent_of_available else None)
    if analyze_symbol:
        analysis = await tool_analyze(analyze_symbol)

    if intent.require_stable:
        stable, reason = assess_market_stability(analysis)
        audit("WORKFLOW_STEP", "Stability gate.", stable=stable, reason=reason)
        if not stable:
            render_warning(
                "Market stability gate",
                f"{reason}\nNo Testnet order was prepared. "
                "preview_* and submit_* MCP tools were not called.",
            )
            return
        render_success("Market looks stable", reason)

    if not intent.trade_side or not intent.trade_symbol:
        return

    quantity = await _workflow_quantity(intent, spot_balance, futures_balance, analysis)
    trade = TradeIntent(
        venue=intent.trade_venue or "spot",
        side=intent.trade_side,
        symbol=intent.trade_symbol,
        quantity=quantity,
        stop_loss=intent.stop_loss,
        take_profit=intent.take_profit,
    )
    # Reuses the existing preview → Y/N → submit path. The planner cannot
    # confirm an order or call a submit MCP tool on its own.
    await tool_trade(trade)


async def _workflow_quantity(
    intent: WorkflowIntent,
    spot_balance: dict[str, Any] | None,
    futures_balance: dict[str, Any] | None,
    analysis: dict[str, Any] | None,
) -> str:
    """Size a percent-of-available order locally. Fail-closed on missing data."""
    if not intent.percent_of_available:
        raise ValueError(
            "Compound workflow is missing an explicit quantity. "
            "Use a percent of available USDT or a simple buy/sell command."
        )
    venue = intent.trade_venue or "spot"
    if venue == "futures":
        if futures_balance is None:
            futures_balance = await tool_balance("futures")
        available = available_quote(futures_balance, intent.percent_asset)
    else:
        if spot_balance is None:
            spot_balance = await tool_balance("spot")
        available = available_quote(spot_balance, intent.percent_asset)

    last_price = last_price_from_analysis(analysis, venue=venue)
    if last_price <= 0:
        if intent.trade_symbol:
            analysis = await tool_analyze(intent.trade_symbol)
            last_price = last_price_from_analysis(analysis, venue=venue)
    if last_price <= 0:
        raise RuntimeError(
            "Cannot size a percent order without a positive Testnet last price. "
            "No preview was built and no order was sent."
        )

    sized = quantity_from_percent(
        available_usdt=available,
        percent=Decimal(str(intent.percent_of_available)),
        last_price=last_price,
    )
    quantity_dec = Decimal(str(sized["quantity"]))
    notional_dec = Decimal(str(sized["notional_usdt"]))
    quantity = format_quantity(quantity_dec)
    audit(
        "WORKFLOW_STEP",
        "Computed percent-of-available size.",
        percent=intent.percent_of_available,
        available=format(available, "f"),
        last_price=format(last_price, "f"),
        quantity=quantity,
        notional=format(notional_dec, "f"),
        capped=sized["capped"],
    )
    note = (
        f"{intent.percent_of_available}% of available {intent.percent_asset} "
        f"{format(available, 'f')} @ last {format(last_price, 'f')} → "
        f"qty {quantity} (notional {format(notional_dec, 'f')} USDT)."
    )
    if sized["capped"]:
        render_warning(
            "Size reduced to risk cap",
            note + f"\nEffective ceiling is min({format(MAX_EXPOSURE_FRACTION * 100, 'f')}% of "
            f"available, {format(MAX_NOTIONAL_USDT, 'f')} USDT).",
        )
    else:
        render_success("Sized from available USDT", note)
    return quantity


async def tool_history(limit: int) -> None:
    host = require_mcp()
    payload = await host.call("read_audit_history", {"limit": limit})
    render_history(list(payload.get("events") or []))


async def tool_kill() -> None:
    host = RUNTIME.mcp
    if host is not None:
        try:
            await host.call("trip_kill_switch", {"reason": "CLI kill command"})
        except KillSwitchActivated:
            pass
        except Exception:
            get_kill_switch().trip(reason="CLI kill command")
    else:
        get_kill_switch().trip(reason="CLI kill command")
    render_kill_switch()
    raise KillSwitchActivated("Emergency kill-switch activated.")


async def process_intent(user_input: str) -> None:
    """Route an utterance to local MCP tools/call (never raw REST).

    Simple keywords map 1:1. Compound natural language is orchestrated as a
    chained MCP workflow that always stops on a dry-run preview for HITL.
    """
    get_kill_switch().raise_if_tripped()
    text = user_input.strip()
    if not text:
        return

    lowered = text.lower()
    if lowered in EXIT_COMMANDS:
        raise KeyboardInterrupt
    if lowered in KILL_COMMANDS:
        await tool_kill()
        return
    if lowered in HELP_COMMANDS:
        render_help()
        return

    history_limit = parse_history_limit(text)
    if history_limit is not None:
        await tool_history(history_limit)
        return

    if looks_like_compound_workflow(text):
        workflow = parse_workflow_intent(text)
        if workflow is None:
            with RUNTIME.console.status("[info]Planning multi-step MCP workflow…[/info]"):
                workflow = await asyncio.to_thread(llm_plan_workflow, text)
        if workflow is not None:
            await tool_workflow(workflow)
            return
        render_warning(
            "Unable to plan a safe Testnet workflow",
            "The utterance looks multi-step, but SentinelOS could not build an "
            "allowlisted MCP plan. No order was sent. Try: "
            "[info]Check my spot balance, analyze ETHUSDT, and if the market looks "
            "stable, prepare a spot order to buy using 5% of my available USDT.[/info]",
        )
        return

    positions = parse_positions_intent(text)
    if positions is not None:
        await tool_positions(positions.symbol)
        return

    analyze = parse_analyze_symbol(text)
    if analyze is not None:
        await tool_analyze(analyze)
        return

    if FUTURES_BALANCE_PATTERN.match(text):
        await tool_balance("futures")
        return

    trade = parse_trade_intent(text)
    if trade is not None:
        await tool_trade(trade)
        return

    if BALANCE_PATTERN.search(text) and parse_trade_intent(text) is None:
        await tool_balance("spot")
        return

    RUNTIME.console.print(
        "[warn]Intent not recognized.[/warn] Type [info]help[/info] for the tool list. "
        "Examples: [info]analyze BTCUSDT[/info], "
        "[info]buy BTCUSDT 0.001 --sl 58000 --tp 62000[/info], "
        "[info]futures buy BTCUSDT 0.001[/info], "
        "[info]positions[/info], [info]history[/info], [info]kill[/info]."
    )


async def main_loop() -> None:
    render_banner()
    client = build_mcp_client()
    async with client:
        host = SentinelMcpHost(client)
        await bootstrap_mcp(host)
        RUNTIME.console.print(
            "[muted]Listening over MCP. Ctrl+C, 'exit', or 'kill' to quit. All trades stay on Testnet.[/muted]"
        )

        while True:
            get_kill_switch().raise_if_tripped()
            try:
                user_input = await prompt_line("[bold cyan]sentinel[/bold cyan]")
            except (EOFError, KeyboardInterrupt):
                RUNTIME.console.print("\n[muted]Session closed.[/muted]")
                _close_runtime()
                return

            try:
                await process_intent(user_input)
            except KillSwitchActivated:
                _close_runtime()
                return
            except KeyboardInterrupt:
                RUNTIME.console.print("\n[muted]Session closed.[/muted]")
                _close_runtime()
                return
            except (ValidationError, RuntimeError, PermissionError, ValueError, OSError, httpx.HTTPError) as exc:
                handle_exception(exc)
            except Exception as exc:  # noqa: BLE001 — CLI must not crash on connector faults
                handle_exception(exc)


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main_loop())
    except KillSwitchActivated:
        CONSOLE.print("\n[bold green]SentinelOS shutting down securely...[/bold green]")
    except KeyboardInterrupt:
        CONSOLE.print("\n[muted]Session closed.[/muted]")
    finally:
        _close_runtime()


if __name__ == "__main__":
    main()
