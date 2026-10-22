"""Exercise the real GitHubKit HTTP boundary with synthetic, token-free responses."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fixtures import item, repository
from githubkit import GitHub as Client
from test_sync import MemoryBeads

from beads_hub import sync
from beads_hub.github import GitHub, Json, RateLimited, node_metadata, source_metadata
from beads_hub.state import FULL_SCAN_SECONDS, OVERLAP_SECONDS, SyncState


def source(handler: Callable[[httpx.Request], httpx.Response], *, workers: int = 1) -> GitHub:
    """Build a real SDK client whose network is replaced by an in-memory transport."""
    return GitHub(
        Client(
            "synthetic-token",
            transport=httpx.MockTransport(handler),
            http_cache=False,
            auto_retry=False,
        ),
        workers=workers,
    )


def rest_item(number: int = 1) -> Json:
    """Provide the issue-list shape shared by GitHub issues and pull requests."""
    row = item(number=number)
    return {
        "node_id": row["id"],
        "number": number,
        "title": row["title"],
        "body": row["body"],
        "state": "open",
        "html_url": row["url"],
        "labels": [],
        "user": {"login": "synthetic-author", "type": "User"},
        "assignees": [{"login": "synthetic-assignee"}],
        "author_association": "CONTRIBUTOR",
        "comments": 4,
        "locked": False,
        "created_at": "2026-10-19T12:00:00Z",
        "updated_at": "2026-10-20T12:00:00Z",
        "closed_at": None,
        "state_reason": None,
        "milestone": None,
    }


def test_rest_pagination_beyond_search_cap() -> None:
    """The SDK follows Link headers past 1,000 items, including a short first page."""
    pages: list[int] = []

    def response(request: httpx.Request) -> httpx.Response:
        """Serve linked collection pages and verify direct SDK authentication."""
        assert request.headers["authorization"] == "token synthetic-token"
        assert request.headers["x-github-api-version"] == "2022-11-28"
        page = int(request.url.params.get("page", "1"))
        pages.append(page)
        headers = {"Link": f'<{request.url.copy_set_param("page", page + 1)}>; rel="next"'}
        return httpx.Response(
            200,
            json=[{"number": page}] * (1 if page == 1 else 100),
            headers=headers if page < 12 else {},
        )

    github = source(response)
    with github.client:
        assert len(github.pages("repos/example/orbit/issues?state=open")) == 1101
    assert pages == list(range(1, 13))


def test_inventory_scopes_deduplicate_and_exclude_archives() -> None:
    """User, organization, and explicit repository scopes share node identity."""
    active = repository()
    archived = dict(repository("example/archive", "R_2"), archived=True)

    def response(request: httpx.Request) -> httpx.Response:
        """Serve inventory and explicit repository endpoints through SDK HTTP."""
        return httpx.Response(
            200, json=active if request.url.path == "/repos/example/orbit" else [active, archived]
        )

    github = source(response)
    with github.client:
        assert github.repositories("user:@me;org:example;repo:example/orbit") == [active]


@pytest.mark.parametrize("scope", ["", "user:@me is:open", "org:foo/bar", "repo:foo"])
def test_invalid_scopes_rejected(scope: str) -> None:
    """Unsupported search syntax fails before any HTTP request."""

    def response(request: httpx.Request) -> httpx.Response:
        """Reject unexpected network access for invalid scope input."""
        raise AssertionError(request.url)

    github = source(response)
    with github.client, pytest.raises(ValueError):
        github.repositories(scope)


def test_issue_and_pr_metadata() -> None:
    """Source snapshots include people, dates, PR size, branch identities, and review state."""
    pr = item("pr", number=2)
    pr.pop("github")
    issue_row, pr_row = rest_item(), rest_item(number=2)
    pr_row.update(node_id=pr["id"], pull_request={"url": "synthetic"})

    def response(request: httpx.Request) -> httpx.Response:
        """Return open issue/PR rows and a GraphQL PR detail result."""
        return httpx.Response(
            200, json={"data": {"node": pr}} if request.method == "POST" else [issue_row, pr_row]
        )

    github = source(response)
    with github.client:
        issue, pull = github.items(repository())
    assert issue["github"]["author"] == {"login": "synthetic-author", "type": "User"}
    assert issue["github"]["assignees"] == ["synthetic-assignee"]
    assert issue["github"]["comment_count"] == 4
    assert issue["github"]["milestone"] is None
    snapshot = pull["github"]["pull_request"]
    assert snapshot["head_sha"] == "a" * 40 and snapshot["base_ref"] == "main"
    assert snapshot["additions"] == 12 and snapshot["changed_files"] == 2
    assert snapshot["commit_count"] == 2 and snapshot["ci_state"] == "SUCCESS"
    assert snapshot["requested_reviewers"][0]["login"] == "synthetic-reviewer"
    assert snapshot["requested_reviewers_complete"] is True


def test_reference_pagination_and_reviewer_coverage() -> None:
    """Closing references are exhausted while reviewer details report their bounded coverage."""
    cursors: list[str | None] = []

    def response(request: httpx.Request) -> httpx.Response:
        """Serve two closing-reference pages for one PR node."""
        cursors.append(json.loads(request.content)["variables"]["cursor"])
        pr = item("pr")
        pr["reviewRequests"]["totalCount"] = 101
        pr["closingIssuesReferences"] = {
            "nodes": [{"id": f"issue-{len(cursors)}"}],
            "pageInfo": {"hasNextPage": len(cursors) == 1, "endCursor": "next"},
        }
        return httpx.Response(200, json={"data": {"node": pr}})

    github = source(response)
    with github.client:
        result = github.node(item("pr")["id"])
    assert len(result["closingIssuesReferences"]["nodes"]) == 2
    assert cursors == [None, "next"]
    assert result["github"]["pull_request"]["requested_reviewers_complete"] is False


@pytest.mark.parametrize("fault", ["graphql-errors", "unavailable", "repeated-cursor"])
def test_incomplete_graphql_rejected(fault: str) -> None:
    """Partial GraphQL results, inaccessible nodes, and looping cursors cannot be persisted."""

    def response(request: httpx.Request) -> httpx.Response:
        """Return a specific source coverage failure without real GitHub access."""
        pr = item("pr")
        pr["closingIssuesReferences"]["pageInfo"] = {"hasNextPage": True, "endCursor": "same"}
        payload: Json = {"data": {"node": None if fault == "unavailable" else pr}}
        if fault == "graphql-errors":
            payload["errors"] = [{"message": "Synthetic partial data", "type": "FORBIDDEN"}]
        return httpx.Response(200, json=payload)

    github = source(response)
    with github.client, pytest.raises((RuntimeError, ValueError)):
        github.node(item("pr")["id"])


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_later_rest_page_failure_rejects_snapshot(status: int) -> None:
    """Authentication, throttling, and outages cannot return a truncated successful collection."""
    requests = 0

    def response(request: httpx.Request) -> httpx.Response:
        """Fail on the second page with retries disabled at the SDK boundary."""
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                200,
                json=[rest_item()],
                headers={
                    "Link": '<https://api.github.com/repos/example/orbit/issues?page=2>; rel="next"'
                },
            )
        return httpx.Response(status, json={"message": "Synthetic failure"})

    github = source(response)
    with github.client, pytest.raises(RuntimeError, match="GitHub"):
        github.items(repository())
    assert requests == 2


def test_network_timeout_is_reported() -> None:
    """Network timeouts become bounded source failures rather than terminating the worker."""

    def response(request: httpx.Request) -> httpx.Response:
        """Raise a transport timeout instead of contacting a real service."""
        raise httpx.ReadTimeout("Synthetic timeout", request=request)

    github = source(response)
    with github.client, pytest.raises(RuntimeError, match="RequestTimeout"):
        github.pages("user/repos")


def test_missing_token_does_not_depend_on_cli_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Require explicit token injection even if a user might have gh installed locally."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Inject GH_TOKEN"):
        GitHub()


def test_nullable_source_and_milestone_metadata() -> None:
    """Deleted authors and optional milestones stay explicit without inventing source facts."""
    row = rest_item()
    row.update(
        user=None,
        assignees=[],
        milestone={
            "number": 3,
            "title": "Synthetic release",
            "state": "open",
            "due_on": None,
        },
    )
    metadata = source_metadata(row, repository())
    assert metadata["author"] is None and metadata["assignees"] == []
    assert metadata["milestone"] == row["milestone"]


def test_team_and_unavailable_reviewer_metadata() -> None:
    """Team identities are retained and an unavailable reviewer makes coverage incomplete."""
    pr = item("pr")
    team = {"__typename": "Team", "slug": "synthetic-team", "organization": {"login": "example"}}
    pr["reviewRequests"] = {
        "totalCount": 2,
        "nodes": [{"requestedReviewer": team}, {"requestedReviewer": None}],
    }
    metadata = node_metadata(pr)["pull_request"]
    assert metadata["requested_reviewers"] == [team]
    assert metadata["requested_reviewers_complete"] is False


def test_delta_uses_all_states_and_refreshes_unchanged_pr() -> None:
    """An empty REST delta still reads live PR signals and retains known common metadata."""
    pr = item("pr")
    bead = {
        "labels": ["github:help%20wanted"],
        "metadata": {
            "hub": {
                "node_id": pr["id"],
                "github": pr.pop("github"),
            }
        },
    }
    since = "2026-10-21T12:00:00+00:00"
    requests: list[str] = []

    def response(request: httpx.Request) -> httpx.Response:
        """Require an all-state incremental query and serve fresh GraphQL evidence."""
        requests.append(request.method)
        if request.method == "GET":
            assert request.url.params["state"] == "all"
            assert request.url.params["since"] == since
            return httpx.Response(200, json=[])
        pr["reviewDecision"] = "CHANGES_REQUESTED"
        return httpx.Response(200, json={"data": {"node": pr}})

    github = source(response)
    with github.client:
        rows = github.items(repository(), since=since, prs=[bead])
    assert requests == ["GET", "POST"]
    assert len(rows) == 1 and rows[0]["kind"] == "pr"
    assert rows[0]["labels"] == ["help wanted"]
    assert rows[0]["github"]["author"]["login"] == "synthetic-author"
    assert rows[0]["github"]["pull_request"]["review_decision"] == "CHANGES_REQUESTED"


@pytest.mark.parametrize("repair", ["hourly", "forced", "rename"])
@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_full_repair_preserves_terminal_delta(tmp_path: Path, repair: str, kind: str) -> None:
    """Repairs import work created and closed since the cursor before advancing coverage."""
    repo = repository()
    state = SyncState(tmp_path / "progress.json")
    started = time.time() - 120
    state.mark_success(repo, started=started, finished=started + 1, full=True)
    if repair == "hourly":
        state.data["repositories"]["R_1"]["last_full_at"] -= FULL_SCAN_SECONDS
    elif repair == "rename":
        repo["full_name"] = "example/renamed"
    terminal = item(kind, number=2, repo=repo)
    terminal["state"] = "MERGED" if kind == "pr" else "CLOSED"
    terminal_row = dict(rest_item(number=2), node_id=terminal["id"], state="closed")
    if kind == "pr":
        terminal_row["pull_request"] = {}
    queries: list[str] = []

    def response(request: httpx.Request) -> httpx.Response:
        """Separate the open inventory from terminal work visible only in the delta."""
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"node": terminal}})
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=[repo])
        query = request.url.params["state"]
        queries.append(query)
        if query == "all":
            since = datetime.fromisoformat(request.url.params["since"]).timestamp()
            assert since == pytest.approx(started - OVERLAP_SECONDS, rel=0, abs=1e-6)
        return httpx.Response(200, json=[rest_item()] if query == "open" else [terminal_row])

    github, beads = source(response), MemoryBeads()
    with github.client:
        result = sync.reconcile(github, beads, "user:@me", state, force_full=repair == "forced")
    assert result["status"] == "complete" and result["modes"] == {repo["full_name"]: "full"}
    assert result["closed"] == 1 and len(beads.rows) == 2
    assert sorted(queries) == ["all", "open"]
    assert any(
        row["metadata"]["hub"]["source_state"] == terminal["state"] for row in beads.rows.values()
    )
    assert SyncState(state.path).data["repositories"]["R_1"]["checkpoint"] > started


@pytest.mark.parametrize("failed_collection", ["open", "all"])
def test_repair_page_failure_retains_delta_coverage(tmp_path: Path, failed_collection: str) -> None:
    """Neither repair collection can fail after a page and still advance the saved cursor."""
    state = SyncState(tmp_path / "progress.json")
    started = time.time() - 120
    state.mark_success(repository(), started=started, finished=started + 1, full=True)
    state.save()
    fail = True

    def response(request: httpx.Request) -> httpx.Response:
        """Fail a later page, then allow replay of both open and terminal records."""
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=[repository()])
        collection = request.url.params["state"]
        if fail and collection == failed_collection:
            if request.url.params.get("page") == "2":
                return httpx.Response(503, json={"message": "Synthetic repair outage"})
            return httpx.Response(
                200,
                json=[rest_item()],
                headers={"Link": f'<{request.url.copy_set_param("page", "2")}>; rel="next"'},
            )
        rows = (
            [rest_item()] if collection == "open" else [dict(rest_item(number=2), state="closed")]
        )
        return httpx.Response(200, json=rows)

    github, beads = source(response), MemoryBeads()
    with github.client:
        result = sync.reconcile(github, beads, "user:@me", state, force_full=True)
        assert result["status"] == "partial" and result["writes"] == 0
        assert not beads.rows
        saved = SyncState(state.path)
        assert saved.data["repositories"]["R_1"]["checkpoint"] == started
        assert saved.data["repositories"]["R_1"]["error"]
        fail = False
        result = sync.reconcile(github, beads, "user:@me", saved, force_full=True)
    assert result["status"] == "complete" and result["closed"] == 1
    assert len(beads.rows) == 2
    assert SyncState(state.path).data["repositories"]["R_1"]["checkpoint"] > started


def test_repair_deduplicates_prs_before_detail_reads() -> None:
    """A PR in both collections and the local inventory needs one live signal read."""
    pr = item("pr")
    row = dict(rest_item(), node_id=pr["id"], pull_request={})
    node_reads = 0

    def response(request: httpx.Request) -> httpx.Response:
        """Return a stale label in the open scan and its newer value in the following delta."""
        nonlocal node_reads
        if request.method == "POST":
            node_reads += 1
            return httpx.Response(200, json={"data": {"node": pr}})
        label = "new" if request.url.params["state"] == "all" else "old"
        return httpx.Response(200, json=[dict(row, labels=[{"name": label}])])

    github = source(response)
    with github.client:
        rows = github.items(
            repository(),
            since="2026-10-21T12:00:00Z",
            full=True,
            prs=[{"metadata": {"hub": {"node_id": pr["id"]}}}],
        )
    assert len(rows) == 1 and rows[0]["labels"] == ["new"]
    assert node_reads == 1


def test_threaded_node_reads_are_bounded_and_joined() -> None:
    """Two workers overlap I/O while each owns its session and all reads finish before return."""
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5)
    active = peak = completed = 0
    threads: set[str] = set()

    def response(request: httpx.Request) -> httpx.Response:
        """Synchronize the first request from each worker without relying on timing thresholds."""
        nonlocal active, peak, completed
        node_id = json.loads(request.content)["variables"]["id"]
        with lock:
            first = threading.current_thread().name not in threads
            threads.add(threading.current_thread().name)
            active += 1
            peak = max(peak, active)
        if first:
            barrier.wait()
        row = dict(item("pr"), id=node_id)
        with lock:
            active -= 1
            completed += 1
        return httpx.Response(200, json={"data": {"node": row}})

    github = source(response, workers=2)
    ids = [f"synthetic-pr-{number}" for number in range(8)]
    with github.client:
        assert set(github.nodes(ids)) == set(ids)
    assert completed == 8 and active == 0 and peak == 2
    assert len(threads) == 2


def test_rate_reset_stops_pagination_before_another_request() -> None:
    """A successful page exhausting the REST budget prevents the next page from being sent."""
    requests = 0
    reset = int(time.time()) + 600

    def response(request: httpx.Request) -> httpx.Response:
        """Exhaust the budget while advertising another required page."""
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json=[rest_item()],
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(reset),
                "Link": '<https://api.github.com/repos/example/orbit/issues?page=2>; rel="next"',
            },
        )

    github = source(response)
    with github.client, pytest.raises(RateLimited) as error:
        github.items(repository())
    assert requests == 1 and error.value.retry_at == reset


def test_graphql_rate_error_records_reset() -> None:
    """HTTP 200 GraphQL rate failures preserve their cooldown rather than accepting partial data."""
    reset = int(time.time()) + 600

    def response(request: httpx.Request) -> httpx.Response:
        """Provide a partial GraphQL result carrying a rate-limit error and deadline."""
        return httpx.Response(
            200,
            json={
                "data": {
                    "rateLimit": {
                        "remaining": 0,
                        "resetAt": datetime.fromtimestamp(reset, UTC).isoformat(),
                    }
                },
                "errors": [{"type": "RATE_LIMITED", "message": "Synthetic rate limit"}],
            },
        )

    github = source(response)
    with github.client, pytest.raises(RateLimited) as error:
        github.node("synthetic")
    assert error.value.retry_at == reset


def test_rate_cooldown_survives_worker_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An API throttling deadline survives restart and blocks another authenticated attempt."""
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads/metadata.json").write_text("{}")
    monkeypatch.setenv("BEADS_WORKING_DIR", str(tmp_path))
    requests = 0

    def response(request: httpx.Request) -> httpx.Response:
        """Rate-limit repository discovery with an explicit Retry-After interval."""
        nonlocal requests
        requests += 1
        return httpx.Response(
            429, json={"message": "Synthetic throttle"}, headers={"Retry-After": "600"}
        )

    github = source(response)
    monkeypatch.setattr(sync, "GitHub", lambda **kwargs: github)
    monkeypatch.setattr(sync, "Beads", MemoryBeads)
    assert sync.run_sync()["status"] == "partial"
    saved = SyncState(tmp_path / ".github-sync-state.json")
    assert saved.data["retry_at"] > time.time() + 590
    assert sync.run_sync()["retry_at"] == saved.data["retry_at"]
    assert requests == 1


@pytest.mark.parametrize("workers", [0, 5])
def test_invalid_worker_count_rejected(workers: int) -> None:
    """Reject accidental unbounded concurrency before credentials or network access."""
    with pytest.raises(ValueError, match="between 1 and 4"):
        GitHub(workers=workers)


def test_threaded_read_failure_prevents_repository_writes() -> None:
    """A failing worker rejects the staged repository before any bead is created."""
    prs = [item("pr", number=number) for number in (1, 2)]
    rows = [dict(rest_item(pr["number"]), node_id=pr["id"], pull_request={}) for pr in prs]

    def response(request: httpx.Request) -> httpx.Response:
        """Serve one repository with two PRs, one of which is inaccessible by node ID."""
        if request.method == "GET":
            return httpx.Response(
                200, json=[repository()] if request.url.path == "/user/repos" else rows
            )
        node_id = json.loads(request.content)["variables"]["id"]
        node = prs[0] if node_id == prs[0]["id"] else None
        return httpx.Response(200, json={"data": {"node": node}})

    github, beads = source(response, workers=2), MemoryBeads()
    with github.client:
        result = sync.reconcile(github, beads, "user:@me")
    assert result["status"] == "partial" and result["errors"]
    assert not beads.rows and result["writes"] == 0
