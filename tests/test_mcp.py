"""Check that the wrapper retains official tools, schemas, and quickstart discovery."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from beads_hub import mcp as server
from beads_hub.mcp import mcp


def test_official_server_and_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise MCP discovery and the hub trigger through the actual FastMCP client."""
    monkeypatch.setenv("BEADS_WORKING_DIR", str(tmp_path))

    async def inspect_server() -> None:
        """Open an in-memory MCP session without GitHub credentials or a local bd binary."""
        async with Client(mcp) as client:
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


@pytest.mark.parametrize("transport", ["stdio", "streamable-http", "sse"])
def test_transport_settings(monkeypatch: pytest.MonkeyPatch, transport: str) -> None:
    """Stdio receives no HTTP-only options; network transports retain their configured bind."""
    monkeypatch.setenv("MCP_TRANSPORT", transport)
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_PORT", "unused" if transport == "stdio" else "8090")
    monkeypatch.setattr(server, "wait_for_hub", lambda: None)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(mcp, "run", lambda **kwargs: calls.append(kwargs))
    server.main()
    expected: dict[str, Any] = {"transport": transport}
    if transport != "stdio":
        expected.update(host="127.0.0.1", port=8090)
    assert calls == [expected]


def test_stdio_client_process(tmp_path: Path) -> None:
    """A spawned installed server speaks MCP over stdio without credentials or an HTTP listener."""
    beads_dir = tmp_path / ".beads"
    beads_dir.mkdir()
    (beads_dir / "metadata.json").write_text("{}")
    fake_bd = tmp_path / "bd"
    fake_bd.write_text("#!/bin/sh\nprintf '{\"ready\":true}\\n'\n")
    fake_bd.chmod(0o755)
    transport = StdioTransport(
        command="beads-hub-mcp",
        args=[],
        cwd=str(tmp_path),
        env={
            **os.environ,
            "BEADS_PATH": str(fake_bd),
            "BEADS_WORKING_DIR": str(tmp_path),
            "BEADS_DIR": str(beads_dir),
            "MCP_TRANSPORT": "stdio",
            "MCP_PORT": "unused",
            "GH_TOKEN": "",
        },
    )

    async def inspect_stdio() -> None:
        """Negotiate a subprocess session, discover upstream tools, and call the sync trigger."""
        async with asyncio.timeout(30), Client(transport) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert {"ready", "show", "trigger_github_sync"} <= tools
            resources = await client.list_resources()
            assert "beads://quickstart" in {str(resource.uri) for resource in resources}
            result = await client.call_tool("trigger_github_sync", {})
            assert result.data["status"] == "partial"
            assert "Inject GH_TOKEN" in result.data["errors"][0]

    asyncio.run(inspect_stdio())
