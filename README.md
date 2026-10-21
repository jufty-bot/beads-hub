# beads-hub

A shared [Beads](https://github.com/gastownhall/beads) backlog for GitHub issues and pull requests across repositories. Categorize, prioritize, and claim either kind of work using the `bd` CLI, the official beads-mcp server, or [Beads UI](https://github.com/mantoni/beads-ui).

GitHub supplies source text, labels, PR signals, and closure. Beads holds local categories, priorities, assignees, notes, and workflow decisions. Reconciliation reads GitHub; local edits never write back to GitHub.

```mermaid
flowchart LR
    GitHub[GitHub repositories] --> Reconciler
    Reconciler --> CLI[bd CLI]
    MCP[Official beads-mcp :8080] --> CLI
    UI[Beads UI :3000] --> CLI
    Agents[Shell agents] --> Dolt[Dolt :3307]
    CLI --> Dolt
    Dolt --> PVC[Persistent volume]
```

Issues and PRs are peer tasks with `type:issue` / `type:pr` labels. Actionable PRs enter the open queue. GitHub closing references create dependencies so an open PR blocks its issue until the PR closes. Local labels such as `category:bug-fix` remain yours to manage.

```sh
task install
task lint
task check
task test
task smoke  # Builds the image; tests a disposable stack without GitHub credentials
```

For deployment, configure the image, GitHub scopes, and Kubernetes Secret described in [Usage](docs/usage.md), then run `task deploy` and `task port-forward`. The base deployment exposes only ClusterIP services; the optional Gateway route requires your private access controls.

- [Usage and deployment](docs/usage.md)
- [Agent connections](AGENTS.md)
- [Architecture and sync contracts](docs/architecture.md)
- [Design decisions](docs/decisions.md)
- [Roadmap](docs/roadmap.md) and [validation checklist](docs/checklist.md)

This is a fresh design on the orphan `beads-hub` branch. It does not migrate the earlier gh-maintainer SQLite application. The previous implementation remains on its original branch.
