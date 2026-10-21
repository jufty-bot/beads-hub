"""Exercise incremental coverage, durable replay, and source freshness without live credentials."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import gh_global_sync as sync
import pytest
from fixtures import FixtureGitHub, item, repository
from github_source import node_metadata
from sync_state import FULL_SCAN_SECONDS, OVERLAP_SECONDS, SyncState
from test_sync import MemoryBeads


def test_unchanged_poll_has_no_beads_writes() -> None:
    """Observation times and existing dependencies do not cause repeat mutations."""
    issue, pr = item(), item("pr", number=2)
    pr["closingIssuesReferences"]["nodes"] = [{"id": issue["id"]}]
    source, beads = FixtureGitHub([issue, pr]), MemoryBeads()
    assert sync.reconcile(source, beads, "user:@me")["writes"] > 0
    for row in source.rows:
        row["github"]["observed_at"] = "2026-10-21T12:01:00Z"
    beads.calls.clear()
    assert sync.reconcile(source, beads, "user:@me")["writes"] == 0
    assert all(args[0] == "list" or args[:2] == ("dep", "list") for args in beads.calls)
    beads.rows["hub-1"]["title"] = "An accidental local edit of a source-owned field"
    assert sync.reconcile(source, beads, "user:@me")["writes"] == 1
    assert beads.rows["hub-1"]["title"].endswith(issue["title"])


def test_bootstrap_then_delta_survives_restart(tmp_path: Path) -> None:
    """A durable pass-start checkpoint produces an overlapping all-state delta next time."""
    source, beads = FixtureGitHub([item()]), MemoryBeads()
    path = tmp_path / "progress.json"
    state = SyncState(path)
    assert sync.reconcile(source, beads, "user:@me", state)["modes"] == {"example/orbit": "full"}
    committed = json.loads(path.read_text())["repositories"]["R_1"]
    source.rows = []
    state = SyncState(path)
    result = sync.reconcile(source, beads, "user:@me", state)
    assert result["status"] == "complete" and result["writes"] == 0
    assert result["modes"] == {"example/orbit": "delta"}
    since = source.since_calls[-1]
    assert since is not None
    assert datetime.fromisoformat(since).timestamp() == committed["checkpoint"] - OVERLAP_SECONDS
    assert source.node_calls == []
    assert beads.rows["hub-1"]["status"] == "open"
    assert not path.with_suffix(".tmp").exists()


@pytest.mark.parametrize("failure", ["source", "write", "dependency"])
def test_failures_replay_without_advancing_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Partial source, database, and graph failures retain the last successfully applied range."""
    issue, pr = item(), item("pr", number=2)
    pr["closingIssuesReferences"]["nodes"] = [{"id": issue["id"]}]
    source, beads = FixtureGitHub([issue, pr]), MemoryBeads()
    path = tmp_path / "progress.json"
    state = SyncState(path)
    sync.reconcile(source, beads, "user:@me", state)
    checkpoint = state.data["repositories"]["R_1"]["checkpoint"]
    issue["title"] = pr["title"] = "New upstream title"
    call = beads.call

    def fail_write(*args: str) -> Any:
        """Fail after an earlier issue update has already been committed."""
        if args[:2] == ("update", "hub-2"):
            raise RuntimeError("Synthetic database failure")
        return call(*args)

    def fail_dependency(pr: sync.Json, desired: set[str]) -> None:
        """Reject graph convergence after source-owned fields were persisted."""
        raise RuntimeError("Synthetic dependency failure")

    with monkeypatch.context() as patch:
        if failure == "source":
            source.failed_repos.add("R_1")
        elif failure == "write":
            patch.setattr(beads, "call", fail_write)
        else:
            patch.setattr(beads, "links", fail_dependency)
        assert sync.reconcile(source, beads, "user:@me", state)["status"] == "partial"
    saved = SyncState(path)
    assert saved.data["repositories"]["R_1"]["checkpoint"] == checkpoint
    assert saved.data["repositories"]["R_1"]["error"]
    source.failed_repos.clear()
    assert sync.reconcile(source, beads, "user:@me", saved)["status"] == "complete"
    assert len(beads.rows) == 2
    assert all(row["title"].endswith("New upstream title") for row in beads.rows.values())
    assert saved.data["repositories"]["R_1"]["error"] is None
    assert saved.data["repositories"]["R_1"]["checkpoint"] > checkpoint


def test_checkpoints_advance_independently_by_repository(tmp_path: Path) -> None:
    """A failed repository cannot suppress durable progress for another successful one."""
    other = repository("example/other", "R_2")
    source = FixtureGitHub([item(), item(repo=other)], repos=[repository(), other])
    beads, state = MemoryBeads(), SyncState(tmp_path / "progress.json")
    sync.reconcile(source, beads, "user:@me", state)
    checkpoints = copy.deepcopy(state.data["repositories"])
    source.failed_repos.add("R_1")
    assert sync.reconcile(source, beads, "user:@me", state)["status"] == "partial"
    assert state.data["repositories"]["R_1"]["checkpoint"] == checkpoints["R_1"]["checkpoint"]
    assert state.data["repositories"]["R_2"]["checkpoint"] > checkpoints["R_2"]["checkpoint"]


