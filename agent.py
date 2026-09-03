"""SentinelOS conversational orchestrator (Track A).

Fail-closed Spot Testnet CLI. Keyword routing simulates LLM function calling
until a live model key is configured. State-changing orders always require
an explicit Y confirmation after a dry-run preview.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from rich import box
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.theme import Theme
from rich.traceback import install as install_rich_traceback

from core.config import SPOT_BASE_URL, get_settings
from policies.testnet_only import verify_testnet_environment
from tools.binance_testnet_tools import (
    MAX_EXPOSURE_FRACTION,
    create_spot_testnet_client,
    get_spot_testnet_balance,
    preview_spot_testnet_order,
    submit_spot_testnet_order,
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

TRADE_PATTERN = re.compile(
    r"^\s*(buy|sell)\s+([A-Za-z0-9/_-]+)\s+([0-9]+(?:\.[0-9]+)?)\s*$",
    re.IGNORECASE,
)
BALANCE_PATTERN = re.compile(
    r"\b(balance|balances|account|portfolio|wallet)\b",
    re.IGNORECASE,
)
EXIT_COMMANDS = frozenset({"exit", "quit", "q", ":q"})
HELP_COMMANDS = frozenset({"help", "h", "?", "commands"})
AFFIRMATIVE = frozenset({"Y"})


@dataclass
class AgentRuntime:
    """Process-wide CLI runtime. Client is created only after Testnet checks pass."""

    console: Console
    client: Any = None
    ready: bool = False


RUNTIME = AgentRuntime(console=CONSOLE)


def render_banner() -> None:
    console = RUNTIME.console
    body = (
        "[brand]SentinelOS[/brand]  ·  Binance Agent OS Hackathon  ·  Track A\n"
        "[ok]Environment: TESTNET[/ok]  ·  Venue: Spot  ·  Host: "
        f"[info]{SPOT_BASE_URL}[/info]\n"
        "[muted]Fail-closed production block  ·  Human confirmation required  ·  "
        f"Max exposure {format(MAX_EXPOSURE_FRACTION * 100, 'f')}% per trade[/muted]\n\n"
        "Type [info]help[/info] for commands, [info]balance[/info] for Testnet balances, "
        "or [info]buy BTCUSDT 0.001[/info] / [info]sell ETHUSDT 0.01[/info] to preview an order."
    )
    console.print(
        Panel(
            body,
            title="[brand]SENTINEL OS[/brand]",
            subtitle="[muted]Dry-Run / Testnet only[/muted]",
            border_style="cyan",
            box=box.DOUBLE,
        )
    )


def render_help() -> None:
    table = Table(
        title="Function-calling tools (Spot Testnet)",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Utterance", style="info", no_wrap=True)
    table.add_column("Tool")
    table.add_column("Notes")
    table.add_row(
        "balance",
        "get_spot_testnet_balance",
        "Read-only account snapshot",
    )
    table.add_row(
        "buy SYMBOL QTY",
        "preview_spot_testnet_order → submit_spot_testnet_order",
        "Preview first; submit only after Y",
    )
    table.add_row(
        "sell SYMBOL QTY",
        "preview_spot_testnet_order → submit_spot_testnet_order",
        "Preview first; submit only after Y",
    )
    table.add_row("help", "—", "Show this table")
    table.add_row("exit", "—", "Leave the agent")
    RUNTIME.console.print(table)
    RUNTIME.console.print(
        "[muted]Production hosts are rejected with RuntimeError. "
        "Unconfirmed orders are treated as denial.[/muted]"
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


def handle_exception(exc: BaseException) -> None:
    """Surface policy, config, and connector failures as rich warnings."""
    message = str(exc).strip() or exc.__class__.__name__
    if isinstance(exc, ValidationError):
        render_warning(
            "Missing or invalid configuration",
            "Could not load Testnet settings. Copy [info].env.example[/info] to "
            "[info].env[/info] and set Spot Testnet API key/secret.\n\n"
            f"[muted]{message}[/muted]",
        )
        return
    if isinstance(exc, PermissionError):
        render_warning("Authorization / risk guardrail", message)
        return
    if isinstance(exc, RuntimeError):
        render_warning("Testnet policy block", message)
        return
    if isinstance(exc, ValueError):
        render_warning("Invalid order parameters", message)
        return
    render_error(exc.__class__.__name__, message)


def bootstrap_runtime() -> None:
    """Load fail-closed settings and a pinned Spot Testnet client."""
    try:
        settings = get_settings()
        verify_testnet_environment(settings.spot_base_url)
        settings.require_testnet_endpoint(settings.spot_base_url)
        RUNTIME.client = create_spot_testnet_client()
        RUNTIME.ready = True
        render_success(
            "Testnet client ready",
            f"Spot connector pinned to [info]{settings.spot_base_url}[/info]. "
            f"BINANCE_ENV=[ok]{settings.binance_env}[/ok].",
        )
    except (ValidationError, RuntimeError, PermissionError, OSError, ValueError) as exc:
        RUNTIME.client = None
        RUNTIME.ready = False
        handle_exception(exc)
        render_warning(
            "Degraded mode",
            "The CLI is up, but Binance tools will fail until Testnet configuration is valid. "
            "No production endpoint will be used.",
        )


def require_client() -> Any:
    if RUNTIME.client is None:
        bootstrap_runtime()
    if RUNTIME.client is None:
        raise RuntimeError(
            "Spot Testnet client is not available. Check BINANCE_ENV=testnet and "
            "BINANCE_SPOT_TESTNET_API_KEY / BINANCE_SPOT_TESTNET_API_SECRET in .env."
        )
    return RUNTIME.client


def render_balances(payload: dict[str, Any]) -> None:
    table = Table(
        title=f"Spot Testnet balances  ·  {payload.get('base_url', SPOT_BASE_URL)}",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Asset", style="brand")
    table.add_column("Free", justify="right")
    table.add_column("Locked", justify="right")
    table.add_column("Total", justify="right", style="ok")

    balances = payload.get("balances") or []
    if not balances:
        table.add_row("—", "0", "0", "0")
    else:
        for row in balances:
            table.add_row(
                str(row.get("asset", "")),
                str(row.get("free", "0")),
                str(row.get("locked", "0")),
                str(row.get("total", "0")),
            )

    RUNTIME.console.print(table)
    flags = (
        f"environment={payload.get('environment')}  "
        f"venue={payload.get('venue')}  "
        f"can_trade={payload.get('can_trade')}  "
        f"account_type={payload.get('account_type')}"
    )
    RUNTIME.console.print(f"[muted]{flags}[/muted]")


def render_preview(preview: dict[str, Any]) -> None:
    order = preview.get("order") or {}
    risk = preview.get("risk") or {}
    summary = (
        f"[brand]Venue[/brand]     Spot Testnet\n"
        f"[brand]Host[/brand]      {preview.get('base_url', SPOT_BASE_URL)}\n"
        f"[brand]Symbol[/brand]    {order.get('symbol')}\n"
        f"[brand]Side[/brand]      {order.get('side')}\n"
        f"[brand]Type[/brand]      {order.get('type')}\n"
        f"[brand]Quantity[/brand]  {order.get('quantity')}\n"
        f"[brand]Quote[/brand]     {order.get('quote_asset')}\n"
        f"[brand]Cap[/brand]       {risk.get('max_exposure_fraction')} of portfolio "
        f"(see {risk.get('policy')})\n"
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
    summary = (
        f"[ok]Submitted on Spot Testnet only.[/ok]\n"
        f"symbol={order.get('symbol')}  side={order.get('side')}  "
        f"qty={order.get('quantity')}  human_confirmed={result.get('human_confirmed')}"
    )
    RUNTIME.console.print(
        Panel(
            summary,
            title="[ok]TESTNET ORDER SUBMITTED[/ok]",
            border_style="green",
            box=box.ROUNDED,
        )
    )
    RUNTIME.console.print(JSON.from_data(result))


async def prompt_line(label: str, default: str | None = None) -> str:
    kwargs: dict[str, Any] = {}
    if default is not None:
        kwargs["default"] = default
    return await asyncio.to_thread(Prompt.ask, label, **kwargs)


async def confirm_order() -> bool:
    """Fail-closed Y/N. Only an exact Y proceeds. N, empty, and anything else abort."""
    answer = await prompt_line(
        "[bold yellow]Submit this Spot Testnet order? Type Y to confirm or N to abort[/]",
        default="N",
    )
    return answer.strip().upper() in AFFIRMATIVE


async def tool_balance() -> None:
    client = require_client()
    with RUNTIME.console.status("[info]Calling get_spot_testnet_balance on Testnet…[/info]"):
        payload = await asyncio.to_thread(get_spot_testnet_balance, client)
    render_balances(payload)


async def tool_trade(side: str, symbol: str, quantity: str) -> None:
    with RUNTIME.console.status("[info]Building order preview (dry-run, no execution)…[/info]"):
        preview = await asyncio.to_thread(preview_spot_testnet_order, symbol, side, quantity)
    render_preview(preview)

    if preview.get("environment") != "testnet" or preview.get("status") != "order_preview":
        raise RuntimeError(
            "Preview is not a Testnet order_preview. Submit aborted (fail-closed)."
        )

    RUNTIME.console.print(
        "[warn]Human-in-the-loop:[/warn] no order will be sent unless you type [bold]Y[/bold]."
    )
    if not await confirm_order():
        render_success(
            "Aborted",
            "User declined confirmation. No Testnet order was sent. "
            "human_confirmed remains False.",
        )
        return

    client = require_client()
    order = preview.get("order") or {}
    with RUNTIME.console.status("[info]Calling submit_spot_testnet_order (human_confirmed=True)…[/info]"):
        result = await asyncio.to_thread(
            submit_spot_testnet_order,
            client,
            order.get("symbol", symbol),
            order.get("side", side),
            order.get("quantity", quantity),
            human_confirmed=True,
        )
    render_submit_result(result)


async def process_intent(user_input: str) -> None:
    """Mock LLM router: keyword match → Binance Testnet tool call.

    Replace this matcher with a live function-calling model later. The tool
    surface stays identical: balance, preview, confirm, submit.
    """
    text = user_input.strip()
    if not text:
        return

    lowered = text.lower()
    if lowered in EXIT_COMMANDS:
        raise KeyboardInterrupt
    if lowered in HELP_COMMANDS:
        render_help()
        return

    trade = TRADE_PATTERN.match(text)
    if trade is not None:
        side, symbol, quantity = trade.group(1), trade.group(2), trade.group(3)
        await tool_trade(side.upper(), symbol, quantity)
        return

    if BALANCE_PATTERN.search(text) and TRADE_PATTERN.search(text) is None:
        await tool_balance()
        return

    RUNTIME.console.print(
        "[warn]Intent not recognized.[/warn] Simulated LLM routing only maps "
        "[info]balance[/info], [info]buy SYMBOL AMOUNT[/info], and "
        "[info]sell SYMBOL AMOUNT[/info]. Type [info]help[/info] for the tool list."
    )


async def main_loop() -> None:
    render_banner()
    bootstrap_runtime()
    RUNTIME.console.print(
        "[muted]Listening. Ctrl+C or 'exit' to quit. All trades stay on Testnet.[/muted]"
    )

    while True:
        try:
            user_input = await prompt_line("[bold cyan]sentinel[/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            RUNTIME.console.print("\n[muted]Session closed.[/muted]")
            return

        try:
            await process_intent(user_input)
        except KeyboardInterrupt:
            RUNTIME.console.print("\n[muted]Session closed.[/muted]")
            return
        except (ValidationError, RuntimeError, PermissionError, ValueError, OSError) as exc:
            handle_exception(exc)
        except Exception as exc:  # noqa: BLE001 — CLI must not crash on connector faults
            handle_exception(exc)


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        CONSOLE.print("\n[muted]Session closed.[/muted]")


if __name__ == "__main__":
    main()
