# Usage

## Development

Python 3.11+, uv, Task, and Docker with the Compose plugin are required for the full validation workflow. `task install` runs `uv sync --locked`, installing the `src/beads_hub` package editable and its development dependencies. `task fix`, `task lint`, `task check`, and `task test` cover formatting, lint, shell syntax, types, and token-free behavior tests. `task run -- <command>` runs inside the locked environment. `task package` uses `uv build --no-sources` to build the source distribution and wheel in `dist/`; `task build` still builds the Docker image.

The package installs three commands: `beads-hub-sync` for one-shot/periodic sync and progress inspection, `beads-hub-mcp` for the official MCP stdio/HTTP/SSE server, and `beads-hub-runtime` for readiness and SQL account setup. From the checkout, run them through uv or the Task wrapper:

```sh
task run -- beads-hub-sync --help
uv run --locked beads-hub-sync --status
```

These commands default to the current repository; `BEADS_WORKING_DIR` can select another initialized workspace. Local sync/MCP requires the matching native `bd` CLI with embedded Dolt support. Docker sets an explicit workspace and SQL connection, installs the package non-editable with runtime dependencies only, then starts the same commands directly. No `PYTHONPATH` configuration is required.

`task smoke` builds `beads-hub:test`, starts the checked-in Compose stack under a disposable project name with its own volume, and removes them on exit. It uses synthetic GitHub data with real Beads, Dolt, UI, and MCP processes. It verifies readiness, dependencies, claims, local fields, closure, reopening, renames, HTTP and stdio MCP access, a separate remote SQL pointer, and restart persistence. It requires no GitHub token and publishes temporary localhost ports instead of the normal fixed ports. Its cleanup removes only the test project and volume. Do not run `tests/smoke_runtime.py` against a real hub: it intentionally writes its synthetic backlog.

## Local embedded deployment

The [local guide](local.md) covers plain `bd init`, repository-local `.beads/embeddeddolt/` storage, uv polling, and a copyable client configuration. `beads-hub-mcp` defaults to stdio. Native `bd` opens the database directly, with no SQL listener or separate Dolt executable. All local processes share one repository workspace, sync lock, and progress file. `task smoke-local` validates embedded behavior, concurrent poller/MCP access, and persistence in a disposable repository using native `bd` and synthetic source records. Compose continues to select HTTP/SSE and its SQL server explicitly. Existing server-mode workspaces retain their configured connection and are not converted by these defaults.

## Select repositories

Set `GH_SYNC_QUERY` in the environment used by Compose to semicolon-separated scopes:

```sh
export GH_SYNC_QUERY='user:@me;org:example-team;repo:another-owner/project'
```

Supported scopes:

| Scope | Coverage |
| --- | --- |
| `user:@me` | Repositories owned by the authenticated user, including accessible private ones |
| `user:NAME` | Repositories returned by GitHub's public user-repositories listing |
| `org:NAME` | Organization repositories visible to the credential |
| `repo:OWNER/REPO` | One explicitly named accessible repository |

Archived and disabled repositories are excluded. Overlapping scopes deduplicate by repository node ID. Search qualifiers such as `is:open`, `label:`, or free text are rejected. These are inventory scopes, not arbitrary GitHub search queries. Both issues and PRs are always included. Visibility is limited to the credential; inaccessible or filtered-out items are never considered closed merely because they disappear.

To sync a specific set on demand, repeat `--repo OWNER/REPO`. These names replace `GH_SYNC_QUERY` for that command, including when combined with `--full` or `--loop`:

```sh
# Local workspace
uv run --locked beads-hub-sync --repo example/api --repo example/web
# Compose: repair only this repository
task sync-now -- --repo example/api --full
```

The MCP tool accepts the same selection as `trigger_github_sync` arguments:

```json
{"repositories": ["example/api", "example/web"]}
```

Omit `repositories` to use configured scopes. Empty lists and malformed names are rejected. Unselected repositories keep their existing imports, epics, and checkpoints. A manual selection does not change the running periodic worker; set `GH_SYNC_QUERY='repo:example/api;repo:example/web'` in its environment to persist that selection.

