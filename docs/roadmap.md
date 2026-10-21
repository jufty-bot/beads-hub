# Roadmap

The first milestone is an independently runnable Beads backlog that treats GitHub issues and PRs as peers. The [checklist](checklist.md) owns validation evidence; [architecture](architecture.md) owns public data and ownership contracts.

| Capability | Current status | Boundary |
| --- | --- | --- |
| Standalone Beads repository | Implemented in [jufty-bot/beads-hub](https://github.com/jufty-bot/beads-hub) | Independent implementation history; no SQLite migration |
| Multi-repository issue/PR sync | Implemented with GitHubKit REST/GraphQL, repository pagination, and node identities | Full open-item polling; injected token visibility applies |
| Continuous lifecycle convergence | Implemented with configurable polling and explicit closure verification, including locally closed beads | Discovers open work and imported items' terminal states; no automated freshness alerts |
| GitHub freshness monitoring | Pending | Track last successful coverage per repository and alert on stale/failed syncs independently of database health |
| Informative GitHub metadata | Implemented in `metadata.hub.github` and description summaries | Selected source facts; bounded reviewer details, no conversations/diffs |
| Source and local triage separation | Implemented | Classification is supplied by humans/agents |
| PR signal triage and closing dependencies | Implemented | Latest signals, indexed closing targets, GitHub's recognized references |
| Shared CLI, UI, official MCP | Implemented | Trusted clients; one database |
| Unified image and Docker Compose deployment | Implemented | Four services, one persistent volume, localhost ports; operator supplies runtime credentials |
| Runtime and regression validation | 49 offline tests, Compose validation, and ARM64 Compose smoke pass, including metadata clearing, UI data, MCP HTTP, remote SQL, and down/up persistence | Credentialed pilot, AMD64, and full browser interactions remain unverified; see checklist |
| Live multi-repository pilot | Pending | Needs an authorized GitHub credential and Docker host |
| Backup/restore exercise | Pending | Requires storage/backup selection and a disposable restore target |

Before adopting this as a daily backlog, run a credentialed pilot across issue-only, PR-only, private, and organization repositories. Confirm visibility, merge policy signals, graph cleanup, and operational freshness. Record request volume and pass duration before deciding whether incremental watermarks, batching, or explicit per-repository cadence are needed.

HA, webhooks, automatic GitHub writes, model-driven categorization, full conversation/code ingestion, built-in authentication, and migration from the earlier application are outside this milestone. Add them only when a workflow demonstrates the need and defines completion criteria.
