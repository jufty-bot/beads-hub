"""Exercise the real GitHubKit HTTP boundary with synthetic, token-free responses."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from fixtures import item, repository
from github_source import GitHub, Json, node_metadata, source_metadata
from githubkit import GitHub as Client


def source(handler: Callable[[httpx.Request], httpx.Response]) -> GitHub:
    """Build a real SDK client whose network is replaced by an in-memory transport."""
    return GitHub(
        Client(
            "synthetic-token",
            transport=httpx.MockTransport(handler),
            http_cache=False,
            auto_retry=False,
        )
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
        issue, pull = github.open_items(repository())
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
        github.open_items(repository())
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
