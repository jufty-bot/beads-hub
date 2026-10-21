"""Check that the wrapper retains official tools, schemas, and quickstart discovery."""

from __future__ import annotations

import asyncio
import runpy
from pathlib import Path

import pytest
from fastmcp import Client


def test_official_server_and_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise MCP discovery and the hub trigger through the actual FastMCP client."""
    monkeypatch.setenv("BEADS_WORKING_DIR", str(tmp_path))
    namespace = runpy.run_path(str(Path(__file__).parents[1] / "scripts/entrypoint-mcp.py"))

    async def inspect_server() -> None:
        """Open an in-memory MCP session without GitHub credentials or a local bd binary."""
        async with Client(namespace["mcp"]) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert {
                "ready",
                "list",
                "show",
                "create",
                "update",
                "close",
                "reopen",
                "dep",
                "comment",
                "comments",
                "note",
                "blocked",
                "stats",
                "context",
                "admin",
                "trigger_github_sync",
            } <= tools.keys()
            assert tools["show"].inputSchema
            resources = await client.list_resources()
            assert "beads://quickstart" in {str(resource.uri) for resource in resources}
            result = await client.call_tool("trigger_github_sync", {})
            assert result.data["status"] == "partial"

    asyncio.run(inspect_server())
