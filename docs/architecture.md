# Architecture and contracts

Python lives in the installable `src/beads_hub` package. `github.py` contains the read-only GitHubKit adapter. It uses synchronous REST and GraphQL calls, with SDK-managed authentication, HTTP sessions, and REST Link pagination; it does not invoke `gh`. `sync.py` contains the `Beads` CLI adapter and reconciliation policy, `state.py` persists polling progress, and `runtime.py` handles readiness and SQL provisioning. `mcp.py` serves the official beads-mcp instance, which owns all standard tool schemas, resource registration, and workspace context handling. The hub wrapper adds one synchronous trigger; it does not reimplement official tools.

`pyproject.toml` declares the `uv_build` backend and console entrypoints `beads-hub-sync`, `beads-hub-mcp`, and `beads-hub-runtime`. `uv sync --locked` installs the package editable for development; tests import that package without adding source directories to Python's search path. The Docker image installs the package non-editable with locked runtime dependencies, and Compose and shell entrypoints invoke its installed commands. `scripts/` contains only shell startup and smoke helpers. `task package` builds a source distribution and wheel with uv.

## Local embedded deployment

The [local workflow](local.md) defaults to the current directory, where plain `bd init` configures embedded Dolt in `.beads/embeddeddolt/`. Native `bd` opens the database directly; no separate Dolt executable, SQL listener, port, or SQL account is required. One uv poller maintains GitHub coverage, and each MCP client launches `beads-hub-mcp`, which defaults to stdio. `BEADS_WORKING_DIR` can select another initialized repository. Stdio receives no HTTP bind options, logs use stderr, and the official tools/resource and synchronous sync trigger are retained.

Readiness uses `bd info` for either backend. The embedded engine coordinates database access between CLI processes; the hub lock separately serializes reconcilers sharing the repository. Poller writes and MCP reads are exercised concurrently by `task smoke-local`. Work items persist across new MCP/CLI processes without a server lifetime. Existing server-mode metadata retains its backend and data; defaults do not perform a migration. A host pointer to a Compose database remains a different filesystem workspace with its own lock/progress, so source sync for that database stays inside Compose.

## Storage and identity

Compose's named `data` volume mounts at `/var/lib/beads` in every container. `.beads/metadata.json` points to the explicit `beads_hub` SQL database. Dolt's actual data directory is `/var/lib/beads/dolt`, outside `.beads`: precreating `.beads/dolt` before `bd init` is interpreted as a legacy workspace by Beads 1.3.1. The UI container initializes once after the external SQL listener is available. MCP waits for a successful `bd info`. All clients disable auto-start and use the one external server. `docker compose down` retains the volume for the next startup.

External references use `gh:OWNER/REPO#issue:NUMBER` and `gh:OWNER/REPO#pr:NUMBER`. Beads IDs remain independent. Structured `metadata.hub` records GitHub node ID, repository node ID, source kind/number/state, last sync-owned status, and managed blocking-issue IDs. Stable node IDs prevent duplicates on repository renames; the external reference and repository label are refreshed. Duplicate existing identities fail the pass instead of choosing a row silently. All Beads lookups use `--all --limit 0 --flat --json`.

Each selected repository also has one Beads epic, including repositories with no open work. Its external reference is `gh:OWNER/REPO#repository`; `metadata.hub` stores the repository node ID as both `node_id` and `repo_id`, with `kind=repository`. The epic title is `OWNER/REPO`, and its labels include `repo:OWNER/REPO` and `type:repository`. Renames refresh the same epic. Repository epics are excluded from issue/PR source-state verification and counts. Their status, assignee, priority, notes, comments, and local labels remain local; sync maintains their epic type and source identity/text. They are not automatically closed when children close or a repository leaves the configured scopes.

Sync owns each imported issue/PR's parent and repairs it with `bd update --parent EPIC_ID`, including existing closed imports and items absent from a delta. Other dependency types remain independent of this hierarchy. Repository reads and validation finish before epic or parent writes; hierarchy failures prevent that repository's checkpoint from advancing. Unchanged epic and parent state produces no writes.

### Source metadata

`metadata.hub.github` holds selected source facts, distinct from local Beads fields:

| Fields | Content |
| --- | --- |
| `url`, `author`, `author_association`, `assignees` | Upstream link, author login/type and relationship, GitHub assignee logins |
| `milestone` | Number, title, state, due date, or null |
| `created_at`, `updated_at`, `closed_at`, `state_reason` | Source dates and closure reason |
| `comment_count`, `locked` | Conversation size and lock flag; no comment bodies |
| `repository` | Visibility, default branch, primary language, stars, fork flag |
| `observed_at` | UTC time of the source observation; GitHub's update date alone does not establish CI freshness |
| `pull_request` | Draft, mergeability and merge state, review decision, latest CI state, head/base branch names and SHAs, additions/deletions/files/commits, cross-repository flag, merge date, requested reviewers |

