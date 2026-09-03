"""Protocol tests for the official SentinelOS MCP server.

These tests speak MCP through ``mcp.Client`` — not a fake wrapper class.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REQUIRED_TOOLS = {
    "get_spot_testnet_balance",
    "get_futures_testnet_balance",
    "preview_spot_testnet_order",
    "submit_spot_testnet_order",
    "preview_futures_testnet_order",
    "submit_futures_testnet_order",
    "analyze_symbol",
    "read_audit_history",
    "trip_kill_switch",
    "discover_official_binance_mcp",
}


async def _with_client(fn):
    from mcp import Client

    from mcp_server import mcp

    async with Client(mcp) as client:
        return await fn(client)


async def test_mcp_lists_trade_tools() -> None:
    async def inner(client):
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        missing = REQUIRED_TOOLS - names
        assert not missing, f"MCP server is missing tools: {missing}"
        submit = next(tool for tool in listed.tools if tool.name == "submit_futures_testnet_order")
        schema = submit.input_schema or {}
        props = schema.get("properties") or {}
        assert "human_confirmed" in props
        assert "symbol" in props

    await _with_client(inner)


async def test_submit_without_hitl_is_denied_over_mcp() -> None:
    async def inner(client):
        result = await client.call_tool(
            "submit_spot_testnet_order",
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "quantity": "0.001",
                "human_confirmed": False,
            },
        )
        assert result.is_error is True
        text = " ".join(getattr(block, "text", "") for block in result.content)
        assert "human" in text.lower() or "confirmation" in text.lower()
        assert result.structured_content is None

        futures = await client.call_tool(
            "submit_futures_testnet_order",
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "quantity": "0.005",
            },
        )
        assert futures.is_error is True

    await _with_client(inner)


async def test_discovery_and_history_are_testnet() -> None:
    async def inner(client):
        discovered = await client.call_tool("discover_official_binance_mcp", {})
        assert discovered.is_error is False
        payload = discovered.structured_content
        assert payload["environment"] == "testnet"
        assert payload["status"] == "discovery_only"
        assert "agent.binance.com/mcp/agentic" in payload["official_mcp"]
        assert "local" in payload["policy"].lower() or "Testnet" in payload["policy"]

        history = await client.call_tool("read_audit_history", {"limit": 5})
        assert history.is_error is False
        assert history.structured_content["environment"] == "testnet"
        assert history.structured_content["status"] == "audit_history"

    await _with_client(inner)


async def test_mcp_prompt_forbids_unsigned_confirm() -> None:
    async def inner(client):
        listed = await client.list_prompts()
        names = {prompt.name for prompt in listed.prompts}
        assert "confirm_testnet_order" in names
        rendered = await client.get_prompt(
            "confirm_testnet_order",
            {"venue": "futures", "symbol": "BTCUSDT", "side": "BUY", "quantity": "0.005"},
        )
        text = " ".join(
            getattr(message.content, "text", str(message.content)) for message in rendered.messages
        )
        assert "Y" in text
        assert "human_confirmed" in text

    await _with_client(inner)


def test_agent_does_not_import_rest_wrappers() -> None:
    source = (ROOT / "agent.py").read_text(encoding="utf-8")
    forbidden = (
        "from tools.binance_testnet_tools import",
        "from tools.binance_futures_testnet_tools import",
        "from tools.market_analysis import",
        "create_spot_testnet_client",
        "create_futures_testnet_client",
    )
    for needle in forbidden:
        assert needle not in source, f"agent.py still imports REST wrapper: {needle}"
    assert "from core.mcp_host import" in source
    assert "host.call(" in source


if __name__ == "__main__":
    tests = [
        test_mcp_lists_trade_tools,
        test_submit_without_hitl_is_denied_over_mcp,
        test_discovery_and_history_are_testnet,
        test_mcp_prompt_forbids_unsigned_confirm,
        test_agent_does_not_import_rest_wrappers,
    ]
    for test in tests:
        if asyncio.iscoroutinefunction(test):
            asyncio.run(test())
        else:
            test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} passed")
