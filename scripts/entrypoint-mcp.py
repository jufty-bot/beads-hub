#!/usr/bin/env python3
"""Serve the official beads-mcp tools and resources over HTTP or SSE."""

from __future__ import annotations

import os
from typing import Any

from beads_mcp.server import mcp
from gh_global_sync import run_sync
from hub_runtime import wait_for_hub


@mcp.tool(
    name="trigger_github_sync",
    description="Reconcile GitHub issues and pull requests; returns complete, partial, or busy.",
)
def trigger_github_sync() -> dict[str, Any]:
    """Run reconciliation synchronously under the same lock as the periodic worker."""
    return run_sync()


if __name__ == "__main__":
    wait_for_hub()
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    if transport not in {"streamable-http", "sse"}:
        raise ValueError("MCP_TRANSPORT must be streamable-http or sse")
    mcp.run(
        transport="sse" if transport == "sse" else "streamable-http",
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8080")),
    )
