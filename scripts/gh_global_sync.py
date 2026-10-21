#!/usr/bin/env python3
"""Reconcile credential-visible GitHub issues and PRs into a shared Beads backlog.

GitHub owns source text, source labels, and closure. Humans own priority, assignee,
notes, categories, and explicit status overrides. A filesystem lock serializes hub
reconcilers; all database access goes through bd, never direct SQL writes.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

Json = dict[str, Any]
"""JSON objects exchanged with gh and bd."""

MANAGED_PREFIXES = ("repo:", "type:", "github:", "ci:", "conflict:", "review:", "draft:")
"""Label namespaces owned exclusively by reconciliation."""

NODE_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    __typename
    ... on Issue { id number title body state url repository { id nameWithOwner } }
    ... on PullRequest {
      id number title body state url repository { id nameWithOwner }
      isDraft mergeable reviewDecision headRefName
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      closingIssuesReferences(first: 100, after: $cursor) {
        nodes { id number repository { id nameWithOwner } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
"""Node identity survives repository renames and transfers."""


def command(args: list[str]) -> Any:
    """Run a bounded noninteractive command and decode its JSON result."""
    workspace = os.environ.get("BEADS_WORKING_DIR")
    result = subprocess.run(
        args, cwd=workspace, capture_output=True, text=True, timeout=180, check=False
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr.strip()[:500]}")
    return json.loads(result.stdout) if result.stdout.strip() else None


class GitHub:
    """Read GitHub through gh using its securely injected runtime credentials."""

    def pages(self, endpoint: str) -> list[Json]:
        """Read an entire REST collection, without using GitHub search."""
        rows: list[Json] = []
        separator = "&" if "?" in endpoint else "?"
        for page in range(1, 100_001):
            batch = command(["gh", "api", f"{endpoint}{separator}per_page=100&page={page}"])
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise ValueError(f"Invalid GitHub collection: {endpoint}")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise ValueError(f"Pagination did not terminate: {endpoint}")

    def repositories(self, scopes: str) -> list[Json]:
        """Resolve explicit user/org/repo scopes; reject unsupported search predicates."""
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
            if kind == "repo":
                if name.count("/") != 1:
                    raise ValueError("repo: requires OWNER/REPO")
                repos = [command(["gh", "api", f"repos/{name}"])]
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
            args = ["gh", "api", "graphql", "-f", f"query={NODE_QUERY}", "-f", f"id={node_id}"]
            if cursor:
                args.extend(["-f", f"cursor={cursor}"])
            payload = command(args)
            if payload.get("errors"):
                raise ValueError(f"GraphQL returned errors for node {node_id}")
            node = (payload.get("data") or {}).get("node")
            if not node or node.get("id") != node_id:
                raise ValueError(f"GitHub node {node_id} is unavailable; retaining its local state")
            if node["__typename"] == "Issue":
                return dict(node)
            connection = node["closingIssuesReferences"]
            links.extend(connection["nodes"])
            info = connection["pageInfo"]
            if not info["hasNextPage"]:
                node["closingIssuesReferences"]["nodes"] = links
                return dict(node)
            cursor = info["endCursor"]
            if not cursor or cursor in seen:
                raise ValueError(f"GitHub repeated a closing-reference cursor for {node_id}")
            seen.add(cursor)

    def open_items(self, repo: Json) -> list[Json]:
        """Stage a full repository before writing, including all source labels and PR signals."""
        rows = self.pages(f"repos/{repo['full_name']}/issues?state=open&sort=created&direction=asc")
        items: list[Json] = []
        for row in rows:
            kind = "pr" if "pull_request" in row else "issue"
            if kind == "pr":
                item = self.node(row["node_id"])
            else:
                item = {
                    "id": row["node_id"],
                    "number": row["number"],
                    "title": row["title"],
                    "body": row.get("body") or "",
                    "state": row["state"].upper(),
                    "url": row["html_url"],
                    "repository": {"id": repo["node_id"], "nameWithOwner": repo["full_name"]},
                }
            item["kind"] = kind
            item["labels"] = [label["name"] for label in row["labels"]]
            validate_item(item)
            items.append(item)
        return items


def validate_item(item: Json) -> None:
    """Reject malformed or indeterminate source states before applying repository writes."""
    for field in ("id", "title", "url"):
        if not isinstance(item.get(field), str) or not item[field]:
            raise ValueError(f"Missing GitHub field: {field}")
    if item.get("state") not in {"OPEN", "CLOSED", "MERGED"}:
        raise ValueError("Unknown GitHub state")
    if not isinstance(item["number"], int) or item["number"] < 1:
        raise ValueError("Invalid GitHub item number")
    if not item["repository"]["id"] or not item["repository"]["nameWithOwner"]:
        raise ValueError("Missing repository identity")
    evaluate(item)
    if item["kind"] == "pr":
        for link in item["closingIssuesReferences"]["nodes"]:
            if not link["id"]:
                raise ValueError("Missing closing-issue identity")


def source_ref(item: Json) -> str:
    """Build the human-readable external reference, independent of Beads issue IDs."""
    return f"gh:{item['repository']['nameWithOwner']}#{item['kind']}:{item['number']}"


def evaluate(item: Json) -> tuple[str, set[str]]:
    """Derive source-owned signals; uncertain CI or mergeability remains actionable."""
    labels = {f"repo:{item['repository']['nameWithOwner']}", f"type:{item['kind']}"}
    labels.update(f"github:{quote(label, safe='')}" for label in item["labels"])
    if item["state"] != "OPEN":
        return "closed", labels
    if item["kind"] == "issue":
        return "open", labels
    if item["isDraft"]:
        labels.add("draft:true")
    if item["mergeable"] == "CONFLICTING":
        labels.add("conflict:true")
    elif item["mergeable"] != "MERGEABLE":
        labels.add("conflict:unknown")
    commits = item["commits"]["nodes"]
    rollup = commits[-1]["commit"]["statusCheckRollup"] if commits else None
    state = rollup["state"] if rollup else None
    ci = {"FAILURE": "failing", "ERROR": "failing", "PENDING": "pending", "SUCCESS": "passing"}.get(
        state or "", "unknown"
    )
    labels.add(f"ci:{ci}")
    review = item["reviewDecision"]
    if review == "CHANGES_REQUESTED":
        labels.add("review:changes-requested")
    elif review != "APPROVED" and not item["isDraft"]:
        labels.add("review:needed")
    else:
        labels.add("review:approved" if review == "APPROVED" else "review:draft")
    actionable = bool(
        labels
        & {
            "conflict:true",
            "conflict:unknown",
            "ci:failing",
            "ci:unknown",
            "review:changes-requested",
            "review:needed",
        }
    )
    return ("open" if actionable else "in_progress"), labels


def csv_value(value: str) -> str:
    """Encode one label for Cobra's CSV-based StringSlice flags."""
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow([value])
    return out.getvalue()


