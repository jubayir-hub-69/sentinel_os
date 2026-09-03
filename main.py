"""SentinelOS application entry point.

Boots the asynchronous CLI agent under a fail-closed Testnet banner so
judges see safety status before any tool is invoked.
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from rich.panel import Panel

from agent import main_loop
from core.kill_switch import KillSwitchActivated

CONSOLE = Console()

STARTUP_BODY = (
    "[bold white]SentinelOS Initialized.[/bold white]\n"
    "Architecture: [bold cyan]Binance Agent OS & MCP (Model Context Protocol)[/bold cyan]\n"
    "Status: [bold cyan]Autonomous Guardian Active[/bold cyan]\n"
    "Environment: [bold green]Strictly Binance Testnet (Spot + USDⓈ-M Futures)[/bold green]\n"
    "Safety Protocol: [bold yellow]Fail-Closed & Human-in-the-Loop Enforced.[/bold yellow]\n"
    "Risk: [bold yellow]10% portfolio cap · $1000 notional ceiling · Kill-switch armed[/bold yellow]"
)


def _print_startup_banner() -> None:
    CONSOLE.print()
    CONSOLE.print(
        Panel.fit(
            STARTUP_BODY,
            title="[bold green]Testnet Mode Active[/bold green]",
            subtitle="[dim]Binance Agent OS & MCP  ·  Track A  ·  Production hosts blocked[/dim]",
            border_style="green",
            padding=(1, 4),
        )
    )
    CONSOLE.print()


async def start() -> None:
    """Display the Testnet safety banner, then run the agent loop."""
    _print_startup_banner()
    try:
        await main_loop()
    except (KeyboardInterrupt, KillSwitchActivated):
        pass
    CONSOLE.print(
        "\n[bold green]SentinelOS shutting down securely...[/bold green]"
    )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(start())
    except (KeyboardInterrupt, KillSwitchActivated):
        CONSOLE.print(
            "\n[bold green]SentinelOS shutting down securely...[/bold green]"
        )