Each selected repository gets an epic titled `OWNER/REPO`, labeled `type:repository`, with its issues and PRs as children. Existing imports, including closed items, gain the parent during the next successful pass for their repository, even with an empty delta. Repository renames preserve the epic and child identities. Empty repositories also have epics. Epic status and local planning fields remain yours; closing children or removing a scope does not automatically close the epic. Sync maintains epic type and child parenting.

```sh
bd list --type epic --json
bd list --parent <epic-id> --all --limit 0 --json
```

The hub starts with a full open-item inventory, then polls changed issues/PRs in all states using a durable per-repository checkpoint and five-minute overlap. Every pass independently refreshes known open PR signals and all closing references. An hourly full open-backlog reconciliation repairs missed changes. It starts immediately after startup readiness; a new pass starts 120 seconds after the preceding pass ends. Set `GH_SYNC_INTERVAL` to another positive number of seconds before `task deploy` to change the pause. This is polling, not a fixed freshness SLA: source/API availability and pass duration affect lag. Manual and MCP triggers share the same lock.

| GitHub change | Next successful poll |
| --- | --- |
| New or reopened open issue/PR | Import or refresh the same stable bead |
| Edited title, labels, assignments, milestone | Refresh source-owned text and metadata |
| New commits, CI results, or review decisions on an open PR | Refresh PR evidence, signal labels, and automatic readiness |
| Imported issue closed or PR closed/merged | Verify state directly, then close the bead and save closure evidence |
| Closed/referenced PR no longer blocks an issue | Closed blockers cease blocking through Beads; removed references prune hub-owned edges |
| Temporary API failure or downtime | Retain the previous snapshot; retry on later polls or after restart |

After bootstrap, all-state deltas can discover an item created and closed entirely between polls. Hourly, rename, and `--full` repairs retain that delta read alongside the full open inventory, so a repair does not skip terminal updates while advancing the checkpoint. Both reads must succeed before writes. Repairs after a long outage may therefore read a larger range of changed closed work. Old closed history predating bootstrap is not imported, and this is not a complete event history. Local Beads closes do not prevent upstream closure verification. A missing or inaccessible source is never proof of closure.

## Deploy with Docker Compose

Docker Engine and the Docker Compose plugin run the four services in `compose.yaml`: Dolt, UI/initialization, official MCP, and the reconciler. The named `data` volume stores the database and workspace. Keep one instance of each service.

Inject these variables through your shell or password manager before starting:

| Variable | Use |
| --- | --- |
| `BEADS_REMOTE_PASSWORD` | Required, nonempty password for the database-scoped remote SQL user `beads`; passed only to hub initialization |
| `GH_TOKEN` | Read credential for selected repositories; passed only to MCP and the reconciler. Optional for offline UI/MCP use; sync reports an authentication failure when absent |
| `GH_SYNC_QUERY` | Inventory scopes, defaults to `user:@me` |
| `GH_SYNC_INTERVAL` | Positive pause in seconds between automatic passes, defaults to `120` |
| `GH_SYNC_WORKERS` | Independent GitHub node-read threads: `1` by default, optionally `2`–`4`; writes stay serial |

The GitHub credential needs metadata, issues, pull-request, checks, and commit-status read access to the selected repositories, including any private-repository and organization authorization. The Python source adapter authenticates directly through GitHubKit; GitHub CLI installation and `gh auth login` are not used by sync. Keep credentials out of source files, printed Compose configuration, and command history. `task validate` checks configuration with `docker compose config --quiet`, without printing environment values. Keep the same runtime environment available for operational tasks; Compose requires the SQL password when loading its configuration.

```sh
task validate
task deploy
```

`task deploy` builds the image locally, then runs `docker compose up -d --wait --wait-timeout 180`. The direct equivalent is `docker compose up -d --build --wait --wait-timeout 180`. Startup waits for SQL, then initializes the workspace and remote account before starting MCP and sync. All services have health checks and `restart: unless-stopped`. An unhealthy running container is reported unhealthy; the restart policy applies when its process exits.

The default image is `beads-hub:test`. To choose another tag consistently across build and startup, use `task deploy IMAGE=ghcr.io/example/beads-hub:0.1.0`. A registry is optional for a local deployment; `task push IMAGE=...` publishes an image only when explicitly invoked.

