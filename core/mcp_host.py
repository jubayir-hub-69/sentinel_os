"""Official MCP client host for SentinelOS.

The CLI agent must not call Binance REST wrappers directly. It speaks
JSON-RPC Model Context Protocol through the official ``mcp.Client`` to
the local ``sentinelos-testnet-mcp`` server.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.types import TextContent, TextResourceContents

from core.audit import audit
from core.kill_switch import KillSwitchActivated

ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = ROOT / "mcp_server.py"


def build_mcp_client() -> Client:
    """Connect to the local SentinelOS MCP server.

    Default: in-process ``Client(mcp)`` — still the real MCP protocol
    (list/validate/invoke), as documented by the official Python SDK.

    Set ``SENTINEL_MCP_TRANSPORT=stdio`` to spawn ``mcp_server.py`` as a
    subprocess and speak newline-delimited JSON-RPC on stdin/stdout.
    """
    transport = os.environ.get("SENTINEL_MCP_TRANSPORT", "inprocess").strip().lower()
    if transport in {"stdio", "subprocess", "jsonrpc"}:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_PATH)],
            cwd=str(ROOT),
            env={key: value for key, value in os.environ.items() if value is not None},
        )
        audit("MCP_CONNECT", "stdio MCP client to local sentinelos-testnet-mcp.", transport="stdio")
        return Client(params)

    from mcp_server import mcp as server

    audit("MCP_CONNECT", "in-process MCP client to local sentinelos-testnet-mcp.", transport="inprocess")
    return Client(server)


class SentinelMcpHost:
    """Thin host around ``mcp.Client``: discover tools, call tools, read resources."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.tools: list[Any] = []
        self.protocol_version: str | None = None
        self.server_name: str | None = None

    async def discover(self) -> list[str]:
        info = getattr(self.client, "server_info", None)
        self.server_name = getattr(info, "name", None) if info is not None else None
        self.protocol_version = getattr(self.client, "protocol_version", None)
        listed = await self.client.list_tools()
        self.tools = list(listed.tools or [])
        names = [str(getattr(tool, "name", "")) for tool in self.tools]
        audit(
            "MCP_DISCOVER",
            "Listed local MCP tools.",
            server=self.server_name,
            protocol=self.protocol_version,
            tools=",".join(names),
        )
        return names

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke an MCP tool. Fail-closed: tool errors become exceptions."""
        audit("MCP_CALL", f"tools/call {name}", tool=name)
        result = await self.client.call_tool(name, arguments or {})
        if result.is_error:
            text = _content_text(result.content)
            audit("MCP_ERROR", f"tools/call {name} failed.", tool=name, error=text)
            _raise_tool_failure(text)
        payload = result.structured_content
        if isinstance(payload, dict):
            return _require_testnet(payload)
        text = _content_text(result.content)
        if not text:
            raise RuntimeError(f"MCP tool {name!r} returned an empty result.")
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = {"environment": "testnet", "status": "ok", "text": text}
        if isinstance(decoded, dict):
            return _require_testnet(decoded)
        return {"environment": "testnet", "result": decoded}

    async def read_json(self, uri: str) -> dict[str, Any]:
        result = await self.client.read_resource(uri)
        for item in result.contents or []:
            if isinstance(item, TextResourceContents) and item.text:
                payload = json.loads(item.text)
                if isinstance(payload, dict):
                    return payload
        raise RuntimeError(f"MCP resource {uri!r} returned no JSON.")


def _content_text(content: object) -> str:
    parts: list[str] = []
    if not content:
        return ""
    for block in content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
    return "\n".join(parts).strip()


def _require_testnet(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("environment") != "testnet":
        raise RuntimeError(
            "MCP tool returned a non-testnet payload. Submit aborted (fail-closed)."
        )
    return payload


def _raise_tool_failure(text: str) -> None:
    message = text.strip() or "MCP tool failed."
    lowered = message.lower()
    if "kill-switch" in lowered or "kill switch" in lowered:
        raise KillSwitchActivated(message)
    if "security warning" in lowered or "human_confirmed" in lowered or "confirmation" in lowered:
        raise PermissionError(message)
    if "blocked non-testnet" in lowered or "binance_env" in lowered:
        raise RuntimeError(message)
    if lowered.startswith("invalid") or "must be" in lowered:
        raise ValueError(message)
    raise RuntimeError(message)
