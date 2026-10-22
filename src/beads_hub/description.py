"""Render linked GitHub summaries without changing the original source body."""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote, urlsplit


def text(value: Any) -> str:
    """Keep source text inside a single Markdown table cell."""
    value = "Unknown" if value is None or value == "" else str(value)
    value = " ".join(value.splitlines())
    return re.sub(r"([\\`*_{}\[\]()!|])", r"\\\1", html.escape(value, quote=False))


def link(label: Any, url: str | None) -> str:
    """Escape link text/destinations and retain plain text for unavailable or unsafe URLs."""
    display = text(label)
    if not url:
        return display
    try:
        parsed = urlsplit(url)
    except ValueError:
        return display
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return display
    destination = quote(url, safe="/:?#[]@!$&'*,;=+%~")
    return f"[{display}]({destination})"


def profile(login: str, url: str | None = None) -> str:
    """Prefer source profile URLs, especially for bots whose profiles live under /apps/."""
    fallback = None if login.endswith("[bot]") else f"https://github.com/{quote(login, safe='')}"
    return link(login, url or fallback)


def describe(item: dict[str, Any], labels: set[str]) -> str:
    """Present source identity, attention reasons, and linked facts above the verbatim body."""
    repo = item["repository"]["nameWithOwner"]
    repo_url = f"https://github.com/{repo}"
    source = item.get("github") or {}
    urls = source.get("links") or {}
    author = source.get("author") or {}
    milestone = source.get("milestone") or {}
    source_link = link(f"{repo}#{item['number']}", item["url"])
    rows = [
        ("GitHub", f"{source_link} · {item['kind']}"),
        ("Repository", link(repo, repo_url)),
        (
            "Author",
            profile(author["login"], urls.get("author")) if author.get("login") else "Unknown",
        ),
        (
            "GitHub assignees",
            ", ".join(
                profile(login, (urls.get("assignees") or {}).get(login))
                for login in source.get("assignees") or []
            )
            or "None",
        ),
        (
            "Milestone",
            link(
                milestone.get("title"),
                urls.get("milestone")
                or (
                    f"{repo_url}/milestone/{milestone['number']}"
                    if milestone.get("number")
                    else None
                ),
            )
            if milestone
            else "None",
        ),
        ("Created upstream", text(source.get("created_at"))),
        ("Updated upstream", text(source.get("updated_at"))),
        ("Comments", link(source.get("comment_count"), item["url"])),
    ]
    reasons = {
        "ci:failing": "CI failing",
        "ci:unknown": "CI unknown",
        "ci:pending": "CI pending",
        "conflict:true": "Merge conflicts",
        "conflict:unknown": "Mergeability unknown",
        "review:changes-requested": "Changes requested",
        "review:needed": "Review needed",
        "draft:true": "Draft",
    }
    attention = [message for label, message in reasons.items() if label in labels]
    summary = "Needs attention: " + " · ".join(attention) if attention else "No triage flags"
    if item["state"] != "OPEN":
        summary = "Merged upstream" if item["state"] == "MERGED" else "Closed upstream"
    elif item["kind"] == "issue":
        summary = "Open upstream issue"
    pr = source.get("pull_request")
    if pr:
        head_repo = pr.get("head_repository") or {}
        head_url = head_repo.get("url")
        if not head_url and pr.get("cross_repository") is False:
            head_url = repo_url
        head = pr.get("head_ref")
        base = pr.get("base_ref")
        head_link = link(
            head,
            f"{head_url}/tree/{quote(pr.get('head_sha') or head, safe='')}"
            if head and head_url
            else None,
        )
        base_link = link(
            base, f"{repo_url}/tree/{quote(pr.get('base_sha') or base, safe='')}" if base else None
        )
        reviewers = []
        for reviewer in pr.get("requested_reviewers") or []:
            if reviewer.get("login"):
                reviewers.append(profile(reviewer["login"], reviewer.get("url")))
            elif reviewer.get("slug"):
                org = (reviewer.get("organization") or {}).get("login")
                name = f"{org}/{reviewer['slug']}" if org else reviewer["slug"]
                reviewers.append(
                    link(
                        name,
                        reviewer.get("url")
                        or (
                            f"https://github.com/orgs/{org}/teams/"
                            f"{quote(reviewer['slug'], safe='')}"
                            if org
                            else None
                        ),
                    )
                )
            else:
                reviewers.append(text(reviewer.get("__typename")))
        review_text = ", ".join(reviewers) or "None"
        if pr.get("requested_reviewers_complete") is not True:
            review_text = (", ".join(reviewers) or "Unknown") + " (coverage incomplete)"
        closing = []
        for issue in item.get("closingIssuesReferences", {}).get(
            "nodes", pr.get("closing_issues", [])
        ):
            name = (issue.get("repository") or {}).get("nameWithOwner")
            number = issue.get("number")
            label = f"{name}#{number}" if name and number else issue["id"]
            closing.append(link(label, issue.get("url")))
        rows.extend(
            [
                ("Branch", f"{head_link} → {base_link}"),
                (
                    "Changes",
                    f"+{text(pr.get('additions'))} / −{text(pr.get('deletions'))} · "
                    f"{text(pr.get('changed_files'))} files · "
                    f"{text(pr.get('commit_count'))} commits",
                ),
                ("CI", link(pr.get("ci_state"), f"{item['url']}/checks")),
                ("Review", link(pr.get("review_decision"), f"{item['url']}/files")),
                ("Merge", text(pr.get("merge_state_status"))),
                ("Reviewers requested", review_text),
                ("Closes", ", ".join(closing) or "None"),
            ]
        )
    table = "\n".join(
        ["| Field | Value |", "| --- | --- |", *[f"| {field} | {value} |" for field, value in rows]]
    )
    return f"**{summary}**\n\n{table}\n\n---\n\n{item.get('body') or ''}"
