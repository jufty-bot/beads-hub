"""Synthetic GitHub snapshots shared by offline and real-runtime checks."""

from __future__ import annotations

import copy

from beads_hub import sync
from beads_hub.github import GitHub, node_metadata, source_metadata


def repository(name: str = "example/orbit", node_id: str = "R_1") -> sync.Json:
    """Return a synthetic repository without using live GitHub data."""
    return {"node_id": node_id, "full_name": name, "archived": False, "disabled": False}


def item(kind: str = "issue", number: int = 1, repo: sync.Json | None = None) -> sync.Json:
    """Create a source snapshot with approved, passing PR defaults."""
    repo = repo or repository()
    result = {
        "id": f"{repo['node_id']}_{kind}_{number}",
        "__typename": "PullRequest" if kind == "pr" else "Issue",
        "kind": kind,
        "number": number,
        "title": "Synthetic work",
        "body": "Synthetic source body",
        "state": "OPEN",
        "url": f"https://github.com/{repo['full_name']}/issues/{number}",
        "repository": {"id": repo["node_id"], "nameWithOwner": repo["full_name"]},
        "labels": ["bug", "label,with,commas"],
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "headRefName": "fix/synthetic",
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "baseRefOid": "b" * 40,
        "mergeStateStatus": "CLEAN",
        "updatedAt": "2026-10-20T12:00:00Z",
        "closedAt": None,
        "mergedAt": None,
        "additions": 12,
        "deletions": 3,
        "changedFiles": 2,
        "isCrossRepository": True,
        "commits": {
            "totalCount": 2,
            "nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}],
        },
        "reviewRequests": {
            "totalCount": 1,
            "nodes": [{"requestedReviewer": {"__typename": "User", "login": "synthetic-reviewer"}}],
        },
        "closingIssuesReferences": {"nodes": [], "pageInfo": {"hasNextPage": False}},
    }
    result["github"] = {
        **source_metadata(
            {
                "html_url": result["url"],
                "user": {"login": "synthetic-author", "type": "User"},
                "assignees": [{"login": "synthetic-assignee"}],
                "created_at": "2026-10-19T12:00:00Z",
                "updated_at": "2026-10-20T12:00:00Z",
                "comments": 4,
                "milestone": {"number": 1, "title": "Synthetic milestone", "state": "open"},
            },
            repo,
        ),
        **(node_metadata(result) if kind == "pr" else {}),
    }
    return result


class FixtureGitHub(GitHub):
    """Supply staged source snapshots and explicit state verification failures."""

    def __init__(self, items: list[sync.Json], repos: list[sync.Json] | None = None) -> None:
        """Retain source rows separately from later direct-state reads."""
        self.rows = items
        self.repos = repos or [repository()]
        self.states = {row["id"]: copy.deepcopy(row) for row in items}
        self.failed_repos: set[str] = set()
        self.since_calls: list[str | None] = []
        self.node_calls: list[str] = []

    def repositories(self, scopes: str) -> list[sync.Json]:
        """Return only repositories selected in this test."""
        return self.repos

    def items(
        self,
        repo: sync.Json,
        *,
        since: str | None = None,
        prs: list[sync.Json] | None = None,
        full: bool = False,
    ) -> list[sync.Json]:
        """Return staged changes and current PR evidence, failing before any Beads writes."""
        self.since_calls.append(since)
        if repo["node_id"] in self.failed_repos:
            raise ValueError("Synthetic partial page")
        rows = copy.deepcopy(
            [row for row in self.rows if row["repository"]["id"] == repo["node_id"]]
        )
        seen = {row["id"] for row in rows}
        for bead in prs or []:
            node_id = bead["metadata"]["hub"]["node_id"]
            if node_id not in seen:
                node = self.node(node_id)
                node["github"] = {**bead["metadata"]["hub"].get("github", {}), **node["github"]}
                rows.append(node)
        return rows

    def node(self, node_id: str) -> sync.Json:
        """Return explicit state, or report inaccessible data without implying closure."""
        if node_id not in self.states:
            raise ValueError("Synthetic unavailable node")
        self.node_calls.append(node_id)
        return copy.deepcopy(self.states[node_id])

    def nodes(self, node_ids: list[str]) -> dict[str, sync.Json]:
        """Read synthetic nodes without a real SDK or worker threads."""
        return {node_id: self.node(node_id) for node_id in node_ids}
