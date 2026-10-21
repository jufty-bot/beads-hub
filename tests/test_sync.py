"""Token-free behavior checks for source reconciliation and local triage preservation."""

from __future__ import annotations

import copy
import csv
import fcntl
import json
from pathlib import Path
from typing import Any

import gh_global_sync as sync
import pytest
from fixtures import FixtureGitHub, item, repository


class MemoryBeads(sync.Beads):
    """Store CLI effects in memory while exercising the production reconciliation policy."""

    def __init__(self) -> None:
        """Start with an empty backlog and dependency graph."""
        self.rows: dict[str, sync.Json] = {}
        self.edges: set[tuple[str, str]] = set()

    def call(self, *args: str) -> Any:
        """Emulate the bd commands used by the adapter, rejecting unsupported calls."""
        if args[0] == "list":
            assert "--limit" in args and args[args.index("--limit") + 1] == "0"
            return copy.deepcopy(list(self.rows.values()))
        if args[0] == "create":
            bead_id = f"hub-{len(self.rows) + 1}"
            self.rows[bead_id] = {"id": bead_id, "status": "open", "labels": [], "metadata": {}}
            self._update(self.rows[bead_id], args[1:])
            return copy.deepcopy(self.rows[bead_id])
        if args[0] == "dep":
            action, source = args[1:3]
            if action == "list":
                return [{"id": target} for start, target in self.edges if start == source]
            target = args[3]
            if action == "add":
                self.edges.add((source, target))
            elif action == "remove":
                self.edges.discard((source, target))
            else:
                raise AssertionError(args)
            return None
        bead = self.rows[args[1]]
        if args[0] in {"close", "reopen"}:
            bead["status"] = "closed" if args[0] == "close" else "open"
        elif args[0] == "update":
            self._update(bead, args[2:])
        else:
            raise AssertionError(args)
        return None

    def _update(self, bead: sync.Json, flags: tuple[str, ...]) -> None:
        """Apply individual flag values without emulating production policy decisions."""
        if "--if-assignee" in flags:
            assert (bead.get("assignee") or "") == flags[flags.index("--if-assignee") + 1]
        if "--if-status" in flags:
            assert bead["status"] == flags[flags.index("--if-status") + 1]
        flags = tuple(flag for flag in flags if flag != "--force")
        for flag, value in zip(flags[::2], flags[1::2], strict=True):
            if flag.startswith("--if-"):
                continue
            if flag == "--metadata":
                bead["metadata"].update(json.loads(value))
            elif flag in {"--add-label", "--remove-label"}:
                label = next(csv.reader([value]))[0]
                if flag == "--add-label" and label not in bead["labels"]:
                    bead["labels"].append(label)
                elif flag == "--remove-label":
                    bead["labels"].remove(label)
            else:
                bead[flag.removeprefix("--").replace("-", "_")] = value


@pytest.mark.parametrize(
    ("field", "value", "label"),
    [
        ("mergeable", "CONFLICTING", "conflict:true"),
        ("mergeable", "UNKNOWN", "conflict:unknown"),
        ("reviewDecision", "CHANGES_REQUESTED", "review:changes-requested"),
        ("reviewDecision", None, "review:needed"),
    ],
)
def test_actionable_pr(field: str, value: str | None, label: str) -> None:
    """PRs needing work or review join the open queue."""
    pr = item("pr")
    pr[field] = value
    status, labels = sync.evaluate(pr)
    assert status == "open" and label in labels


@pytest.mark.parametrize("state", ["FAILURE", "ERROR", None])
def test_failing_or_unknown_ci(state: str | None) -> None:
    """Unknown checks are never described as passing."""
    pr = item("pr")
    pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {"state": state} if state else None
    assert sync.evaluate(pr)[0] == "open"


def test_healthy_pr_waits_for_merge() -> None:
    """Approved passing PRs sit in progress instead of competing for ready work."""
    assert sync.evaluate(item("pr"))[0] == "in_progress"