class Beads:
    """Keep local workflow fields intact while updating source-owned fields through bd."""

    def call(self, *args: str) -> Any:
        """Invoke the configured bd executable with machine-readable output."""
        return command([os.environ.get("BEADS_PATH", "bd"), *args, "--json"])

    def items(self) -> list[Json]:
        """Load every source-backed bead, explicitly disabling the CLI result limit."""
        return [
            bead
            for bead in (self.call("list", "--all", "--limit", "0", "--flat") or [])
            if (bead.get("external_ref") or "").startswith("gh:")
        ]

    def metadata(self, bead: Json, hub: Json) -> None:
        """Merge only the hub metadata key, preserving metadata owned by local tools."""
        self.call("update", bead["id"], "--metadata", json.dumps({"hub": hub}))
        bead.setdefault("metadata", {})["hub"] = hub

    def close_source(self, bead: Json, state: str) -> None:
        """Apply verified upstream closure while retaining the last source snapshot."""
        hub = dict(bead["metadata"]["hub"])
        hub.update(source_state=state, synced_status="closed")
        self.call(
            "update",
            bead["id"],
            "--status",
            "closed",
            "--force",
            "--metadata",
            json.dumps({"hub": hub}),
        )
        bead["metadata"]["hub"] = hub
        bead["status"] = "closed"

    def upsert(self, item: Json, bead: Json | None) -> Json:
        """Refresh a source snapshot without clearing categories, claims, notes, or priority."""
        status, labels = evaluate(item)
        title = (
            f"[{item['repository']['nameWithOwner']} {item['kind']} #{item['number']}] "
            f"{item['title']}"
        )
        description = f"Upstream {item['kind']}: {item['url']}\n\n{item.get('body') or ''}"
        identity = {
            "node_id": item["id"],
            "repo_id": item["repository"]["id"],
            "repo": item["repository"]["nameWithOwner"],
            "kind": item["kind"],
            "number": item["number"],
            "source_state": item["state"],
        }
        if bead is None:
            bead = self.call(
                "create",
                "--title",
                title,
                "--type",
                "task",
                "--external-ref",
                source_ref(item),
                "--description",
                description,
                "--metadata",
                json.dumps({"hub": {**identity, "synced_status": "open"}}),
            )
        hub = dict((bead.get("metadata") or {}).get("hub") or {})
        current = bead["status"]
        last_status = hub.get("synced_status", "open")
        manual = "hub:manual-status" in (bead.get("labels") or [])
        status_args: list[str] = []
        if status == "closed":
            status_args = ["--status", "closed", "--force"]
            hub["synced_status"] = "closed"
        elif (
            not manual
            and current == last_status
            and (not bead.get("assignee") or hub.get("source_state") in {"CLOSED", "MERGED"})
        ):
            if bead.get("assignee"):
                status = "in_progress"
            status_args = [
                "--status",
                status,
                "--if-status",
                current,
                "--if-assignee",
                bead.get("assignee") or "",
            ]
            hub["synced_status"] = status
        args = [
            "update",
            bead["id"],
            "--title",
            title,
            "--description",
            description,
            "--external-ref",
            source_ref(item),
        ]
        current_labels = set(bead.get("labels") or [])
        for label in sorted(labels - current_labels):
            args.extend(["--add-label", csv_value(label)])
        for label in sorted(current_labels - labels):
            if label.startswith(MANAGED_PREFIXES):
                args.extend(["--remove-label", csv_value(label)])
        hub.update(identity)
        self.call(*args, *status_args, "--metadata", json.dumps({"hub": hub}))
        bead.setdefault("metadata", {})["hub"] = hub
        if status_args:
            bead["status"] = status
        bead["external_ref"] = source_ref(item)
        return bead

    def links(self, pr: Json, desired: set[str]) -> None:
        """Reconcile only hub-owned blocking edges, retaining preexisting manual dependencies."""
        hub = dict(pr["metadata"]["hub"])
        owned = set(hub.get("blocking_issues", []))
        for issue_id in sorted(owned - desired):
            self.call("dep", "remove", issue_id, pr["id"])
            owned.remove(issue_id)
            hub["blocking_issues"] = sorted(owned)
            self.metadata(pr, hub)
        for issue_id in sorted(desired - owned):
            dependencies = self.call("dep", "list", issue_id, "--type", "blocks") or []
            if any(dep["id"] == pr["id"] for dep in dependencies):
                continue
            # Persist intent first so an interrupted add can be repaired on the next run.
            owned.add(issue_id)
            hub["blocking_issues"] = sorted(owned)
            self.metadata(pr, hub)
        for issue_id in sorted(desired & owned):
            self.call("dep", "add", issue_id, pr["id"])