Requested reviewers include the first 100 entries, with `requested_reviewers_total` and `requested_reviewers_complete` making coverage explicit. User/bot logins and team organization/slug are selected; other reviewer types retain their type only. Closing-issue references are fully paginated independently of this bounded display metadata. Source profiles, emails, conversations, patches, and complete payloads are not imported.

The description shows a compact source summary before the GitHub body. A source read refreshes the snapshot, including cleared assignments, milestone, and reviewers, only when facts or source-owned Beads fields differ. `observed_at` stays unchanged when the facts are unchanged; it records when the stored snapshot was observed, not the latest successful poll. Use repository `last_success_at` in polling state for coverage freshness. A direct node read refreshes closure/PR evidence and retains last-known REST facts such as author and comment count. All-state deltas can refresh later edits to closed items. Snapshot fields are observations, not a history or a guarantee that all fields were read simultaneously.

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
| Repository epic type and imported issue/PR parent | Sync maintains one epic per repository and attaches its imported work |

`hub:manual-status` opts out of automatic open/in-progress selection. A changed status is also recognized by comparing it to `synced_status`. Upstream reopening restores an item previously closed by sync; a retained claim resumes in progress. Local close while GitHub is open remains local. Source-only edits belong in GitHub, since sync refreshes them.

Closing a bead locally does not stop source-state verification. Changed closed items arrive through all-state deltas, open PRs receive a direct signal refresh every pass regardless of local status, and full repairs verify imported items absent from the open inventory. An unchanged item already confirmed closed upstream needs no direct lookup.

## PR readiness

Issue and PR imports start as Beads tasks distinguished by `type:issue` or `type:pr`. PRs with failing/error checks, unknown checks, conflicts, unknown mergeability, requested changes, or missing approval on a non-draft PR are `open`. Signal labels explain the reason. Other PRs are `in_progress`; pending checks are labeled `ci:pending` and draft PRs `draft:true`. Missing checks mean unknown, not passing. This deterministic policy is a triage aid, not proof that repository merge policy is satisfied.

For an indexed closing issue, `bd dep add ISSUE_ID PR_ID` means the issue depends on the PR. Beads computes the blocked queue; the issue's stored status stays open. Reconciliation exhausts the GraphQL closing-reference connection, supports cross-repository node IDs, prunes removed managed links, and preserves existing manual dependencies. Metadata records add intent before the command so an interrupted add can be retried. Each dependency command failure is reported; cycle errors are not suppressed.

## Incremental polling and progress

`src/beads_hub/state.py` owns `.github-sync-state.json` in the selected workspace: the local repository by default, or `/var/lib/beads` on Compose's shared volume. A missing file bootstraps with a full open inventory. Each subsequent pass queries REST issues with `state=all&since=CHECKPOINT_MINUS_300_SECONDS`, including PRs and terminal updates. The checkpoint is the local wall-clock time immediately before reading that repository, never the largest returned source date. Timestamp overlap makes repeats harmless. Malformed state fails visibly rather than resetting coverage silently; clock rollback forces a full repair.

Every pass also refreshes all known source-open PRs by node ID, independently of REST update timestamps. GraphQL supplies current body/title, latest commit rollup, review decision, mergeability, draft flag, and all closing references. If a PR is absent from the REST delta, its last-known REST metadata and source labels are retained. This avoids assuming that CI, reviews, and mergeability can be inferred from an issue update timestamp.

Once an hour, on repository rename, or with `--full`, a full open inventory repairs coverage and verifies absent imported items by explicit node state. When a checkpoint exists, repairs also read the all-state delta since that checkpoint; the open inventory cannot replace coverage of terminal updates. Both collections finish before writes, and rows are deduplicated by node ID before PR detail reads, with the later delta observation taking precedence. A failed page in either collection retains the previous checkpoint. Clock rollback rewinds the delta to five minutes before the current time while requesting a full repair.

Delta absence never triggers closure. Bootstrap does not import old closed history; subsequent deltas, including those paired with repairs, can discover work created and closed between polls. Moving collections, clock skew beyond the overlap, or delayed API visibility can still require the periodic repair. No GitHub event log or point-in-time snapshot is implied.

Each repository's reads and validation finish before its writes. Existing beads are indexed once by repository and identity, avoiding a scan of the entire backlog for each repository. A checkpoint advances only after that repository's Beads updates and PR dependency reconciliation succeed. Failed repositories retain their old checkpoints while successful ones advance. Results are staged in memory and saved together once per pass; a crash before commit safely replays the unfinished pass. A failed replacement retains the old document. State writes use a flushed/fsynced temporary file, atomic rename, and directory fsync under the existing sync lock; saving identical state does no file I/O. State contains per-repository `name`, `checkpoint`, `last_full_at`, `last_success_at`, and `error`, plus a shared `retry_at` cooldown. No source bodies or credentials belong in it.

