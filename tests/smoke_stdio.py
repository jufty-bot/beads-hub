"""Exercise the installed stdio server against an initialized disposable Dolt workspace."""

from __future__ import annotations

import asyncio
import os

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def check_stdio() -> None:
    """Discover official capabilities and read real Beads data over a spawned stdio session."""
    transport = StdioTransport(
        command="beads-hub-mcp",
        args=[],
        env={**os.environ, "MCP_TRANSPORT": "stdio", "GH_TOKEN": ""},
    )
    async with asyncio.timeout(60), Client(transport) as client:
        tools = {tool.name for tool in await client.list_tools()}
        assert {"ready", "stats", "trigger_github_sync"} <= tools
        assert await client.read_resource("beads://quickstart")
        for tool in ("stats", "ready"):
            result = await client.call_tool(tool, {})
            assert not result.is_error
        result = await client.call_tool("trigger_github_sync", {})
        assert result.data["status"] in {"partial", "busy"}


if __name__ == "__main__":
    asyncio.run(check_stdio())
    print("Stdio MCP checks passed against the configured Dolt workspace.")
