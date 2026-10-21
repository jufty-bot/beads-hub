"""Exercise reconciliation with real bd and Dolt in a disposable initialized workspace.

Run only in the disposable smoke stack described in docs/usage.md. GitHub data is
synthetic; this script deliberately modifies that stack's backlog.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request

from fastmcp import Client
from fixtures import FixtureGitHub, item, repository
from gh_global_sync import Beads, reconcile
from hub_runtime import wait_for_hub
from websockets.asyncio.client import connect


def main() -> None:
    """Check peer readiness, dependency repair, claims, closure, reopening, and renames."""
    wait_for_hub()
    beads = Beads()
    assert not beads.items(), "Use an empty disposable database for this check"
    issue, pr = item(), item("pr", 2)
    pr["reviewDecision"] = None
    pr["closingIssuesReferences"]["nodes"] = [{"id": issue["id"]}]
    source = FixtureGitHub([issue, pr])
    assert reconcile(source, beads, "user:@me")["status"] == "complete"
    assert reconcile(source, beads, "user:@me")["status"] == "complete"
    rows = {row["metadata"]["hub"]["kind"]: row for row in beads.items()}
    assert len(rows) == 2
    issue_id, pr_id = rows["issue"]["id"], rows["pr"]["id"]
    ready = {row["id"] for row in beads.call("ready", "--limit", "0")}
    assert pr_id in ready and issue_id not in ready
    source.rows = [pr]
    source.states[issue["id"]] = dict(issue, state="CLOSED")
    assert reconcile(source, beads, "user:@me")["closed"] == 1
    source.rows = [issue, pr]
    assert reconcile(source, beads, "user:@me")["status"] == "complete"
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
    pr["closingIssuesReferences"]["nodes"] = []
    assert reconcile(source, beads, "user:@me")["status"] == "complete"
    ready = {row["id"] for row in beads.call("ready", "--limit", "0")}
    assert issue_id in ready
    local = next(row for row in beads.items() if row["id"] == pr_id)
    assert local["status"] == "in_progress" and local["assignee"]
    assert local["priority"] == 1 and local["notes"] == "Synthetic local reasoning"
    assert local["metadata"]["local"] == {"keep": True}
    assert "category:bug-fix" in local["labels"] and "github:new-label" in local["labels"]
    assert "review:needed" not in local["labels"]
    source.rows = [issue]
    source.states[pr["id"]] = dict(pr, state="MERGED")
    assert reconcile(source, beads, "user:@me")["closed"] == 1
    assert next(row for row in beads.items() if row["id"] == pr_id)["status"] == "closed"
    source.rows = [issue, pr]
    assert reconcile(source, beads, "user:@me")["status"] == "complete"
    assert next(row for row in beads.items() if row["id"] == pr_id)["status"] == "in_progress"
    renamed = repository("example/renamed")
    source.repos = [renamed]
    for row in source.rows:
        row["repository"]["nameWithOwner"] = renamed["full_name"]
    assert reconcile(source, beads, "user:@me")["status"] == "complete"
    assert len(beads.items()) == 2
    assert all("example/renamed#" in row["external_ref"] for row in beads.items())
    asyncio.run(check_http())
    print(
        json.dumps(
            {
                "status": "passed",
                "checks": [
                    "repeat sync",
                    "issue/PR readiness",
                    "dependency removal",
                    "claims and local triage",
                    "upstream closure",
                    "upstream reopening",
                    "repository rename",
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
