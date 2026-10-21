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
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from github_source import GitHub

Json = dict[str, Any]
"""JSON objects exchanged with GitHub and bd."""

MANAGED_PREFIXES = ("repo:", "type:", "github:", "ci:", "conflict:", "review:", "draft:")
"""Label namespaces owned exclusively by reconciliation."""


def command(args: list[str]) -> Any:
    """Run a bounded noninteractive command and decode its JSON result."""
    workspace = os.environ.get("BEADS_WORKING_DIR")
    result = subprocess.run(
        args, cwd=workspace, capture_output=True, text=True, timeout=180, check=False
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr.strip()[:500]}")
    return json.loads(result.stdout) if result.stdout.strip() else None


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


def describe(item: Json) -> str:
    """Put a compact source summary before the body; local reasoning belongs in notes."""
    lines = [f"Upstream {item['kind']}: {item['url']}"]
    source = item.get("github") or {}
    if source:
        author = source.get("author") or {}
        lines.append(
            f"Author: {author.get('login') or 'unknown'} | "
            f"Created: {source.get('created_at') or 'unknown'} | "
            f"Updated: {source.get('updated_at') or 'unknown'}"
        )
        milestone = source.get("milestone") or {}
        lines.append(
            f"GitHub assignees: {', '.join(source.get('assignees') or []) or 'none'} | "
            f"Milestone: {milestone.get('title') or 'none'} | "
            f"Comments: {source.get('comment_count', 'unknown')}"
        )
        pr = source.get("pull_request")
        if pr:
            lines.extend(
                [
                    f"Branch: {pr.get('head_ref')} -> {pr.get('base_ref')} | "
                    f"Changes: +{pr.get('additions')} / -{pr.get('deletions')} "
                    f"in {pr.get('changed_files')} files",
                    f"CI: {pr.get('ci_state') or 'unknown'} | "
                    f"Review: {pr.get('review_decision') or 'none'} | "
                    f"Merge: {pr.get('merge_state_status') or 'unknown'}",
                ]
            )
    return "\n".join(lines) + f"\n\n{item.get('body') or ''}"


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

    def close_source(self, bead: Json, item: Json) -> None:
        """Apply verified closure and refresh evidence, retaining the last REST snapshot."""
        hub = dict(bead["metadata"]["hub"])
        hub.update(source_state=item["state"], synced_status="closed")
        hub["github"] = {**hub.get("github", {}), **item.get("github", {})}
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
        description = describe(item)
        identity = {
            "node_id": item["id"],
            "repo_id": item["repository"]["id"],
            "repo": item["repository"]["nameWithOwner"],
            "kind": item["kind"],
            "number": item["number"],
            "source_state": item["state"],
            "github": item.get("github", {}),
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
            for item in items:
                validate_item(item)
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
                beads.close_source(bead, item)
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
            github = GitHub()
            with github.client:
                return reconcile(github, Beads(), os.environ.get("GH_SYNC_QUERY", "user:@me"))
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
