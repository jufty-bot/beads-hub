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

from github_source import GitHub, RateLimited
from sync_state import SyncState

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


def snapshot_metadata(source: Json, previous: Json) -> Json:
    """Retain a stored observation time when the source facts have not changed."""
    source = dict(source)
    if {key: value for key, value in source.items() if key != "observed_at"} == {
        key: value for key, value in previous.items() if key != "observed_at"
    }:
        if "observed_at" in previous:
            source["observed_at"] = previous["observed_at"]
    return source


class Beads:
    """Keep local workflow fields intact while updating source-owned fields through bd."""

    writes: int = 0
    """Successful mutating CLI commands issued by this adapter instance."""

    def call(self, *args: str) -> Any:
        """Invoke the configured bd executable with machine-readable output."""
        result = command([os.environ.get("BEADS_PATH", "bd"), *args, "--json"])
        if args[0] in {"create", "update"} or args[:2] in {("dep", "add"), ("dep", "remove")}:
            self.writes += 1
        return result

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
        hub["github"] = snapshot_metadata(
            {**hub.get("github", {}), **item.get("github", {})}, hub.get("github", {})
        )
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
        previous_hub = dict(((bead or {}).get("metadata") or {}).get("hub") or {})
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
            "github": snapshot_metadata(item.get("github", {}), previous_hub.get("github", {})),
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
            if current != "closed":
                status_args = ["--status", "closed", "--force"]
            hub["synced_status"] = "closed"
        elif (
            not manual
            and current == last_status
            and (not bead.get("assignee") or hub.get("source_state") in {"CLOSED", "MERGED"})
        ):
            if bead.get("assignee"):
                status = "in_progress"
            if current != status:
                status_args = [
                    "--status",
                    status,
                    "--if-status",
                    current,
                    "--if-assignee",
                    bead.get("assignee") or "",
                ]
            hub["synced_status"] = status
        args = ["update", bead["id"]]
        fields = {"title": title, "description": description, "external_ref": source_ref(item)}
        for name, value in fields.items():
            if bead.get(name) != value:
                args.extend([f"--{name.replace('_', '-')}", value])
        current_labels = set(bead.get("labels") or [])
        for label in sorted(labels - current_labels):
            args.extend(["--add-label", csv_value(label)])
        for label in sorted(current_labels - labels):
            if label.startswith(MANAGED_PREFIXES):
                args.extend(["--remove-label", csv_value(label)])
        hub.update(identity)
        if hub != ((bead.get("metadata") or {}).get("hub") or {}):
            args.extend(["--metadata", json.dumps({"hub": hub})])
        if len(args) > 2 or status_args:
            self.call(*args, *status_args)
        bead.update(fields)
        bead["labels"] = sorted(
            labels | {label for label in current_labels if not label.startswith(MANAGED_PREFIXES)}
        )
        bead.setdefault("metadata", {})["hub"] = hub
        if status_args:
            bead["status"] = status
        return bead

    def links(self, pr: Json, desired: set[str]) -> None:
        """Reconcile only hub-owned blocking edges, retaining preexisting manual dependencies."""
        hub = dict(pr["metadata"]["hub"])
        owned = set(hub.get("blocking_issues", []))
        present = {
            issue_id
            for issue_id in desired | owned
            if any(
                dep["id"] == pr["id"]
                for dep in self.call("dep", "list", issue_id, "--type", "blocks") or []
            )
        }
        for issue_id in sorted(owned - desired):
            if issue_id in present:
                self.call("dep", "remove", issue_id, pr["id"])
            owned.remove(issue_id)
            hub["blocking_issues"] = sorted(owned)
            self.metadata(pr, hub)
        for issue_id in sorted(desired - owned):
            if issue_id in present:
                continue
            # Persist intent first so an interrupted add can be repaired on the next run.
            owned.add(issue_id)
            hub["blocking_issues"] = sorted(owned)
            self.metadata(pr, hub)
        for issue_id in sorted((desired & owned) - present):
            self.call("dep", "add", issue_id, pr["id"])


