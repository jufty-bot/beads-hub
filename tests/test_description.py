"""Check actionable linked summaries, missing source facts, and literal Markdown handling."""

from __future__ import annotations

import copy

import pytest
from fixtures import FixtureGitHub, item, repository
from test_sync import MemoryBeads

from beads_hub.description import describe, link
from beads_hub.github import node_metadata, source_metadata
from beads_hub.sync import evaluate, reconcile


def render(row: dict[str, object]) -> str:
    """Render with the same signals as reconciliation."""
    return describe(row, evaluate(row)[1])


def test_linked_pr_summary() -> None:
    """Tables link source facts, fork snapshots, people, teams, and cross-repository issues."""
    row = item("pr", 2)
    row["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "FAILURE"
    row["reviewDecision"] = "CHANGES_REQUESTED"
    issue = item(repo=repository("other/repository", "R_2"))
    row["closingIssuesReferences"]["nodes"] = [
        {
            "id": issue["id"],
            "number": issue["number"],
            "url": issue["url"],
            "repository": issue["repository"],
        }
    ]
    row["reviewRequests"]["nodes"].append(
        {
            "requestedReviewer": {
                "__typename": "Team",
                "slug": "platform",
                "organization": {"login": "example"},
                "url": "https://github.com/orgs/example/teams/platform",
            }
        }
    )
    row["reviewRequests"]["totalCount"] = 2
    row["github"]["pull_request"] = node_metadata(row)["pull_request"]
    row["body"] = "# Original body\n\n| A | B |\n| --- | --- |\n| x | y |\n"
    result = render(row)
    assert result.startswith("**Needs attention: CI failing · Changes requested**")
    assert "[example/orbit#2](https://github.com/example/orbit/pull/2)" in result
    assert "[synthetic-author](https://github.com/synthetic-author)" in result
    assert "[synthetic-assignee](https://github.com/synthetic-assignee)" in result
    assert "[Synthetic milestone](https://github.com/example/orbit/milestone/1)" in result
    assert "[example/platform](https://github.com/orgs/example/teams/platform)" in result
    assert "[other/repository#1](https://github.com/other/repository/issues/1)" in result
    assert f"[fix/synthetic](https://github.com/contributor/orbit/tree/{'a' * 40})" in result
    assert f"[main](https://github.com/example/orbit/tree/{'b' * 40})" in result
    assert "[FAILURE](https://github.com/example/orbit/pull/2/checks)" in result
    assert "[CHANGES\\_REQUESTED](https://github.com/example/orbit/pull/2/files)" in result
    assert "| Changes | +12 / −3 · 2 files · 2 commits |" in result
    assert result.endswith("\n\n---\n\n" + row["body"])


def test_unknown_fork_does_not_link_to_base_repository() -> None:
    """Missing/deleted forks retain readable branch names without inventing destinations."""
    row = item("pr")
    row["github"]["pull_request"]["head_repository"] = None
    assert "| Branch | fix/synthetic → [main]" in render(row)
    row["github"]["pull_request"]["cross_repository"] = False
    assert "[fix/synthetic](https://github.com/example/orbit/tree/" in render(row)


def test_unknown_and_empty_values_and_reviewer_coverage() -> None:
    """Missing facts and incomplete reviewer collections cannot appear as confirmed empty."""
    row = item("pr")
    row["github"].update(author=None, assignees=[], milestone=None, comment_count=None)
    row["github"]["pull_request"].update(requested_reviewers=[], requested_reviewers_complete=False)
    result = render(row)
    assert "| Author | Unknown |" in result
    assert "| GitHub assignees | None |" in result and "| Milestone | None |" in result
    assert "| Reviewers requested | Unknown (coverage incomplete) |" in result
    assert "| Closes | None |" in result
    row["github"]["pull_request"]["requested_reviewers_complete"] = True
    assert "| Reviewers requested | None |" in render(row)


def test_bot_profiles_use_source_app_urls() -> None:
    """Bot account links use GitHub's actual app destination rather than a guessed profile."""
    row = item()
    row["github"]["author"] = {"login": "dependabot[bot]", "type": "Bot"}
    row["github"]["links"]["author"] = "https://github.com/apps/dependabot"
    assert "[dependabot\\[bot\\]](https://github.com/apps/dependabot)" in render(row)
    assert "author:bot" in evaluate(row)[1]


def test_source_markdown_is_literal_in_table_but_body_is_preserved() -> None:
    """Pipes, newlines, HTML, and link syntax cannot break the generated table layout."""
    row = item("pr")
    row["github"]["milestone"]["title"] = "Release | [trap](javascript:alert(1))\n<img src=x>"
    row["body"] = "<details>\nOriginal body | untouched\n</details>\n"
    result = render(row)
    header, body = result.split("\n\n---\n\n", 1)
    assert "Release \\| \\[trap\\]\\(javascript:alert\\(1\\)\\) &lt;img src=x&gt;" in header
    assert "<img" not in header and "\\n" not in header
    assert body == row["body"]


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "data:text/html,test", "https://[bad", "//example.com"]
)
def test_unsafe_or_malformed_link_destinations_are_plain_text(url: str) -> None:
    """Generated summaries permit only absolute web links."""
    assert link("source", url) == "source"


