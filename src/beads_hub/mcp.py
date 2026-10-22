"""Serve the official beads-mcp tools and resources over stdio, HTTP, or SSE."""

from __future__ import annotations

import os
from typing import Any

from beads_mcp.server import mcp as mcp

from beads_hub.runtime import wait_for_hub
from beads_hub.sync import run_sync


@mcp.tool(
    name="trigger_github_sync",
    description="Reconcile GitHub issues and pull requests; returns complete, partial, or busy.",
)
def trigger_github_sync() -> dict[str, Any]:
    """Run reconciliation synchronously under the same lock as the periodic worker."""
    return run_sync()


def main() -> None:
    """Wait for initialization and serve the official MCP instance on the configured transport."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "streamable-http", "sse"}:
        raise ValueError("MCP_TRANSPORT must be stdio, streamable-http, or sse")
    wait_for_hub()
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    mcp.run(
        transport="sse" if transport == "sse" else "streamable-http",
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