@pytest.mark.parametrize("forced", [False, True])
def test_full_repair_verifies_missed_closure(tmp_path: Path, forced: bool) -> None:
    """Hourly and operator-requested full repairs explicitly verify missed terminal transitions."""
    row = item()
    source, beads, state = (
        FixtureGitHub([row]),
        MemoryBeads(),
        SyncState(tmp_path / "progress.json"),
    )
    sync.reconcile(source, beads, "user:@me", state)
    source.rows = []
    source.states[row["id"]]["state"] = "CLOSED"
    assert sync.reconcile(source, beads, "user:@me", state)["closed"] == 0
    assert source.node_calls == []
    if not forced:
        state.data["repositories"]["R_1"]["last_full_at"] -= FULL_SCAN_SECONDS
    result = sync.reconcile(source, beads, "user:@me", state, force_full=forced)
    assert result["closed"] == 1 and result["modes"] == {"example/orbit": "full"}
    assert beads.rows["hub-1"]["status"] == "closed"


def test_pr_signals_refresh_without_rest_changes(tmp_path: Path) -> None:
    """Known open PRs change readiness even when the incremental REST collection is empty."""
    pr = item("pr")
    source, beads, state = FixtureGitHub([pr]), MemoryBeads(), SyncState(tmp_path / "progress.json")
    sync.reconcile(source, beads, "user:@me", state)
    source.rows = []
    current = source.states[pr["id"]]
    current["reviewDecision"] = "CHANGES_REQUESTED"
    current["github"] = {**current["github"], **node_metadata(current)}
    assert sync.reconcile(source, beads, "user:@me", state)["status"] == "complete"
    assert beads.rows["hub-1"]["status"] == "open"
    assert "review:changes-requested" in beads.rows["hub-1"]["labels"]
    assert source.node_calls == [pr["id"]]


def test_delta_imports_work_created_and_closed_between_polls(tmp_path: Path) -> None:
    """All-state deltas discover closed work without inferring closure from absent rows."""
    source, beads, state = (
        FixtureGitHub([item()]),
        MemoryBeads(),
        SyncState(tmp_path / "progress.json"),
    )
    sync.reconcile(source, beads, "user:@me", state)
    source.rows = [dict(item(number=2), state="CLOSED")]
    result = sync.reconcile(source, beads, "user:@me", state)
    assert result["closed"] == 1 and len(beads.rows) == 2
    assert beads.rows["hub-1"]["status"] == "open"
    assert beads.rows["hub-2"]["status"] == "closed"


def test_rename_requires_full_repair_even_after_failed_attempt(tmp_path: Path) -> None:
    """A failed rename refresh cannot make old repository metadata look fully reconciled."""
    state = SyncState(tmp_path / "progress.json")
    state.success(repository(), started=1000, finished=1001, full=True)
    renamed = repository("example/renamed")
    assert state.since(renamed, now=1100) is None
    state.failure(renamed, "Synthetic failure")
    assert SyncState(state.path).since(renamed, now=1200) is None


@pytest.mark.parametrize("value", [True, -1, float("nan"), "not-a-timestamp"])
def test_invalid_checkpoints_fail_without_resetting(tmp_path: Path, value: Any) -> None:
    """Malformed progress cannot silently move the cursor past unapplied changes."""
    path = tmp_path / "progress.json"
    content = json.dumps({"version": 1, "repositories": {"R_1": {"checkpoint": value}}})
    path.write_text(content)
    with pytest.raises(ValueError, match="timestamp"):
        SyncState(path)
    assert path.read_text() == content


def test_checkpoint_replace_failure_replays_committed_beads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed checkpoint replacement keeps old coverage and replays without duplicate writes."""
    row = item()
    source, beads = FixtureGitHub([row]), MemoryBeads()
    path = tmp_path / "progress.json"
    state = SyncState(path)
    sync.reconcile(source, beads, "user:@me", state)
    checkpoint = state.data["repositories"]["R_1"]["checkpoint"]
    row["title"] = "New source title"

    def fail_replace(source: Path, target: Path) -> None:
        """Fail at the commit boundary after flushing the new temporary document."""
        raise OSError("Synthetic interrupted checkpoint commit")

    with monkeypatch.context() as patch:
        patch.setattr("sync_state.os.replace", fail_replace)
        with pytest.raises(OSError, match="checkpoint"):
            sync.reconcile(source, beads, "user:@me", state)
    replay = SyncState(path)
    assert replay.data["repositories"]["R_1"]["checkpoint"] == checkpoint
    assert beads.rows["hub-1"]["title"].endswith("New source title")
    assert sync.reconcile(source, beads, "user:@me", replay)["writes"] == 0
    assert SyncState(path).data["repositories"]["R_1"]["checkpoint"] > checkpoint