def test_link_destinations_escape_markdown_delimiters() -> None:
    """An otherwise valid URL cannot escape its Markdown link destination."""
    assert link("source", "https://github.com/a/b?q=one (two)|three") == (
        "[source](https://github.com/a/b?q=one%20%28two%29%7Cthree)"
    )


@pytest.mark.parametrize(
    ("lines", "label"), [(0, "s"), (99, "s"), (100, "m"), (499, "m"), (500, "l")]
)
def test_size_label_boundaries(lines: int, label: str) -> None:
    """Diff buckets use added plus deleted lines, with explicit documented boundaries."""
    row = item("pr")
    row.update(additions=lines, deletions=0)
    assert f"size:{label}" in evaluate(row)[1]


def test_derived_labels_refresh_and_resolution_reopens() -> None:
    """Source filters are removed/replaced while local categories survive a full lifecycle."""
    row = item("pr")
    row["github"]["author"]["type"] = "Bot"
    row["closingIssuesReferences"]["nodes"] = [{"id": "outside-selected-repositories"}]
    source, beads = FixtureGitHub([row]), MemoryBeads()
    reconcile(source, beads, "user:@me")
    local = beads.rows["hub-1"]
    local["labels"].append("category:keep")
    assert {"author:bot", "size:s", "links:closing-issue"} <= set(local["labels"])
    source.rows = []
    merged = copy.deepcopy(row)
    merged["state"] = "MERGED"
    source.states[row["id"]] = merged
    assert reconcile(source, beads, "user:@me")["closed"] == 1
    assert "resolution:merged" in local["labels"]
    assert local["description"].startswith("**Merged upstream**")
    assert not any(label.startswith(("ci:", "review:")) for label in local["labels"])
    row.update(additions=600, deletions=0)
    row["github"]["author"]["type"] = "User"
    row["closingIssuesReferences"]["nodes"] = []
    source.rows = [row]
    assert reconcile(source, beads, "user:@me")["status"] == "complete"
    assert "size:l" in local["labels"] and "category:keep" in local["labels"]
    assert not {"resolution:merged", "author:bot", "size:s", "links:closing-issue"} & set(
        local["labels"]
    )
    assert reconcile(source, beads, "user:@me")["writes"] == 0


def test_not_planned_resolution_and_source_url_metadata() -> None:
    """Upstream closure reasons and canonical URLs are retained independently of local fields."""
    row = item()
    row["state"] = "CLOSED"
    row["github"]["state_reason"] = "not_planned"
    assert "resolution:not-planned" in evaluate(row)[1]
    metadata = source_metadata(
        {
            "html_url": row["url"],
            "user": {"login": "app[bot]", "type": "Bot", "html_url": "https://github.com/apps/app"},
            "assignees": [{"login": "person", "html_url": "https://github.com/person"}],
            "milestone": {"number": 1, "html_url": "https://github.com/example/orbit/milestone/1"},
        },
        repository(),
    )
    assert metadata["links"] == {
        "author": "https://github.com/apps/app",
        "assignees": {"person": "https://github.com/person"},
        "milestone": "https://github.com/example/orbit/milestone/1",
    }
