"""SentinelOS conversational orchestrator (Track A).

Fail-closed Spot + USDⓈ-M Futures Testnet CLI built on Binance Agent OS
and MCP (Model Context Protocol). Keyword routing maps utterances to local
tools. State-changing orders always require an explicit Y confirmation
after a dry-run preview.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
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

from core.audit import AUDIT_LOG_PATH, audit, read_recent
from core.cli_parse import (
    AFFIRMATIVE,
    BALANCE_PATTERN,
    EXIT_COMMANDS,
    FUTURES_BALANCE_PATTERN,
    HELP_COMMANDS,
    KILL_COMMANDS,
    TradeIntent,
    parse_analyze_symbol,
    parse_history_limit,
    parse_trade_intent,
)
from core.config import FUTURES_BASE_URL, SPOT_BASE_URL, get_settings
from core.kill_switch import KillSwitchActivated, get_kill_switch
from core.risk import MAX_EXPOSURE_FRACTION, MAX_NOTIONAL_USDT
from policies.testnet_only import verify_testnet_environment
from tools.binance_futures_testnet_tools import (
    FuturesTestnetClient,
    create_futures_testnet_client,
    get_futures_testnet_balance,
    preview_futures_testnet_order,
    submit_futures_testnet_order,
)
from tools.binance_testnet_tools import (
    create_spot_testnet_client,
    get_spot_testnet_balance,
    preview_spot_testnet_order,
    submit_spot_testnet_order,
)
from tools.market_analysis import analyze_symbol

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
    """Process-wide CLI runtime. Clients are created only after Testnet checks pass."""

    console: Console
    client: Any = None
    futures_client: FuturesTestnetClient | None = None
    ready: bool = False


RUNTIME = AgentRuntime(console=CONSOLE)


def render_banner() -> None:
    console = RUNTIME.console
    body = (
        "[brand]SentinelOS[/brand]  ·  Binance Agent OS & MCP (Model Context Protocol)  ·  Track A\n"
        "[ok]Environment: TESTNET[/ok]  ·  Spot: "
        f"[info]{SPOT_BASE_URL}[/info]  ·  Futures: [info]{FUTURES_BASE_URL}[/info]\n"
        "[muted]Fail-closed production block  ·  Human-in-the-loop required  ·  "
        f"Max {format(MAX_EXPOSURE_FRACTION * 100, 'f')}% of portfolio or "
        f"${format(MAX_NOTIONAL_USDT, 'f')} notional  ·  Kill-switch armed[/muted]\n\n"
        "Type [info]help[/info]  ·  [info]balance[/info]  ·  [info]analyze BTCUSDT[/info]  ·  "
        "[info]buy BTCUSDT 0.001 --sl 58000 --tp 62000[/info]  ·  "
        "[info]futures buy BTCUSDT 0.001[/info]  ·  [info]history[/info]  ·  [info]kill[/info]"
    )
    console.print(
        Panel(
            body,
            title="[brand]SENTINEL OS  ·  AGENT OS / MCP[/brand]",
            subtitle="[muted]Dry-Run / Testnet only  ·  MCP: agent.binance.com/mcp/agentic[/muted]",
            border_style="cyan",
            box=box.DOUBLE,
        )
    )


def render_help() -> None:
    table = Table(
        title="Function-calling tools (Binance Agent OS & MCP)",
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
    table.add_row("history [N]", "audit_log.txt", "Show the latest N audit events")
    table.add_row("kill", "kill_switch.trip", "Abort pending work and shut down")
    table.add_row("help", "—", "Show this table")
    table.add_row("exit", "—", "Leave the agent")
    RUNTIME.console.print(table)
    RUNTIME.console.print(
        "[muted]Architecture: Binance Agent OS & MCP (Model Context Protocol). "
        "Production hosts are rejected with RuntimeError. "
        "Unconfirmed orders are treated as denial. "
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


def _close_clients() -> None:
    if RUNTIME.futures_client is not None:
        try:
            RUNTIME.futures_client.close()
        except Exception:
            pass
        RUNTIME.futures_client = None
    if RUNTIME.client is not None:
        session = getattr(RUNTIME.client, "session", None)
        closer = getattr(session, "close", None) if session is not None else None
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        RUNTIME.client = None
    RUNTIME.ready = False


def bootstrap_runtime() -> None:
    """Load fail-closed settings and pinned Testnet clients."""
    switch = get_kill_switch()
    switch.raise_if_tripped()
    switch.register_shutdown_hook(_close_clients)
    try:
        settings = get_settings()
        verify_testnet_environment(settings.spot_base_url)
        verify_testnet_environment(settings.futures_base_url)
        settings.require_testnet_endpoint(settings.spot_base_url)
        settings.require_testnet_endpoint(settings.futures_base_url)
        RUNTIME.client = create_spot_testnet_client()
        RUNTIME.futures_client = create_futures_testnet_client()
        RUNTIME.ready = True
        render_success(
            "Testnet clients ready",
            f"Spot pinned to [info]{settings.spot_base_url}[/info]. "
            f"Futures pinned to [info]{settings.futures_base_url}[/info]. "
            f"BINANCE_ENV=[ok]{settings.binance_env}[/ok]. "
            f"Gemini=[info]{settings.llm_model}[/info] (dynamic flash catalog).",
        )
        audit(
            "BOOT",
            "Testnet clients ready.",
            spot=settings.spot_base_url,
            futures=settings.futures_base_url,
        )
    except (ValidationError, RuntimeError, PermissionError, OSError, ValueError) as exc:
        _close_clients()
        handle_exception(exc)
        render_warning(
            "Degraded mode",
            "The CLI is up, but Binance tools will fail until Testnet configuration is valid. "
            "No production endpoint will be used.",
        )


def require_spot_client() -> Any:
    get_kill_switch().raise_if_tripped()
    if RUNTIME.client is None:
        bootstrap_runtime()
    if RUNTIME.client is None:
        raise RuntimeError(
            "Spot Testnet client is not available. Check BINANCE_ENV=testnet and "
            "BINANCE_SPOT_TESTNET_API_KEY / BINANCE_SPOT_TESTNET_API_SECRET in .env."
        )
    return RUNTIME.client


def require_futures_client() -> FuturesTestnetClient:
    get_kill_switch().raise_if_tripped()
    if RUNTIME.futures_client is None:
        bootstrap_runtime()
    if RUNTIME.futures_client is None:
        raise RuntimeError(
            "Futures Testnet client is not available. Check BINANCE_ENV=testnet and "
            "BINANCE_FUTURES_TESTNET_API_KEY / BINANCE_FUTURES_TESTNET_API_SECRET in .env."
        )
    return RUNTIME.futures_client


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


async def tool_balance(venue: str = "spot") -> None:
    if venue == "futures":
        client = require_futures_client()
        with RUNTIME.console.status("[info]Calling get_futures_testnet_balance on Testnet…[/info]"):
            payload = await asyncio.to_thread(get_futures_testnet_balance, client)
    else:
        client = require_spot_client()
        with RUNTIME.console.status("[info]Calling get_spot_testnet_balance on Testnet…[/info]"):
            payload = await asyncio.to_thread(get_spot_testnet_balance, client)
    render_balances(payload)


async def tool_trade(intent: TradeIntent) -> None:
    get_kill_switch().raise_if_tripped()
    if intent.venue == "futures":
        with RUNTIME.console.status("[info]Building Futures order preview (dry-run, no execution)…[/info]"):
            preview = await asyncio.to_thread(
                preview_futures_testnet_order,
                intent.symbol,
                intent.side,
                intent.quantity,
                intent.stop_loss,
                intent.take_profit,
                require_futures_client(),
            )
    else:
        with RUNTIME.console.status("[info]Building Spot order preview (dry-run, no execution)…[/info]"):
            preview = await asyncio.to_thread(
                preview_spot_testnet_order,
                intent.symbol,
                intent.side,
                intent.quantity,
                intent.stop_loss,
                intent.take_profit,
                require_spot_client(),
            )
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
            "human_confirmed remains False.",
        )
        return

    get_kill_switch().raise_if_tripped()
    order = preview.get("order") or {}
    if intent.venue == "futures":
        client = require_futures_client()
        with RUNTIME.console.status("[info]Calling submit_futures_testnet_order (human_confirmed=True)…[/info]"):
            result = await asyncio.to_thread(
                submit_futures_testnet_order,
                client,
                order.get("symbol", intent.symbol),
                order.get("side", intent.side),
                order.get("quantity", intent.quantity),
                True,
                order.get("stop_loss", intent.stop_loss),
                order.get("take_profit", intent.take_profit),
            )
    else:
        client = require_spot_client()
        with RUNTIME.console.status("[info]Calling submit_spot_testnet_order (human_confirmed=True)…[/info]"):
            result = await asyncio.to_thread(
                submit_spot_testnet_order,
                client,
                order.get("symbol", intent.symbol),
                order.get("side", intent.side),
                order.get("quantity", intent.quantity),
                True,
                order.get("stop_loss", intent.stop_loss),
                order.get("take_profit", intent.take_profit),
            )
    render_submit_result(result)


async def tool_analyze(symbol: str) -> None:
    with RUNTIME.console.status(f"[info]Fetching Testnet market data and running Gemini analysis for {symbol}…[/info]"):
        payload = await asyncio.to_thread(analyze_symbol, symbol)
    render_analysis(payload)


async def tool_history(limit: int) -> None:
    lines = await asyncio.to_thread(read_recent, limit)
    render_history(lines)


def tool_kill() -> None:
    get_kill_switch().trip(reason="CLI kill command")
    render_kill_switch()
    raise KillSwitchActivated("Emergency kill-switch activated.")


async def process_intent(user_input: str) -> None:
    """Keyword router: utterance → local Binance Testnet tool call."""
    get_kill_switch().raise_if_tripped()
    text = user_input.strip()
    if not text:
        return

    lowered = text.lower()
    if lowered in EXIT_COMMANDS:
        raise KeyboardInterrupt
    if lowered in KILL_COMMANDS:
        tool_kill()
        return
    if lowered in HELP_COMMANDS:
        render_help()
        return

    history_limit = parse_history_limit(text)
    if history_limit is not None:
        await tool_history(history_limit)
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
        "[info]futures buy BTCUSDT 0.001[/info], [info]history[/info], [info]kill[/info]."
    )


async def main_loop() -> None:
    render_banner()
    bootstrap_runtime()
    RUNTIME.console.print(
        "[muted]Listening. Ctrl+C, 'exit', or 'kill' to quit. All trades stay on Testnet.[/muted]"
    )

    while True:
        get_kill_switch().raise_if_tripped()
        try:
            user_input = await prompt_line("[bold cyan]sentinel[/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            RUNTIME.console.print("\n[muted]Session closed.[/muted]")
            _close_clients()
            return

        try:
            await process_intent(user_input)
        except KillSwitchActivated:
            _close_clients()
            return
        except KeyboardInterrupt:
            RUNTIME.console.print("\n[muted]Session closed.[/muted]")
            _close_clients()
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
        _close_clients()


if __name__ == "__main__":
    main()