Unchanged snapshots skip `bd update`; existing dependencies are checked before add/remove commands. A pass-local cache reads each target issue's blockers once, shares that result across PRs, and reflects successful edge additions/removals. The next pass reloads the graph, so external edits and interrupted writes remain repairable. Source-owned fields are compared against actual Beads fields, so an unchanged GitHub source can still repair an accidentally edited source title or missing managed dependency. Claims/status preconditions remain in force for status transitions. Pass summaries include `modes` (full/delta), processed item counts, errors, and successful mutating `bd` command count (`writes`); delta counts are not portfolio totals.

## Failure and concurrency

GitHubKit uses an injected `GH_TOKEN`, a 30-second HTTP timeout, no persistent HTTP cache, and no automatic SDK retries. REST Link pagination and repository inventories remain uncapped by search. GraphQL errors, inaccessible nodes, malformed records, and failed pages prevent that repository from being refreshed. Other repositories may succeed. Scopes are resolved before writes and do not authorize closing records outside the current inventory.

`GH_SYNC_QUERY` supplies persistent inventory scopes. Repeated CLI `--repo OWNER/REPO` flags or the MCP trigger's optional `repositories` list replace these scopes for that invocation, using only explicit repository lookups. Omitting selection retains configured behavior; an empty or malformed list is rejected. A selected loop keeps its selection on every pass. Unselected repositories retain stored snapshots, epics, and checkpoints; the selection does not reconfigure a separate periodic worker. Selected PRs can still reconcile closing dependencies against already indexed cross-repository issues.

Primary exhaustion and throttling honor reset/Retry-After deadlines, with a 60-second fallback when no deadline is provided. The shared cooldown stops further repository requests and is saved across restarts/manual/MCP triggers; already in-flight reads may finish. REST pagination checks each page's remaining budget before allowing a following page. GraphQL reads request `rateLimit` evidence and handle rate-limit errors even with HTTP 200. The worker keeps its normal interval and skips network access until the cooldown expires. The cooldown conservatively pauses both REST and GraphQL. Authentication failures, timeouts, and server errors remain visible partial passes. Errors report operation/type/status without copying arbitrary API error bodies. REST requests pin API version `2022-11-28`; GraphQL follows GitHub's live schema.

`GH_SYNC_WORKERS=1` keeps API requests serial by default, following [GitHub's API guidance](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api). Operators can opt into 2–4 threads for independent node reads. A fixed number of partitions bounds work; each worker owns a context-local SDK HTTP session, uses the shared cooldown, and is joined before source validation or Beads writes. Repository discovery, REST page traversal, Beads mutations, graph operations, and checkpoint commits remain serial. There is no async event loop or background MCP task, and no Beads workspace/actor context is passed into read workers.

The shared `.github-sync.lock` uses a nonblocking advisory `flock`. Periodic, shell-triggered, and synchronous MCP sync calls use it; concurrent triggers return `busy`. There are no background tasks holding MCP ContextVars and no process-wide working-directory changes. Each subprocess uses an explicit workspace. Human/agent Beads commands still run concurrently through Dolt; the sync lock serializes reconcilers, not every local user. Multi-command operations may partially apply on process/database failure and are retried on later polls.

Status and its hub metadata are applied in one guarded `bd update`; status/assignee preconditions prevent a stale unclaimed snapshot from overwriting a new claim. Verified upstream closure uses a forced status update so a local blocker cannot prevent source-state convergence. Source closure and the hub state are written together. Source node identity is included at creation so a later interrupted update can be retried by identity.

`compose.yaml` defines four services: `dolt`, `hub` (initialization and UI), `beads-mcp`, and `github-reconciler`. UI, MCP, and sync use `network_mode: service:dolt`, sharing the SQL server's loopback interface while keeping separate process lifetimes. This retains Dolt's localhost-only root boundary. The Dolt service owns published UI/MCP/SQL ports, all bound to the host's `127.0.0.1`. Initialization provisions the password-protected `beads` account with grants only on `beads_hub` for clients outside that network namespace. `BEADS_REMOTE_PASSWORD` is passed only to initialization, sent to the SQL client over stdin, and removed before launching the UI. `GH_TOKEN` is passed only to MCP and sync.

Dolt and the UI run in the foreground; tini forwards signals and reaps children. Compose health checks gate initialization on SQL readiness and MCP/sync on UI readiness. The reconciler's health check verifies database access, not GitHub freshness; inspect sync summaries and `task sync-status` for source errors and last successful coverage. `restart: unless-stopped` restarts exited processes, while an unhealthy health check by itself does not restart a running service. Keep one instance of each service and use the local Docker volume driver for shared advisory locks. Bring the complete stack up together when replacing Dolt, since other services reference its network namespace.