| Endpoint | Default host address |
| --- | --- |
| Web UI | `http://127.0.0.1:3000` |
| Streamable HTTP MCP | `http://127.0.0.1:8080/mcp` |
| Dolt SQL | `127.0.0.1:3307` |

Compose binds all three published ports to localhost. `BEADS_UI_PORT`, `BEADS_MCP_PORT`, and `BEADS_SQL_PORT` can select different host ports while container ports remain fixed. UI, MCP, and sync share Dolt's network namespace, so their port mappings belong to the `dolt` service. A separate container on the Compose network can reach SQL as `dolt:3307`. Initialize a matching `bd` CLI with the [pointer bootstrap](../AGENTS.md).

Dolt's root account stays local to the shared container network namespace; external clients use the password-protected `beads` account restricted to `beads_hub`. To rotate its password, update the injected `BEADS_REMOTE_PASSWORD` and run `task deploy` so Compose recreates initialization with the new environment. Use a private tunnel or your own authenticated proxy for access from another host. SQL has no TLS configured, and UI/MCP have no built-in authentication. For legacy SSE, set `MCP_TRANSPORT=sse` and connect to `http://127.0.0.1:8080/sse`.

```sh
task down       # Stops services and removes containers/network; retains the data volume
task deploy     # Reuses the existing database
```

The default Compose project name is `beads-hub`, with data in `beads-hub_data`. `COMPOSE_PROJECT_NAME` or `docker compose -p` selects a different stack and volume. Bring the complete stack up together when replacing the Dolt container because the other services reference its network namespace. `task down` deliberately omits `--volumes`; deleting that volume would erase the backlog.

## Operate and triage

```sh
task sync-now
task sync-now -- --full  # Force an open-backlog repair without deleting progress
task sync-status        # Repository checkpoints, last success/error, and rate cooldown
docker compose logs --tail=20 github-reconciler
docker compose logs --tail=20 beads-mcp
docker compose ps
```

One-shot sync emits JSON and exits nonzero on `partial`; a periodic worker logs the same result and retries after its interval. `modes` reports full/delta per repository; `writes` counts successful mutating Beads commands. Item counts describe processed records, not total backlog size. A `busy` result means another pass holds the lock. Discovery failures stop the pass before writes. Repository read failures preserve that repository's prior snapshots; write/graph failures may leave partial changes but do not advance its checkpoint. Successful repository results are saved together once at the end of a pass, while failed ones keep their prior checkpoints. A crash before saving safely replays that pass. There is no transactional snapshot of GitHub or the entire portfolio.

Progress lives in `/var/lib/beads/.github-sync-state.json` on the shared volume. `task sync-status` reads it without contacting GitHub. Each repository has its last committed pass start (`checkpoint`), successful completion (`last_success_at`), full scan (`last_full_at`), and last error. Times are UTC Unix seconds. Check their age and reconciler logs; inventory errors appear in pass logs, and repositories outside the current scopes retain historical progress. The global `retry_at` records API cooldowns. Removing only this progress file requests a new open-inventory bootstrap, but normally use `--full` for repairs; malformed state fails visibly and needs operator attention. Never delete the data volume to reset polling.

A healthy container/database alone does not prove GitHub freshness: the health check verifies database access. An unchanged bead's `metadata.hub.github.observed_at` intentionally stays unchanged to avoid repeat writes; use repository `last_success_at` for polling freshness. No automatic freshness alert or webhook receiver is implemented.

Keep the default one read worker initially. For high-latency portfolios, `export GH_SYNC_WORKERS=2` before `task deploy` enables bounded parallel node reads; 4 is the maximum. GitHub recommends serial API requests to avoid secondary limits, so measure pass duration and API errors before increasing it. Only GitHub reads use threads. Beads writes, graph changes, and checkpoint commits remain ordered, including when sync is triggered through MCP.

Use local labels for classification and standard Beads priority for ranking:

```sh
bd list --label type:issue --json
bd ready --label 'type:pr,review:needed' --json
bd update <id> --add-label category:dependency-upgrade --priority 3
bd update <id> --claim
bd blocked --json
```