def reconcile(
    github: GitHub,
    beads: Beads,
    scopes: str,
    state: SyncState | None = None,
    *,
    force_full: bool = False,
) -> Json:
    """Apply staged deltas and PR evidence, committing progress only after graph updates."""
    state = state or SyncState()
    summary: Json = {
        "status": "complete",
        "issues": 0,
        "prs": 0,
        "closed": 0,
        "errors": [],
        "modes": {},
    }
    before_writes = beads.writes
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
    pending: dict[str, tuple[Json, float, bool]] = {}
    failed: set[str] = set()
    for repo in repos:
        started = time.time()
        since = None if force_full else state.since(repo, now=started)
        summary["modes"][repo["full_name"]] = "delta" if since else "full"
        try:
            local = [
                bead
                for bead in existing
                if (bead.get("metadata") or {}).get("hub", {}).get("repo_id") == repo["node_id"]
            ]
            prs = [
                bead
                for bead in local
                if bead["metadata"]["hub"].get("kind") == "pr"
                and bead["metadata"]["hub"].get("source_state") == "OPEN"
            ]
            items = github.items(repo, since=since, prs=prs)
            for item in items:
                validate_item(item)
            closing: list[tuple[Json, Json]] = []
            if since is None:
                seen = {item["id"] for item in items}
                missing = {
                    bead["metadata"]["hub"]["node_id"]: bead
                    for bead in local
                    if bead["metadata"]["hub"].get("node_id") not in seen
                    and (
                        bead["status"] != "closed"
                        or bead["metadata"]["hub"].get("source_state") not in {"CLOSED", "MERGED"}
                    )
                }
                for node_id, item in github.nodes(list(missing)).items():
                    bead = missing[node_id]
                    item.update(kind=bead["metadata"]["hub"]["kind"], labels=[])
                    validate_item(item)
                    if item["state"] in {"CLOSED", "MERGED"}:
                        closing.append((item, bead))
            # Complete source reads and validation precede this repository's writes.
            for item in items:
                previous = by_node.get(item["id"]) or by_ref.get(source_ref(item))
                already_closed = (
                    previous is not None
                    and previous["status"] == "closed"
                    and (
                        (previous.get("metadata") or {}).get("hub", {}).get("source_state")
                        in {"CLOSED", "MERGED"}
                    )
                )
                bead = beads.upsert(item, previous)
                by_node[item["id"]] = bead
                by_ref[source_ref(item)] = bead
                if item["state"] != "OPEN":
                    summary["closed"] += int(not already_closed)
                else:
                    summary["prs" if item["kind"] == "pr" else "issues"] += 1
                    if item["kind"] == "pr":
                        active_prs.append((item, bead))
            for item, bead in closing:
                beads.close_source(bead, item)
                summary["closed"] += 1
            pending[repo["node_id"]] = (repo, started, since is None)
        except RateLimited as error:
            state.data["retry_at"] = error.retry_at
            state.failure(repo, str(error))
            summary["errors"].append(f"{repo['full_name']}: {error}")
            summary["retry_at"] = error.retry_at
            break
        except (KeyError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
            failed.add(repo["node_id"])
            state.failure(repo, str(error))
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
            repo_id = item["repository"]["id"]
            failed.add(repo_id)
            if repo_id in pending:
                state.failure(pending[repo_id][0], str(error))
            summary["errors"].append(f"{source_ref(item)} dependencies: {error}")
    for repo_id, (repo, started, full) in pending.items():
        if repo_id not in failed:
            state.success(repo, started=started, finished=time.time(), full=full)
    if summary["errors"]:
        summary["status"] = "partial"
    summary["writes"] = beads.writes - before_writes
    return summary


def run_sync(*, force_full: bool = False) -> Json:
    """Serialize triggers and persist progress/rate cooldowns across container restarts."""
    workspace = Path(os.environ.get("BEADS_WORKING_DIR", "/var/lib/beads"))
    if not (workspace / ".beads/metadata.json").is_file():
        return {"status": "partial", "errors": ["Hub is not initialized"]}
    with (workspace / ".github-sync.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "errors": []}
        try:
            state = SyncState(workspace / ".github-sync-state.json")
            retry_at = float(state.data.get("retry_at", 0))
            if retry_at > time.time():
                raise RateLimited(retry_at)
            github = GitHub(workers=int(os.environ.get("GH_SYNC_WORKERS", "1")))
            try:
                with github.client:
                    return reconcile(
                        github,
                        Beads(),
                        os.environ.get("GH_SYNC_QUERY", "user:@me"),
                        state,
                        force_full=force_full,
                    )
            finally:
                state.data["retry_at"] = github.retry_at
                state.save()
        except RateLimited as error:
            return {"status": "partial", "errors": [str(error)], "retry_at": error.retry_at}
        except (
            OSError,
            KeyError,
            ValueError,
            TypeError,
            RuntimeError,
            subprocess.TimeoutExpired,
        ) as error:
            return {"status": "partial", "errors": [str(error)]}


def main() -> None:
    """Print one JSON summary per pass; periodic failures remain visible and retry later."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="Reconcile periodically")
    mode.add_argument("--status", action="store_true", help="Show durable polling progress")
    parser.add_argument("--full", action="store_true", help="Force a full open-backlog repair")
    parser.add_argument("--interval", type=int, default=120, help="Seconds between sync passes")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be positive")
    if args.status:
        workspace = Path(os.environ.get("BEADS_WORKING_DIR", "/var/lib/beads"))
        print(json.dumps(SyncState(workspace / ".github-sync-state.json").data))
        return
    while True:
        summary = run_sync(force_full=args.full)
        print(json.dumps(summary), flush=True)
        if not args.loop:
            sys.exit(1 if summary["status"] == "partial" else 0)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