def test_repeat_sync_preserves_local_triage_and_updates_source() -> None:
    """Source refresh removes obsolete signals while retaining local triage fields."""
    pr = item("pr")
    pr["reviewDecision"] = None
    source, beads = FixtureGitHub([pr]), MemoryBeads()
    sync.reconcile(source, beads, "user:@me")
    local = beads.rows["hub-1"]
    local.update({"priority": 1, "notes": "Local reasoning", "assignee": "maintainer"})
    local["labels"].append("category:bug-fix")
    local["metadata"]["other"] = {"keep": True}
    source.rows[0].update({"title": "New title", "reviewDecision": "APPROVED", "labels": ["new"]})
    result = sync.reconcile(source, beads, "user:@me")
    assert result["status"] == "complete" and len(beads.rows) == 1
    assert local["title"].endswith("New title")
    assert local["priority"] == 1 and local["notes"] == "Local reasoning"
    assert local["assignee"] == "maintainer" and local["status"] == "open"
    assert set(local["labels"]) == {
        "category:bug-fix",
        "repo:example/orbit",
        "type:pr",
        "github:new",
        "ci:passing",
        "review:approved",
    }
    assert local["metadata"]["other"] == {"keep": True}


def test_manual_status_survives_sync() -> None:
    """A locally deferred or closed item stays in its chosen workflow state."""
    source, beads = FixtureGitHub([item()]), MemoryBeads()
    sync.reconcile(source, beads, "user:@me")
    beads.rows["hub-1"]["status"] = "deferred"
    sync.reconcile(source, beads, "user:@me")
    assert beads.rows["hub-1"]["status"] == "deferred"


def test_absence_requires_explicit_closure_and_reopen() -> None:
    """Search/scope absence cannot close work; verified closure and reopening propagate."""
    row = item()
    source, beads = FixtureGitHub([row]), MemoryBeads()
    sync.reconcile(source, beads, "user:@me")
    source.rows = []
    source.states = {}
    assert sync.reconcile(source, beads, "user:@me")["status"] == "partial"
    assert beads.rows["hub-1"]["status"] == "open"
    source.states[row["id"]] = dict(row, state="CLOSED")
    assert sync.reconcile(source, beads, "user:@me")["closed"] == 1
    assert beads.rows["hub-1"]["status"] == "closed"
    source.rows = [row]
    sync.reconcile(source, beads, "user:@me")
    assert len(beads.rows) == 1 and beads.rows["hub-1"]["status"] == "open"


def test_scope_change_preserves_other_repo() -> None:
    """A narrower sync never closes work from an excluded repository."""
    source, beads = FixtureGitHub([item()]), MemoryBeads()
    sync.reconcile(source, beads, "user:@me")
    source.repos = [repository("example/other", "R_2")]
    source.rows = []
    source.states = {}
    assert sync.reconcile(source, beads, "repo:example/other")["status"] == "complete"
    assert beads.rows["hub-1"]["status"] == "open"


def test_repository_rename_uses_node_identity() -> None:
    """A repository rename updates external references without duplicating beads."""
    source, beads = FixtureGitHub([item()]), MemoryBeads()
    sync.reconcile(source, beads, "user:@me")
    renamed = repository("example/renamed")
    source.rows = [item(repo=renamed)]
    source.repos = [renamed]
    sync.reconcile(source, beads, "user:@me")
    assert len(beads.rows) == 1
    assert beads.rows["hub-1"]["external_ref"] == "gh:example/renamed#issue:1"
    assert "repo:example/orbit" not in beads.rows["hub-1"]["labels"]


