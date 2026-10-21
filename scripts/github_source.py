"""Read GitHub snapshots with GitHubKit, keeping source facts separate from local triage."""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any
from urllib.parse import unquote, urlencode

from githubkit import GitHub as Client
from githubkit import Response
from githubkit.exception import GitHubException, RateLimitExceeded, RequestFailed

Json = dict[str, Any]
"""JSON objects at the GitHub snapshot boundary."""

HEADERS = {"X-GitHub-Api-Version": "2022-11-28"}
"""Keep REST semantics stable independently of the SDK's generated schema version."""

NODE_QUERY = """
query($id: ID!, $cursor: String) {
  rateLimit { remaining resetAt }
  node(id: $id) {
    __typename
    ... on Issue {
      id number title body state url repository { id nameWithOwner }
      updatedAt closedAt stateReason
    }
    ... on PullRequest {
      id number title body state url repository { id nameWithOwner }
      updatedAt closedAt mergedAt
      isDraft mergeable mergeStateStatus reviewDecision
      headRefName headRefOid baseRefName baseRefOid
      additions deletions changedFiles isCrossRepository
      commits(last: 1) {
        totalCount
        nodes { commit { statusCheckRollup { state } } }
      }
      reviewRequests(first: 100) {
        totalCount
        nodes {
          requestedReviewer {
            __typename
            ... on User { login }
            ... on Bot { login }
            ... on Team { slug organization { login } }
          }
        }
      }
      closingIssuesReferences(first: 100, after: $cursor) {
        nodes { id number repository { id nameWithOwner } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
"""Read PR evidence and authoritative closure by stable node identity."""


class RateLimited(RuntimeError):
    """Stop source reads until a GitHub-provided deadline, shared by all read workers."""

    def __init__(self, retry_at: float) -> None:
        """Expose a safe deadline without including response bodies or credentials."""
        self.retry_at = retry_at
        super().__init__(
            f"GitHub rate limited until {datetime.fromtimestamp(retry_at, UTC).isoformat()}"
        )


def observed_at() -> str:
    """Timestamp an observation without treating GitHub updated_at as a CI watermark."""
    return datetime.now(UTC).isoformat()


def source_metadata(row: Json, repo: Json) -> Json:
    """Select useful issue facts without copying profiles, emails, or source payloads wholesale."""
    author = row.get("user")
    milestone = row.get("milestone")
    return {
        "url": row["html_url"],
        "author": {"login": author["login"], "type": author.get("type")} if author else None,
        "author_association": row.get("author_association"),
        "assignees": [assignee["login"] for assignee in row.get("assignees", [])],
        "milestone": {key: milestone.get(key) for key in ("number", "title", "state", "due_on")}
        if milestone
        else None,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "closed_at": row.get("closed_at"),
        "comment_count": row.get("comments"),
        "locked": row.get("locked"),
        "state_reason": row.get("state_reason"),
        "repository": {
            key: repo.get(key)
            for key in ("visibility", "default_branch", "language", "stargazers_count", "fork")
        },
        "observed_at": observed_at(),
    }


def node_metadata(node: Json) -> Json:
    """Retain PR evidence, including an explicit coverage flag for bounded reviewer details."""
    metadata: Json = {
        "updated_at": node.get("updatedAt"),
        "closed_at": node.get("closedAt"),
        "observed_at": observed_at(),
    }
    if node["__typename"] == "Issue":
        metadata["state_reason"] = (node.get("stateReason") or "").lower() or None
        return metadata
    requests = node.get("reviewRequests") or {}
    reviewers = [
        request["requestedReviewer"]
        for request in requests.get("nodes", [])
        if request and request.get("requestedReviewer")
    ]
    metadata["pull_request"] = {
        "draft": node["isDraft"],
        "mergeable": node["mergeable"],
        "merge_state_status": node.get("mergeStateStatus"),
        "review_decision": node["reviewDecision"],
        "head_ref": node["headRefName"],
        "head_sha": node.get("headRefOid"),
        "base_ref": node.get("baseRefName"),
        "base_sha": node.get("baseRefOid"),
        "additions": node.get("additions"),
        "deletions": node.get("deletions"),
        "changed_files": node.get("changedFiles"),
        "commit_count": node["commits"].get("totalCount"),
        "cross_repository": node.get("isCrossRepository"),
        "merged_at": node.get("mergedAt"),
        "requested_reviewers": reviewers,
        "requested_reviewers_total": requests.get("totalCount"),
        "requested_reviewers_complete": (
            requests.get("totalCount") is not None and len(reviewers) == requests["totalCount"]
        ),
    }
    commits = node["commits"]["nodes"]
    rollup = commits[-1]["commit"].get("statusCheckRollup") if commits else None
    metadata["pull_request"]["ci_state"] = rollup["state"] if rollup else None
    return metadata


class GitHub:
    """Use a synchronous SDK client so CLI and MCP share the same reconciliation lifecycle."""

    def __init__(
        self, client: Client[Any] | None = None, *, workers: int = 1, retry_at: float = 0
    ) -> None:
        """Accept a test client or authenticate directly from the injected GH_TOKEN."""
        if not 1 <= workers <= 4:
            raise ValueError("GH_SYNC_WORKERS must be between 1 and 4")
        if client is None:
            token = os.environ.get("GH_TOKEN")
            if not token:
                raise ValueError("Inject GH_TOKEN to sync GitHub; gh CLI login is not used")
            client = Client(
                token, user_agent="beads-hub", timeout=30, http_cache=False, auto_retry=False
            )
        self.client = client
        self.workers = workers
        self.retry_at = retry_at
        self.rate_lock = Lock()

    def check_rate(self) -> None:
        """Prevent new requests while a prior request has exhausted the shared API budget."""
        with self.rate_lock:
            if self.retry_at > time.time():
                raise RateLimited(self.retry_at)

    def defer(self, retry_at: float) -> None:
        """Let in-flight workers extend, but never shorten, the shared cooldown."""
        with self.rate_lock:
            self.retry_at = max(self.retry_at, retry_at)

    def observe_headers(self, headers: Any) -> None:
        """Record a primary-budget reset even when the last allowed REST request succeeds."""
        if headers.get("x-ratelimit-remaining") == "0":
            self.defer(float(headers.get("x-ratelimit-reset", time.time() + 60)))

    def failure(self, error: GitHubException, operation: str) -> RuntimeError:
        """Preserve rate-limit deadlines and keep arbitrary API error bodies out of logs."""
        deadline = time.time() + 60
        limited = isinstance(error, RateLimitExceeded)
        if limited:
            assert isinstance(error, RateLimitExceeded)
            deadline = time.time() + max(60, error.retry_after.total_seconds())
        if isinstance(error, RequestFailed):
            headers = error.response.headers
            limited |= error.response.status_code == 429
            limited |= "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0"
            retry = headers.get("retry-after")
            if retry:
                try:
                    deadline = time.time() + float(retry)
                except ValueError:
                    deadline = parsedate_to_datetime(retry).timestamp()
            if headers.get("x-ratelimit-remaining") == "0":
                deadline = max(deadline, float(headers.get("x-ratelimit-reset", deadline)))
            if limited and error.response.status_code == 200:
                rate = (error.response.json().get("data") or {}).get("rateLimit") or {}
                if rate.get("resetAt"):
                    deadline = max(deadline, datetime.fromisoformat(rate["resetAt"]).timestamp())
        if limited:
            self.defer(deadline)
            return RateLimited(self.retry_at)
        status = f" HTTP {error.response.status_code}" if isinstance(error, RequestFailed) else ""
        return RuntimeError(f"GitHub {type(error).__name__}{status} {operation}")

    def pages(self, endpoint: str) -> list[Json]:
        """Let GitHubKit follow REST Link headers, validating each page before use."""

        def collection(response: Response[Any, Any]) -> list[Json]:
            """Reject malformed collections instead of accepting incomplete inventories."""
            rows = response.json()
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError(f"Invalid GitHub collection: {endpoint}")
            self.observe_headers(response.headers)
            if response.raw_response.links.get("next"):
                self.check_rate()
            return rows

        separator = "&" if "?" in endpoint else "?"
        try:
            self.check_rate()
            return list(
                self.client.rest.paginate(
                    self.client.request,
                    map_func=collection,
                    method="GET",
                    url=f"/{endpoint}{separator}per_page=100",
                    headers=HEADERS,
                )
            )
        except GitHubException as error:
            raise self.failure(error, f"listing {endpoint}") from error

    def repositories(self, scopes: str) -> list[Json]:
        """Resolve credential-visible user/org/repo inventories without search truncation."""
        selected: dict[str, Json] = {}
        parts = [part.strip() for part in scopes.split(";") if part.strip()]
        if not parts:
            raise ValueError("GH_SYNC_QUERY must contain at least one user:, org:, or repo: scope")
        for part in parts:
            if not re.fullmatch(r"(?:user|org|repo):[A-Za-z0-9_.@/-]+", part):
                raise ValueError(
                    f"Unsupported scope {part!r}; use user:NAME, org:NAME, repo:OWNER/REPO"
                )
            kind, name = part.split(":", 1)
            repos: list[Json]
            if kind == "repo":
                if name.count("/") != 1:
                    raise ValueError("repo: requires OWNER/REPO")
                owner, repo_name = name.split("/")
                try:
                    self.check_rate()
                    response = self.client.rest.repos.get(owner, repo_name, headers=HEADERS)
                    self.observe_headers(response.headers)
                    repos = [dict(response.json())]
                except GitHubException as error:
                    raise self.failure(error, f"reading {name}") from error
            elif name == "@me" and kind == "user":
                repos = self.pages("user/repos?affiliation=owner")
            else:
                if "/" in name or "@" in name:
                    raise ValueError("user:/org: requires a literal account name (or user:@me)")
                endpoint = f"orgs/{name}/repos?type=all" if kind == "org" else f"users/{name}/repos"
                repos = self.pages(endpoint)
            for repo in repos:
                if not repo.get("archived") and not repo.get("disabled"):
                    selected[repo["node_id"]] = repo
        return sorted(selected.values(), key=lambda repo: repo["full_name"].lower())

    def node(self, node_id: str) -> Json:
        """Read explicit source state and exhaust a PR's closing-issue references."""
        cursor: str | None = None
        links: list[Json] = []
        seen: set[str] = set()
        while True:
            try:
                self.check_rate()
                payload = self.client.graphql(
                    NODE_QUERY, variables={"id": node_id, "cursor": cursor}
                )
            except GitHubException as error:
                raise self.failure(error, f"reading node {node_id}") from error
            rate = payload.get("rateLimit") or {}
            if rate.get("remaining") == 0:
                self.defer(datetime.fromisoformat(rate["resetAt"]).timestamp())
            node = payload.get("node")
            if not node or node.get("id") != node_id:
                raise ValueError(f"GitHub node {node_id} is unavailable; retaining its local state")
            if node["__typename"] == "Issue":
                node["github"] = node_metadata(node)
                return dict(node)
            connection = node["closingIssuesReferences"]
            links.extend(connection["nodes"])
            info = connection["pageInfo"]
            if not info["hasNextPage"]:
                node["closingIssuesReferences"]["nodes"] = links
                node["github"] = node_metadata(node)
                return dict(node)
            cursor = info["endCursor"]
            if not cursor or cursor in seen:
                raise ValueError(f"GitHub repeated a closing-reference cursor for {node_id}")
            seen.add(cursor)

    def nodes(self, node_ids: list[str]) -> dict[str, Json]:
        """Bound independent node reads; only workers use threads, never Beads or MCP context."""
        node_ids = list(dict.fromkeys(node_ids))
        if self.workers == 1 or len(node_ids) < 2:
            return {node_id: self.node(node_id) for node_id in node_ids}

        def partition(ids: list[str]) -> list[Json]:
            """Reuse a thread-local SDK session and close it before joining the worker."""
            with self.client:
                return [self.node(node_id) for node_id in ids]

        workers = min(self.workers, len(node_ids))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="github-read") as pool:
            parts = [node_ids[index::workers] for index in range(workers)]
            return {node["id"]: node for group in pool.map(partition, parts) for node in group}

    def items(
        self, repo: Json, *, since: str | None = None, prs: list[Json] | None = None
    ) -> list[Json]:
        """Stage an open inventory or all-state delta and refresh known PRs independently."""
        query = urlencode({"state": "all", "since": since}) if since else "state=open"
        rows = self.pages(f"repos/{repo['full_name']}/issues?{query}&sort=updated&direction=asc")
        previous = {bead["metadata"]["hub"]["node_id"]: bead for bead in prs or []}
        details = self.nodes(
            [row["node_id"] for row in rows if "pull_request" in row] + list(previous)
        )
        items: list[Json] = []
        for row in rows:
            kind = "pr" if "pull_request" in row else "issue"
            item = (
                details[row["node_id"]]
                if kind == "pr"
                else {
                    "id": row["node_id"],
                    "number": row["number"],
                    "title": row["title"],
                    "body": row.get("body") or "",
                    "state": row["state"].upper(),
                    "url": row["html_url"],
                    "repository": {"id": repo["node_id"], "nameWithOwner": repo["full_name"]},
                }
            )
            item["kind"] = kind
            item["labels"] = [label["name"] for label in row["labels"]]
            item["github"] = {**source_metadata(row, repo), **item.get("github", {})}
            items.append(item)
            previous.pop(item["id"], None)
        for node_id, bead in previous.items():
            node = details[node_id]
            node.update(
                kind="pr",
                labels=[
                    unquote(label.removeprefix("github:"))
                    for label in bead.get("labels") or []
                    if label.startswith("github:")
                ],
            )
            node["github"] = {
                **bead["metadata"]["hub"].get("github", {}),
                **node["github"],
            }
            items.append(node)
        return items
