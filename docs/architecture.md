# Architecture and contracts

`scripts/gh_global_sync.py` contains a read-only `GitHub` adapter, a `Beads` CLI adapter, and reconciliation policy. Runtime coordination lives in `hub_runtime.py` and the entrypoints. The official beads-mcp instance owns all standard tool schemas, resource registration, and workspace context handling. The hub wrapper adds one synchronous trigger; it does not reimplement official tools.

## Storage and identity

Compose's named `data` volume mounts at `/var/lib/beads` in every container. `.beads/metadata.json` points to the explicit `beads_hub` SQL database. Dolt's actual data directory is `/var/lib/beads/dolt`, outside `.beads`: precreating `.beads/dolt` before `bd init` is interpreted as a legacy workspace by Beads 1.3.1. The UI container initializes once after the external SQL listener is available. MCP waits for a successful `bd info`. All clients disable auto-start and use the one external server. `docker compose down` retains the volume for the next startup.

External references use `gh:OWNER/REPO#issue:NUMBER` and `gh:OWNER/REPO#pr:NUMBER`. Beads IDs remain independent. Structured `metadata.hub` records GitHub node ID, repository node ID, source kind/number/state, last sync-owned status, and managed blocking-issue IDs. Stable node IDs prevent duplicates on repository renames; the external reference and repository label are refreshed. Duplicate existing identities fail the pass instead of choosing a row silently. All Beads lookups use `--all --limit 0 --flat --json`.

## Ownership

| Field | Owner / update behavior |
| --- | --- |
| Title, description, external reference | GitHub snapshot, refreshed by sync |
| `repo:`, `type:`, `github:`, `ci:`, `conflict:`, `review:`, `draft:` labels | Sync adds/removes individual labels |
| Other labels, priority, notes, comments, assignee, local issue type | Local workflow, retained |
| `metadata.hub` | Reconciler; `--metadata` merges it without replacing unrelated metadata |
| Status | Automatic for unclaimed items still matching the last sync-owned status; claims and manual overrides are retained |
| Closure | Explicit GitHub CLOSED/MERGED always wins; absent search/list results never imply closure |
| Blocking dependencies | Only edges created by the hub are removed by the hub |

`hub:manual-status` opts out of automatic open/in-progress selection. A changed status is also recognized by comparing it to `synced_status`. Upstream reopening restores an item previously closed by sync; a retained claim resumes in progress. Local close while GitHub is open remains local. Source-only edits belong in GitHub, since sync refreshes them.

## PR readiness

All imports are Beads tasks distinguished by `type:issue` or `type:pr`. PRs with failing/error checks, unknown checks, conflicts, unknown mergeability, requested changes, or missing approval on a non-draft PR are `open`. Signal labels explain the reason. Other PRs are `in_progress`; pending checks are labeled `ci:pending` and draft PRs `draft:true`. Missing checks mean unknown, not passing. This deterministic policy is a triage aid, not proof that repository merge policy is satisfied.

For an indexed closing issue, `bd dep add ISSUE_ID PR_ID` means the issue depends on the PR. Beads computes the blocked queue; the issue's stored status stays open. Reconciliation exhausts the GraphQL closing-reference connection, supports cross-repository node IDs, prunes removed managed links, and preserves existing manual dependencies. Metadata records add intent before the command so an interrupted add can be retried. Each dependency command failure is reported; cycle errors are not suppressed.

## Failure and concurrency

Repository inventories and REST open-item collections are fully paginated without using search. PR details include latest commit rollup, mergeability, review decision, draft flag, and closing references. Each repository's source reads, including verification of missing previously imported items, finish before its writes begin. GraphQL errors, inaccessible nodes, malformed records, and failed pages prevent that repository from being refreshed. Other repositories may succeed. Scopes are resolved before writes and do not authorize closing records outside the current inventory.

The shared `.github-sync.lock` uses a nonblocking advisory `flock`. Periodic, shell-triggered, and synchronous MCP sync calls use it; concurrent triggers return `busy`. There are no background tasks holding MCP ContextVars and no process-wide working-directory changes. Each subprocess uses an explicit workspace. Human/agent Beads commands still run concurrently through Dolt; the sync lock serializes reconcilers, not every local user. Multi-command operations may partially apply on process/database failure and are retried on later polls.

Status and its hub metadata are applied in one guarded `bd update`; status/assignee preconditions prevent a stale unclaimed snapshot from overwriting a new claim. Verified upstream closure uses a forced status update so a local blocker cannot prevent source-state convergence. Source closure and the hub state are written together. Source node identity is included at creation so a later interrupted update can be retried by identity.

`compose.yaml` defines four services: `dolt`, `hub` (initialization and UI), `beads-mcp`, and `github-reconciler`. UI, MCP, and sync use `network_mode: service:dolt`, sharing the SQL server's loopback interface while keeping separate process lifetimes. This retains Dolt's localhost-only root boundary. The Dolt service owns published UI/MCP/SQL ports, all bound to the host's `127.0.0.1`. Initialization provisions the password-protected `beads` account with grants only on `beads_hub` for clients outside that network namespace. `BEADS_REMOTE_PASSWORD` is passed only to initialization, sent to the SQL client over stdin, and removed before launching the UI. `GH_TOKEN` is passed only to MCP and sync.

Dolt and the UI run in the foreground; tini forwards signals and reaps children. Compose health checks gate initialization on SQL readiness and MCP/sync on UI readiness. The reconciler's health check verifies database access, not GitHub freshness; inspect sync summaries for source errors. `restart: unless-stopped` restarts exited processes, while an unhealthy health check by itself does not restart a running service. Keep one instance of each service and use the local Docker volume driver for shared advisory locks. Bring the complete stack up together when replacing Dolt, since other services reference its network namespace.
