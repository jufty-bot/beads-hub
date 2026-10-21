# Usage

## Development

Python 3.11+, uv, Task, and Docker with the Compose plugin are required for the full validation workflow. `task install` runs `uv sync --locked`. `task fix`, `task lint`, `task check`, and `task test` cover formatting, lint, shell syntax, types, and token-free behavior tests. `task run -- <command>` runs inside the locked environment.

`task smoke` builds `beads-hub:test`, starts the checked-in Compose stack under a disposable project name with its own volume, and removes them on exit. It uses synthetic GitHub data with real Beads, Dolt, UI, and MCP processes. It verifies readiness, dependencies, claims, local fields, closure, reopening, renames, HTTP access, a separate remote SQL pointer, and restart persistence. It requires no GitHub token and publishes temporary localhost ports instead of the normal fixed ports. Its cleanup removes only the test project and volume. Do not run `tests/smoke_runtime.py` against a real hub: it intentionally writes its synthetic backlog.

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

The hub polls full open-item inventories, then reads PR signals and all GitHub closing references. It starts immediately after startup readiness and repeats automatically. A new pass starts 120 seconds after the preceding pass ends; set `GH_SYNC_INTERVAL` to another positive number of seconds before `task deploy` to change the pause. This is polling, not a fixed freshness SLA: source/API availability and the duration of the pass also affect lag. Manual and MCP triggers share the same lock.

| GitHub change | Next successful poll |
| --- | --- |
| New or reopened open issue/PR | Import or refresh the same stable bead |
| Edited title, labels, assignments, milestone | Refresh source-owned text and metadata |
| New commits, CI results, or review decisions on an open PR | Refresh PR evidence, signal labels, and automatic readiness |
| Imported issue closed or PR closed/merged | Verify state directly, then close the bead and save closure evidence |
| Closed/referenced PR no longer blocks an issue | Closed blockers cease blocking through Beads; removed references prune hub-owned edges |
| Temporary API failure or downtime | Retain the previous snapshot; retry on later polls or after restart |

An item created and closed entirely between polls is not discovered by the open-item inventory. The hub tracks the current open backlog and terminal transitions of imported items; it does not ingest a complete event history. Local Beads closes do not prevent later upstream closure verification. A missing or inaccessible source is never proof of closure.

## Deploy with Docker Compose

Docker Engine and the Docker Compose plugin run the four services in `compose.yaml`: Dolt, UI/initialization, official MCP, and the reconciler. The named `data` volume stores the database and workspace. Keep one instance of each service.

Inject these variables through your shell or password manager before starting:

| Variable | Use |
| --- | --- |
| `BEADS_REMOTE_PASSWORD` | Required, nonempty password for the database-scoped remote SQL user `beads`; passed only to hub initialization |
| `GH_TOKEN` | Read credential for selected repositories; passed only to MCP and the reconciler. Optional for offline UI/MCP use; sync reports an authentication failure when absent |
| `GH_SYNC_QUERY` | Inventory scopes, defaults to `user:@me` |
| `GH_SYNC_INTERVAL` | Positive pause in seconds between automatic passes, defaults to `120` |

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
docker compose logs --tail=20 github-reconciler
docker compose logs --tail=20 beads-mcp
docker compose ps
```

One-shot sync emits JSON and exits nonzero on `partial`; a periodic worker logs the same error result and retries after its interval. A `busy` result means the running pass holds the shared lock. Discovery failures stop the pass before writes. Repository failures allow other repositories to progress and preserve the failed repository's previous snapshots when its source reads fail. Database write failures may leave a partially applied repository; subsequent passes repair it. There is no transactional snapshot of GitHub or of the entire portfolio.

Check reconciler logs for successful passes and investigate repeated `partial` results. A healthy database/container alone does not prove GitHub freshness: the current health check verifies database access. Each source snapshot's `metadata.hub.github.observed_at` records its observation time. No automated freshness alert is implemented. If near-immediate updates are needed, GitHub webhooks can trigger targeted refreshes, with periodic reconciliation retained to recover missed deliveries; that receiver is not implemented here.

Use local labels for classification and standard Beads priority for ranking:

```sh
bd list --label type:issue --json
bd ready --label 'type:pr,review:needed' --json
bd update <id> --add-label category:dependency-upgrade --priority 3
bd update <id> --claim
bd blocked --json
```

GitHub labels appear as `github:<percent-encoded-name>` to avoid collisions with local categories and comma-based CLI filters. For example, `bug` becomes `github:bug` and `help wanted` becomes `github:help%20wanted`. The reconciler owns source title/description; put durable reasoning in Beads notes and comments. Beads priority and issue type are local choices (imports start as `task`, priority 2).

Every bead also carries source facts under `metadata.hub.github`: author, upstream assignees, milestone, dates, comment count, repository details, and PR branch SHAs, size, reviewer requests, merge/review/CI state. Inspect these with `bd show <id> --json`; a short summary also appears in the description. GitHub assignees do not claim the bead. Repository grouping remains the `repo:OWNER/REPO` label, with stable repository identity in `metadata.hub.repo_id`.

These are current observations, with an `observed_at` timestamp. Reviewer details are bounded to 100 with explicit total/coverage fields; comment bodies, diffs, and history are not imported. See [source metadata](architecture.md#source-metadata) for fields and freshness limits. API rate limits and network failures report a partial pass and retry on the next interval; the SDK does not sleep through a rate-limit reset.

Only authoritative `closingIssuesReferences` create blockers. GitHub resolves supported closing keywords, including recognized cross-repository references; arbitrary mentions and text in non-closing contexts do not create dependencies. Linked issues must already be indexed by the selected scopes. Removing a reference removes only edges managed by the hub. Existing manual edges are retained. Closing a PR releases its blocking effect through Beads' normal readiness rules.

For backups, preserve the Compose data volume and use [Dolt backups](https://docs.dolthub.com/sql-reference/version-control/backups) appropriate to your storage. A consistent stopped-volume snapshot is also possible. Restore the workspace metadata and database together; test restoration before relying on a backup. Do not delete or reinitialize the data volume to work around startup errors. Initialization errors fail the container without a fallback destructive init.

## Current limits

This branch stores current source snapshots in Beads, not complete GitHub conversations, diffs, or source event history. Categorization and triage are human/agent actions through Beads; there is no model invocation or automatic taxonomy. Closed history is discovered only for items imported while open. No GitHub write-back, webhooks, incremental watermarks, rate-budget scheduler, HA, built-in authentication, or migration from gh-maintainer is implemented. API changes and moving paginated collections converge on later polls; a pass is not a point-in-time source snapshot. A failed or unavailable source never proves deletion.

The pinned UI watches local workspace files rather than polling SQL for every external mutation. Refresh the browser after remote CLI changes if they have not appeared. Its live-refresh behavior across remote clients is an upstream limitation; the smoke test verifies actual issue/PR delivery over its WebSocket API, not a full browser interaction suite.