def reconcile(github: GitHub, beads: Beads, scopes: str) -> Json:
    """Apply complete repository snapshots; report errors and never infer closure from absence."""
    summary: Json = {"status": "complete", "issues": 0, "prs": 0, "closed": 0, "errors": []}
    repos = github.repositories(scopes)
    existing = beads.items()
    by_node: dict[str, Json] = {}
    by_ref: dict[str, Json] = {}
    for bead in existing:
        hub = (bead.get("metadata") or {}).get("hub") or {}
        node_id = hub.get("node_id")
        if node_id:
            if node_id in by_node:
                raise ValueError(f"Duplicate Beads source identity: {node_id}")
            by_node[node_id] = bead
        if bead["external_ref"] in by_ref:
            raise ValueError(f"Duplicate Beads external reference: {bead['external_ref']}")
        by_ref[bead["external_ref"]] = bead
    active_prs: list[tuple[Json, Json]] = []
    for repo in repos:
        try:
            items = github.open_items(repo)
            closing: list[tuple[Json, Json]] = []
            seen = {item["id"] for item in items}
            for bead in existing:
                hub = (bead.get("metadata") or {}).get("hub") or {}
                if (
                    hub.get("repo_id") == repo["node_id"]
                    and hub.get("node_id") not in seen
                    and bead["status"] != "closed"
                ):
                    item = github.node(hub["node_id"])
                    item["kind"] = hub["kind"]
                    item["labels"] = []
                    validate_item(item)
                    if item["state"] in {"CLOSED", "MERGED"}:
                        closing.append((item, bead))
            # All GitHub reads for this repository succeeded before any writes.
            for item in items:
                previous = by_node.get(item["id"]) or by_ref.get(source_ref(item))
                was_closed = previous is not None and previous["status"] == "closed"
                bead = beads.upsert(item, previous)
                by_node[item["id"]] = bead
                by_ref[source_ref(item)] = bead
                if item["state"] != "OPEN":
                    summary["closed"] += int(not was_closed)
                else:
                    summary["prs" if item["kind"] == "pr" else "issues"] += 1
                    if item["kind"] == "pr":
                        active_prs.append((item, bead))
            for item, bead in closing:
                beads.close_source(bead, item["state"])
                summary["closed"] += 1
        except (KeyError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
            summary["errors"].append(f"{repo['full_name']}: {error}")
    for item, bead in active_prs:
        try:
            targets = {
                by_node[link["id"]]["id"]
                for link in item["closingIssuesReferences"]["nodes"]
                if link["id"] in by_node and by_node[link["id"]]["status"] != "closed"
            }
            beads.links(bead, targets)
        except (KeyError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            summary["errors"].append(f"{source_ref(item)} dependencies: {error}")
    if summary["errors"]:
        summary["status"] = "partial"
    return summary


def run_sync() -> Json:
    """Serialize periodic, manual, and MCP syncs across containers sharing the workspace."""
    workspace = Path(os.environ.get("BEADS_WORKING_DIR", "/var/lib/beads"))
    if not (workspace / ".beads/metadata.json").is_file():
        return {"status": "partial", "errors": ["Hub is not initialized"]}
    with (workspace / ".github-sync.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "errors": []}
        try:
            return reconcile(GitHub(), Beads(), os.environ.get("GH_SYNC_QUERY", "user:@me"))
        except (OSError, KeyError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            return {"status": "partial", "errors": [str(error)]}


def main() -> None:
    """Print one JSON summary per pass; periodic failures remain visible and retry later."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="Reconcile periodically")
    parser.add_argument("--interval", type=int, default=120, help="Seconds between sync passes")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be positive")
    while True:
        summary = run_sync()
        print(json.dumps(summary), flush=True)
        if not args.loop:
            sys.exit(1 if summary["status"] == "partial" else 0)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