def test_cross_repo_links_and_removed_references() -> None:
    """Closing references create issue-to-PR blockers, then remove stale managed edges."""
    other = repository("example/other", "R_2")
    issue, pr = item(), item("pr", 2, other)
    pr["closingIssuesReferences"]["nodes"] = [{"id": issue["id"]}]
    source, beads = FixtureGitHub([pr, issue], [other, repository()]), MemoryBeads()
    assert sync.reconcile(source, beads, "user:@me")["status"] == "complete"
    assert beads.edges == {("hub-2", "hub-1")}
    beads.edges.add(("hub-2", "manual-blocker"))
    pr["closingIssuesReferences"]["nodes"] = []
    sync.reconcile(source, beads, "user:@me")
    assert beads.edges == {("hub-2", "manual-blocker")}


def test_preexisting_manual_link_is_not_owned() -> None:
    """The reconciler leaves a matching preexisting manual dependency intact."""
    issue, pr = item(), item("pr", 2)
    source, beads = FixtureGitHub([issue, pr]), MemoryBeads()
    sync.reconcile(source, beads, "user:@me")
    beads.edges.add(("hub-1", "hub-2"))
    pr["closingIssuesReferences"]["nodes"] = [{"id": issue["id"]}]
    sync.reconcile(source, beads, "user:@me")
    pr["closingIssuesReferences"]["nodes"] = []
    sync.reconcile(source, beads, "user:@me")
    assert beads.edges == {("hub-1", "hub-2")}


def test_incomplete_repo_is_not_written() -> None:
    """A failed repository leaves old snapshots untouched while other repositories progress."""
    other = repository("example/other", "R_2")
    source, beads = FixtureGitHub([item(), item(repo=other)], [repository(), other]), MemoryBeads()
    source.failed_repos.add("R_1")
    result = sync.reconcile(source, beads, "user:@me")
    assert result["status"] == "partial" and result["issues"] == 1
    assert len(beads.rows) == 1
    assert beads.rows["hub-1"]["external_ref"] == "gh:example/other#issue:1"


def test_dependency_intent_is_repaired() -> None:
    """A failed/interrupted add is retried from the persisted ownership record."""
    issue, pr = item(), item("pr", 2)
    pr["closingIssuesReferences"]["nodes"] = [{"id": issue["id"]}]
    source, beads = FixtureGitHub([issue, pr]), MemoryBeads()
    sync.reconcile(source, beads, "user:@me")
    beads.edges.clear()
    assert sync.reconcile(source, beads, "user:@me")["status"] == "complete"
    assert beads.edges == {("hub-1", "hub-2")}


def test_interrupted_create_can_reconcile_later_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creation persists enough identity to verify closure before the next open listing."""
    row = item()
    source, beads = FixtureGitHub([row]), MemoryBeads()
    original = beads.call

    def interrupted_update(*args: str) -> Any:
        """Fail immediately after creating the source-backed bead."""
        if args[0] == "update":
            raise RuntimeError("Synthetic interrupted first update")
        return original(*args)

    monkeypatch.setattr(beads, "call", interrupted_update)
    assert sync.reconcile(source, beads, "user:@me")["status"] == "partial"
    monkeypatch.setattr(beads, "call", original)
    source.rows = []
    source.states[row["id"]] = dict(row, state="CLOSED")
    assert sync.reconcile(source, beads, "user:@me")["closed"] == 1
    assert len(beads.rows) == 1 and beads.rows["hub-1"]["status"] == "closed"


def test_status_update_guards_against_new_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A claim made after snapshot loading prevents an automatic status overwrite."""
    pr = item("pr")
    pr["reviewDecision"] = None
    source, beads = FixtureGitHub([pr]), MemoryBeads()
    sync.reconcile(source, beads, "user:@me")
    pr["reviewDecision"] = "APPROVED"
    original = beads.call

    def claim_before_update(*args: str) -> Any:
        """Simulate bd rejecting a guarded update after a concurrent claim."""
        if args[0] == "update" and "--status" in args:
            assert args[args.index("--if-status") + 1] == "open"
            assert args[args.index("--if-assignee") + 1] == ""
            beads.rows["hub-1"].update(status="in_progress", assignee="another-maintainer")
            raise RuntimeError("Synthetic precondition failure")
        return original(*args)

    monkeypatch.setattr(beads, "call", claim_before_update)
    assert sync.reconcile(source, beads, "user:@me")["status"] == "partial"
    assert beads.rows["hub-1"]["assignee"] == "another-maintainer"
    assert beads.rows["hub-1"]["status"] == "in_progress"


