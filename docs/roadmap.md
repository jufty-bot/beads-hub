# Roadmap

The first milestone is an independently runnable Beads backlog that treats GitHub issues and PRs as peers. The [checklist](checklist.md) owns validation evidence; [architecture](architecture.md) owns public data and ownership contracts.

| Capability | Current status | Boundary |
| --- | --- | --- |
| Standalone Beads repository | Implemented in [jufty-bot/beads-hub](https://github.com/jufty-bot/beads-hub) | Independent implementation history; no SQLite migration |
| Multi-repository issue/PR sync | Implemented with GitHubKit, overlapping incremental cursors, node identities, and hourly full repairs | Bootstrap covers open work; repairs retain all-state deltas to cover changed closed records; credential visibility applies |
| Repository epics and selective sync | Implemented with stable repository epics, imported child parenting, repeatable CLI `--repo`, and MCP `repositories` | Persistent selection uses `GH_SYNC_QUERY`; existing/closed imports migrate on a successful repository pass; unselected snapshots and checkpoints are retained |
| Continuous lifecycle convergence | Implemented with independent PR signal refresh, replay after partial failures, and explicit closure verification | Serial writes; optional 2–4 read workers; repository indexes, pass-local graph reads, and one progress commit per pass |
| GitHub progress visibility | Implemented in atomic shared-volume JSON state and `task sync-status` | Last successful coverage/errors per repository and durable rate cooldowns; database health alone is insufficient |
| Automatic freshness alerts | Pending | Alert on stale/failed source coverage independently of database health |
| Informative GitHub metadata | Implemented in `metadata.hub.github` and description summaries | Selected source facts; bounded reviewer details, no conversations/diffs |
| Source and local triage separation | Implemented | Classification is supplied by humans/agents |
| PR signal triage and closing dependencies | Implemented | Latest signals, indexed closing targets, GitHub's recognized references |
| Shared CLI, UI, official MCP | Implemented | Trusted clients; one database |
| Local embedded Dolt and stdio MCP | Implemented as the local default with repository-local storage and no SQL listener | Current-directory or explicit workspace; native ARM64 initialization, concurrent poller/MCP access, shared locking, and persistence verified with `task smoke-local` |
| Python source package and uv workflows | Implemented in `src/beads_hub` with the uv build backend and installed sync/MCP/runtime commands | Locked editable development install, non-editable Docker install, source distribution and wheel via `task package` |
| Unified image and Docker Compose deployment | Implemented | Four services, one persistent volume, localhost ports; operator supplies runtime credentials |
| Runtime and regression validation | 112 offline tests, installed CLI and stdio default checks, source/wheel builds, operation-count and repair-coverage regressions, native ARM64 macOS and AMD64 Linux embedded Dolt validation, and prior Compose smoke coverage | Updated epic assertions await a Compose rerun; credentialed pilot, AMD64 image build, and full browser interactions remain unverified; see checklist |
| Live multi-repository pilot | Pending | Needs an authorized GitHub credential and Docker host |
| Backup/restore exercise | Pending | Requires storage/backup selection and a disposable restore target |

Before adopting this as a daily backlog, run a credentialed pilot across issue-only, PR-only, private, and organization repositories. Confirm visibility, merge policy signals, graph cleanup, and operational freshness. Record request volume, `writes`, pass duration, and throttling before deciding whether GraphQL batching, conditional HTTP caching, different worker counts, or per-repository cadence are needed.

HA, webhooks, automatic GitHub writes, model-driven categorization, full conversation/code ingestion, built-in authentication, and migration from the earlier application are outside this milestone. Add them only when a workflow demonstrates the need and defines completion criteria.
