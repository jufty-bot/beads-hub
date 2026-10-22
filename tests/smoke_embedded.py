"""Exercise repository-local embedded Dolt, polling, and stdio without Docker or a SQL server."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fixtures import FixtureGitHub, item

from beads_hub.sync import Beads, run_sync


async def check_clients(source: FixtureGitHub, beads: Beads) -> None:
    """Overlap real MCP reads with poller writes, then reopen MCP to verify durable state."""
    issue_id = next(row["id"] for row in beads.items() if row["metadata"]["hub"]["kind"] == "issue")
    beads.call("update", issue_id, "--priority", "1", "--notes", "Synthetic local reasoning")
    for session in range(2):
        transport = StdioTransport(command="beads-hub-mcp", args=[], env=dict(os.environ))
        async with asyncio.timeout(180), Client(transport) as client:
            assert "ready" in {tool.name for tool in await client.list_tools()}
            assert await client.read_resource("beads://quickstart")
            if session == 0:

                async def poll() -> None:
                    """Write changed snapshots through the normal sync lock and checkpoints."""
                    for number in range(2):
                        source.rows[0]["title"] = f"Synthetic revision {number}"
                        result = await asyncio.to_thread(run_sync)
                        assert result["status"] == "complete", result

                async def read() -> None:
                    """Read the same embedded database through separate MCP bd subprocesses."""
                    for _ in range(3):
                        result = await client.call_tool("stats", {})
                        assert not result.is_error

                await asyncio.gather(poll(), read())
                with Path(".github-sync.lock").open("w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    result = await client.call_tool("trigger_github_sync", {})
                    assert result.data["status"] == "busy"
            result = await client.call_tool("show", {"issue_id": issue_id})
            assert not result.is_error
    rows = beads.items()
    issue = next(row for row in rows if row["id"] == issue_id)
    assert len(rows) == 2 and issue["title"].endswith("Synthetic revision 1")
    assert issue["priority"] == 1 and issue["notes"] == "Synthetic local reasoning"
    assert len(beads.call("dep", "list", issue_id, "--type", "blocks")) == 1
    assert run_sync()["writes"] == 0


def main() -> None:
    """Bootstrap a disposable repository using the installed native bd and embedded defaults."""
    bd = shutil.which(os.environ.get("BEADS_PATH", "bd"))
    if bd is None:
        raise RuntimeError("Install native bd 1.3.1 with embedded Dolt support to run smoke-local")
    bd = str(Path(bd).resolve())
    original = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="beads-hub-embedded-") as directory:
        workspace = Path(directory)
        subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("BEADS_", "BD_", "DOLT_"))
            and key not in {"GH_TOKEN", "MCP_TRANSPORT"}
        }
        env.update(
            BEADS_PATH=bd,
            BEADS_DOLT_AUTO_START="0",
            BEADS_ACTOR="synthetic-embedded-smoke",
            GH_TOKEN="",
            MCP_PORT="unused",
        )
        with patch.dict(os.environ, env, clear=True):
            os.chdir(workspace)
            try:
                subprocess.run(
                    [
                        bd,
                        "init",
                        "--prefix",
                        "hub",
                        "--non-interactive",
                        "--skip-agents",
                        "--skip-hooks",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                metadata = json.loads(Path(".beads/metadata.json").read_text())
                assert metadata["dolt_mode"] == "embedded"
                assert Path(".beads/embeddeddolt").is_dir()
                issue, pr = item(), item("pr", number=2)
                pr["closingIssuesReferences"]["nodes"] = [{"id": issue["id"]}]
                source = FixtureGitHub([issue, pr])
                source.client = MagicMock()
                source.retry_at = 0
                with patch("beads_hub.sync.GitHub", return_value=source):
                    result = run_sync()
                    assert result["status"] == "complete", result
                    asyncio.run(check_clients(source, Beads()))
                status = subprocess.run(
                    ["beads-hub-sync", "--status"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                progress = json.loads(status.stdout)
                assert progress["repositories"]["R_1"]["checkpoint"] > 0
            finally:
                os.chdir(original)
    print("Embedded Dolt bootstrap, concurrent polling/stdio, locking, and persistence pass.")


if __name__ == "__main__":
    main()