GitHub labels appear as `github:<percent-encoded-name>` to avoid collisions with local categories and comma-based CLI filters. For example, `bug` becomes `github:bug` and `help wanted` becomes `github:help%20wanted`. The reconciler owns source title/description; put durable reasoning in Beads notes and comments. Beads priority and issue/PR type are local choices (imports start as `task`, priority 2); repository containers stay `epic`.

Every imported issue/PR also carries source facts under `metadata.hub.github`: author, upstream assignees, milestone, dates, comment count, repository details, and PR branch SHAs, size, reviewer requests, merge/review/CI state. Inspect these with `bd show <id> --json`. Descriptions show an attention summary and a linked two-column table, followed by the unchanged GitHub body. Links open the source item/repository, author and assignees, milestone, requested reviewers/teams, source branch snapshots (including forks), PR checks/review pages, and closing issues. Missing forks retain plain branch text; incomplete reviewer lists are marked. GitHub assignees do not claim the bead. Repository grouping remains the `repo:OWNER/REPO` label, with stable repository identity in `metadata.hub.repo_id`.

New source filters are sync-owned:

| Filter | Meaning |
| --- | --- |
| `size:s`, `size:m`, `size:l` | PR added+deleted lines: under 100, 100–499, or 500+; describes diff size, not effort |
| `author:bot` | GitHub identifies the source author as a bot |
| `links:closing-issue` | A PR has authoritative closing references, including targets outside selected scopes |
| `resolution:merged` | Verified upstream PR merge |
| `resolution:not-planned` | Explicit upstream not-planned closure reason |

```sh
bd ready --label 'type:pr,size:s' --json
bd list --label 'author:bot' --json
bd list --all --label 'resolution:merged' --json
```

Canonical author/assignee/milestone URLs are stored under `metadata.hub.github.links`; requested-reviewer URLs, `head_repository`, and fully paginated `closing_issues` live under `pull_request`. No extra API requests are needed for these fields. Existing descriptions and labels refresh when their source items are observed; `task sync-now -- --full` refreshes the current open inventory. Already-closed unchanged history is not reread merely to reformat it. The summary's source dates do not represent polling freshness.

These are selected source observations, with the stored snapshot's `observed_at` timestamp. Reviewer details are bounded to 100 with explicit total/coverage fields; comment bodies, diffs, and history are not imported. See [source metadata](architecture.md#source-metadata) for fields and freshness limits. API throttling/exhaustion persists a cooldown from Retry-After/reset evidence; passes skip network requests until it expires, including after restarts. The SDK does not sleep through resets. Other network failures retry on subsequent polls.

Only authoritative `closingIssuesReferences` create blockers. GitHub resolves supported closing keywords, including recognized cross-repository references; arbitrary mentions and text in non-closing contexts do not create dependencies. Linked issues must already be indexed by the selected scopes. Removing a reference removes only edges managed by the hub. Existing manual edges are retained. Closing a PR releases its blocking effect through Beads' normal readiness rules.

For backups, preserve the Compose data volume and use [Dolt backups](https://docs.dolthub.com/sql-reference/version-control/backups) appropriate to your storage. A consistent stopped-volume snapshot is also possible. Restore the workspace metadata and database together; test restoration before relying on a backup. Do not delete or reinitialize the data volume to work around startup errors. Initialization errors fail the container without a fallback destructive init.

## Current limits

This branch stores selected source snapshots in Beads, not complete GitHub conversations, diffs, or source event history. Categorization and triage are human/agent actions; there is no model invocation or automatic taxonomy. Bootstrap imports open work; subsequent deltas include changed closed records. No GitHub write-back, webhooks, adaptive scheduling, GraphQL batching, automatic freshness alerts, HA, built-in authentication, or migration from gh-maintainer is implemented. Repository inventories and Beads lookups still run every pass. PRs absent from the REST delta retain last-known REST facts while GraphQL signals refresh. API changes, long clock skew, and moving paginated collections may require the periodic full repair; a pass is not a point-in-time snapshot. A failed or unavailable source never proves deletion.

The pinned UI watches local workspace files rather than polling SQL for every external mutation. Refresh the browser after remote CLI changes if they have not appeared. Its live-refresh behavior across remote clients is an upstream limitation; the smoke test verifies actual issue/PR delivery over its WebSocket API, not a full browser interaction suite.
