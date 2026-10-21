"""Synthetic GitHub snapshots shared by offline and real-runtime checks."""

from __future__ import annotations

import copy

import gh_global_sync as sync


def repository(name: str = "example/orbit", node_id: str = "R_1") -> sync.Json:
    """Return a synthetic repository without using live GitHub data."""
    return {"node_id": node_id, "full_name": name, "archived": False, "disabled": False}


def item(kind: str = "issue", number: int = 1, repo: sync.Json | None = None) -> sync.Json:
    """Create a source snapshot with approved, passing PR defaults."""
    repo = repo or repository()
    return {
        "id": f"{repo['node_id']}_{kind}_{number}",
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
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
        "closingIssuesReferences": {"nodes": [], "pageInfo": {"hasNextPage": False}},
    }


class FixtureGitHub(sync.GitHub):
    """Supply staged source snapshots and explicit state verification failures."""

    def __init__(self, items: list[sync.Json], repos: list[sync.Json] | None = None) -> None:
        """Retain source rows separately from later direct-state reads."""
        self.rows = items
        self.repos = repos or [repository()]
        self.states = {row["id"]: copy.deepcopy(row) for row in items}
        self.failed_repos: set[str] = set()

    def repositories(self, scopes: str) -> list[sync.Json]:
        """Return only repositories selected in this test."""
        return self.repos

    def open_items(self, repo: sync.Json) -> list[sync.Json]:
        """Fail before writes when a repository's pagination is incomplete."""
        if repo["node_id"] in self.failed_repos:
            raise ValueError("Synthetic partial page")
        return copy.deepcopy(
            [row for row in self.rows if row["repository"]["id"] == repo["node_id"]]
        )

    def node(self, node_id: str) -> sync.Json:
        """Return explicit state, or report inaccessible data without implying closure."""
        if node_id not in self.states:
            raise ValueError("Synthetic unavailable node")
        return copy.deepcopy(self.states[node_id])
