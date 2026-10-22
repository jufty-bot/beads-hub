"""Exercise reconciliation with real bd and Dolt in a disposable initialized workspace.

Run only in the disposable smoke stack described in docs/usage.md. GitHub data is
synthetic; this script deliberately modifies that stack's backlog.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from pathlib import Path

from fastmcp import Client
from fixtures import FixtureGitHub, item, repository
from smoke_stdio import check_stdio
from websockets.asyncio.client import connect

from beads_hub.github import node_metadata
from beads_hub.runtime import wait_for_hub
from beads_hub.state import SyncState
from beads_hub.sync import Beads, reconcile


def main() -> None:
    """Check peer readiness, dependency repair, claims, closure, reopening, and renames."""
    wait_for_hub()
    beads = Beads()
    assert not beads.items(), "Use an empty disposable database for this check"
    issue, pr = item(), item("pr", 2)
    pr["reviewDecision"] = None
    pr["closingIssuesReferences"]["nodes"] = [{"id": issue["id"]}]
    source = FixtureGitHub([issue, pr])
    progress = Path(os.environ["BEADS_WORKING_DIR"]) / ".github-sync-state.json"
    state = SyncState(progress)
    assert reconcile(source, beads, "user:@me", state)["status"] == "complete"
    state = SyncState(progress)
    result = reconcile(source, beads, "user:@me", state)
    assert result["status"] == "complete" and result["writes"] == 0
    assert result["modes"] == {"example/orbit": "delta"}
    rows = {row["metadata"]["hub"]["kind"]: row for row in beads.items()}
    assert len(rows) == 2
    issue_id, pr_id = rows["issue"]["id"], rows["pr"]["id"]
    snapshot = rows["pr"]["metadata"]["hub"]["github"]
    assert snapshot["author"]["login"] == "synthetic-author"
    assert snapshot["assignees"] == ["synthetic-assignee"]
    assert snapshot["pull_request"]["head_sha"] == "a" * 40
    assert "fix/synthetic -> main" in rows["pr"]["description"]
    ready = {row["id"] for row in beads.call("ready", "--limit", "0")}
    assert pr_id in ready and issue_id not in ready
    source.rows = [dict(issue, state="CLOSED"), pr]
    source.states[issue["id"]] = dict(issue, state="CLOSED")
    assert reconcile(source, beads, "user:@me", state)["closed"] == 1
    source.rows = [issue, pr]
    assert reconcile(source, beads, "user:@me", state)["status"] == "complete"
    assert next(row for row in beads.items() if row["id"] == issue_id)["status"] == "open"
    beads.call(
        "update",
        pr_id,
        "--claim",
        "--priority",
        "1",
        "--notes",
        "Synthetic local reasoning",
        "--add-label",
        "category:bug-fix",
        "--metadata",
        '{"local":{"keep":true}}',
    )
    pr["reviewDecision"] = "APPROVED"
    pr["labels"] = ["new-label"]
    pr["github"].update(assignees=[], milestone=None, comment_count=5)
    pr["github"]["pull_request"].update(requested_reviewers=[], requested_reviewers_total=0)
    pr["closingIssuesReferences"]["nodes"] = []
    assert reconcile(source, beads, "user:@me", state)["status"] == "complete"
    ready = {row["id"] for row in beads.call("ready", "--limit", "0")}
    assert issue_id in ready
    local = next(row for row in beads.items() if row["id"] == pr_id)
    assert local["status"] == "in_progress" and local["assignee"]
    assert local["priority"] == 1 and local["notes"] == "Synthetic local reasoning"
    assert local["metadata"]["local"] == {"keep": True}
    snapshot = local["metadata"]["hub"]["github"]
    assert snapshot["assignees"] == [] and snapshot["milestone"] is None
    assert snapshot["comment_count"] == 5
    assert snapshot["pull_request"]["requested_reviewers"] == []
    assert "category:bug-fix" in local["labels"] and "github:new-label" in local["labels"]
    assert "review:needed" not in local["labels"]
    source.rows = [issue]
    merged = dict(pr, state="MERGED", mergedAt="2026-10-21T12:00:00Z")
    merged["github"] = node_metadata(merged)
    source.states[pr["id"]] = merged
    beads.call("close", pr_id, "--reason", "Synthetic local close before upstream merge")
    assert reconcile(source, beads, "user:@me", state)["closed"] == 1
    closed = next(row for row in beads.items() if row["id"] == pr_id)
    assert closed["status"] == "closed"
    snapshot = closed["metadata"]["hub"]["github"]
    assert snapshot["pull_request"]["merged_at"] == "2026-10-21T12:00:00Z"
    assert snapshot["author"]["login"] == "synthetic-author"
    assert closed["metadata"]["hub"]["source_state"] == "MERGED"
    source.rows = [issue, pr]
    assert reconcile(source, beads, "user:@me", state)["status"] == "complete"
    assert next(row for row in beads.items() if row["id"] == pr_id)["status"] == "in_progress"
    renamed = repository("example/renamed")
    source.repos = [renamed]
    for row in source.rows:
        row["repository"]["nameWithOwner"] = renamed["full_name"]
    result = reconcile(source, beads, "user:@me", state)
    assert result["status"] == "complete" and result["modes"] == {"example/renamed": "full"}
    assert len(beads.items()) == 2
    assert all("example/renamed#" in row["external_ref"] for row in beads.items())
    asyncio.run(check_http())
    asyncio.run(check_stdio())
    print(
        json.dumps(
            {
                "status": "passed",
                "checks": [
                    "repeat sync",
                    "zero-write unchanged pass",
                    "durable incremental checkpoints",
                    "issue/PR readiness",
                    "dependency removal",
                    "claims and local triage",
                    "GitHub metadata refresh and clearing",
                    "upstream closure",
                    "upstream reopening",
                    "repository rename",
                    "stdio MCP tools, resource, and sync trigger",
                ],
            }
        )
    )


async def check_http() -> None:
    """Use actual HTTP clients against the UI and official MCP transport."""
    for _ in range(60):
        try:
            with urllib.request.urlopen("http://127.0.0.1:3000", timeout=2) as response:
                assert response.status == 200
            break
        except OSError:
            time.sleep(1)
    else:
        raise TimeoutError("UI did not become ready")
    async with asyncio.timeout(20), connect("ws://127.0.0.1:3000/ws") as socket:
        await socket.send(
            json.dumps(
                {
                    "id": "smoke",
                    "type": "subscribe-list",
                    "payload": {"id": "smoke-list", "type": "all-issues"},
                }
            )
        )
        while True:
            message = json.loads(await socket.recv())
            assert message.get("ok") is not False, message
            if message["type"] == "snapshot":
                issues = message["payload"]["issues"]
                assert len(issues) == 2
                assert {label for row in issues for label in row["labels"]} >= {
                    "type:issue",
                    "type:pr",
                }
                break
    async with Client("http://127.0.0.1:8080/mcp") as client:
        assert "ready" in {tool.name for tool in await client.list_tools()}
        assert await client.read_resource("beads://quickstart")
        result = await client.call_tool("stats", {})
        assert not result.is_error
        result = await client.call_tool("trigger_github_sync", {})
        assert result.data["status"] == "partial", "The smoke stack has no GitHub credentials"


if __name__ == "__main__":
    main()