def test_rest_pagination_beyond_search_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """More than 1,000 items are read without a search API cutoff."""
    calls: list[list[str]] = []

    def response(args: list[str]) -> list[sync.Json]:
        """Return eleven full pages followed by an empty terminal page."""
        calls.append(args)
        return [{"number": 1}] * 100 if len(calls) <= 11 else []

    monkeypatch.setattr(sync, "command", response)
    assert len(sync.GitHub().pages("repos/example/orbit/issues?state=open")) == 1100
    assert "page=12" in calls[-1][-1]


def test_inventory_scopes_deduplicate_and_exclude_archives(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overlapping user, organization, and explicit scopes import each active repo once."""
    source = sync.GitHub()
    active = repository()
    archived = dict(repository("example/archive", "R_2"), archived=True)
    monkeypatch.setattr(source, "pages", lambda endpoint: [active, archived])
    monkeypatch.setattr(sync, "command", lambda args: active)
    assert source.repositories("user:@me;org:example;repo:example/orbit") == [active]


def test_invalid_later_source_row_prevents_repository_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate the whole REST snapshot before persisting even its earlier valid rows."""
    source, beads = sync.GitHub(), MemoryBeads()
    valid = {
        "node_id": "synthetic",
        "number": 1,
        "title": "Synthetic",
        "body": "",
        "state": "open",
        "html_url": "https://github.com/example/orbit/issues/1",
        "labels": [],
    }
    invalid = dict(valid, node_id="invalid", state="unexpected")
    monkeypatch.setattr(source, "repositories", lambda scopes: [repository()])
    monkeypatch.setattr(source, "pages", lambda endpoint: [valid, invalid])
    assert sync.reconcile(source, beads, "user:@me")["status"] == "partial"
    assert not beads.rows


@pytest.mark.parametrize("scope", ["", "user:@me is:open", "org:foo/bar", "repo:foo"])
def test_invalid_scopes_rejected(scope: str) -> None:
    """Unsupported search syntax fails before touching a backlog."""
    with pytest.raises(ValueError):
        sync.GitHub().repositories(scope)


def test_graphql_partial_errors_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GraphQL partial result cannot become a successful source snapshot."""
    monkeypatch.setattr(sync, "command", lambda args: {"data": {}, "errors": [{"message": "x"}]})
    with pytest.raises(ValueError, match="errors"):
        sync.GitHub().node("synthetic")


def test_reference_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR references beyond the first page are retained."""
    calls: list[list[str]] = []

    def response(args: list[str]) -> sync.Json:
        """Return two closing-reference pages for a single PR node."""
        calls.append(args)
        pr = item("pr")
        pr["__typename"] = "PullRequest"
        pr["closingIssuesReferences"] = {
            "nodes": [{"id": f"issue-{len(calls)}"}],
            "pageInfo": {"hasNextPage": len(calls) == 1, "endCursor": "next"},
        }
        return {"data": {"node": pr}}

    monkeypatch.setattr(sync, "command", response)
    result = sync.GitHub().node(item("pr")["id"])
    assert len(result["closingIssuesReferences"]["nodes"]) == 2
    assert "cursor=next" in calls[-1]


def test_concurrent_sync_returns_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second worker cannot interleave creation, closure, or dependency writes."""
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads/metadata.json").write_text("{}")
    monkeypatch.setenv("BEADS_WORKING_DIR", str(tmp_path))
    with (tmp_path / ".github-sync.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert sync.run_sync() == {"status": "busy", "errors": []}


def test_uninitialized_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup races report an explicit failure without creating a second database."""
    monkeypatch.setenv("BEADS_WORKING_DIR", str(tmp_path))
    assert sync.run_sync()["status"] == "partial"
