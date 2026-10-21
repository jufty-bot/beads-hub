# Roadmap

The first milestone is an independently runnable Beads backlog that treats GitHub issues and PRs as peers. The [checklist](checklist.md) owns validation evidence; [architecture](architecture.md) owns public data and ownership contracts.

| Capability | Current status | Boundary |
| --- | --- | --- |
| Standalone Beads repository | Implemented in [jufty-bot/beads-hub](https://github.com/jufty-bot/beads-hub) | Independent implementation history; no SQLite migration |
| Multi-repository issue/PR sync | Implemented with GitHubKit, overlapping incremental cursors, node identities, and hourly full repairs | Bootstrap covers open work; deltas include changed closed records; credential visibility applies |
| Continuous lifecycle convergence | Implemented with independent PR signal refresh, replay after partial failures, and explicit closure verification | Serial writes; optional 2–4 read workers; repository indexes, pass-local graph reads, and one progress commit per pass |
| GitHub progress visibility | Implemented in atomic shared-volume JSON state and `task sync-status` | Last successful coverage/errors per repository and durable rate cooldowns; database health alone is insufficient |
| Automatic freshness alerts | Pending | Alert on stale/failed source coverage independently of database health |
| Informative GitHub metadata | Implemented in `metadata.hub.github` and description summaries | Selected source facts; bounded reviewer details, no conversations/diffs |
| Source and local triage separation | Implemented | Classification is supplied by humans/agents |
| PR signal triage and closing dependencies | Implemented | Latest signals, indexed closing targets, GitHub's recognized references |
| Shared CLI, UI, official MCP | Implemented | Trusted clients; one database |
| Unified image and Docker Compose deployment | Implemented | Four services, one persistent volume, localhost ports; operator supplies runtime credentials |
| Runtime and regression validation | 76 offline tests, operation-count regressions, Compose validation, and ARM64 Compose smoke pass, including incremental state, zero-write repeated sync, UI/MCP, remote SQL, and restart persistence | Credentialed pilot, AMD64, and full browser interactions remain unverified; see checklist |
| Live multi-repository pilot | Pending | Needs an authorized GitHub credential and Docker host |
| Backup/restore exercise | Pending | Requires storage/backup selection and a disposable restore target |

Before adopting this as a daily backlog, run a credentialed pilot across issue-only, PR-only, private, and organization repositories. Confirm visibility, merge policy signals, graph cleanup, and operational freshness. Record request volume, `writes`, pass duration, and throttling before deciding whether GraphQL batching, conditional HTTP caching, different worker counts, or per-repository cadence are needed.

HA, webhooks, automatic GitHub writes, model-driven categorization, full conversation/code ingestion, built-in authentication, and migration from the earlier application are outside this milestone. Add them only when a workflow demonstrates the need and defines completion criteria.
