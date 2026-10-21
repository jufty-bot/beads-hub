# Design decisions

## Start a separate Beads implementation

The `beads-hub` orphan branch replaces the SQLite application with a Beads-backed shared backlog. No earlier application files or migration contracts carry into this design. Existing history remains on the original branch. This lets the new workflow be evaluated independently without claiming compatibility with earlier state.

## Use upstream interfaces and pin the tested releases

The image pins Beads/beads-mcp 1.3.1, Dolt 2.5.1, beads-ui 0.12.6, and uv 0.12.1. Python's transitive dependencies live in `uv.lock`. The Beads archive is verified against its published checksums. The image uses tagged upstream releases instead of executing moving install scripts. Node's Debian base and OS packages, plus UI transitive packages, are not fully digest/lock pinned; complete bit-for-bit image reproducibility is outside this initial implementation.

Source contracts: [Beads initialization](https://github.com/gastownhall/beads/blob/v1.3.1/cmd/bd/init.go), [official MCP](https://github.com/gastownhall/beads/tree/v1.3.1/integrations/beads-mcp), [UI package](https://www.npmjs.com/package/beads-ui/v/0.12.6). The UI launcher daemonizes, so its pinned `server/index.js` runs directly in the container. This internal entrypoint must be revalidated when updating the UI version.

## Enumerate repositories instead of global item search

The original draft used GraphQL search. GitHub search has a result cap and is not a safe global open-item inventory. This version accepts user/org/repo inventory scopes and paginates repository issue listings, which include PRs, then fetches PR signals via GraphQL. This trades request count for transparent coverage without adaptive search partitioning. Full polling is intentionally the initial baseline; incremental scheduling can follow measured demand.

## Keep GitHub and local workflow ownership explicit

Source labels are namespaced and escaped. The hub updates its own label namespaces and metadata key while leaving local classification and triage intact. It uses source node identity for rename continuity. Upstream closure requires an explicit state read; a scope change, permission failure, or missing record does not imply closure. Claims and status overrides survive source refresh. See the [architecture contract](architecture.md) for field ownership and readiness policy.

## Use authoritative closing relationships

GitHub's paginated `closingIssuesReferences` supplies the intended closure relationship. The hub does not parse arbitrary body mentions or code examples into blockers. Only indexed targets can receive edges. An issue depends on its PR, and Beads computes readiness without changing the issue's stored status to blocked. Managed dependency provenance permits stale-edge removal while preserving manual edges.

## Separate process lifetimes and keep exposure private

Docker Compose runs four foreground services with one named volume and one SQL server. UI, MCP, and sync share Dolt's network namespace so internal clients retain loopback access to its localhost-only root account. Health checks order SQL startup, initialization, and dependent clients. Compose provides restart policies, shutdown ordering, persistent storage, and localhost port publishing without a custom supervisor. One filesystem lock serializes sync entrypoints. Initialization creates a separate database-scoped account whose password is injected at runtime, preserving the local root boundary. [Dolt's access documentation](https://www.dolthub.com/docs/sql-reference/server/access-management/) describes these SQL user/grant semantics.

Compose is the repository's deployment contract. It replaces the initial Kubernetes manifests and cluster-dependent tasks with a stack that can be built and verified on one Docker host. The smoke workflow uses the checked-in Compose file under an isolated project name, supplies synthetic credentials, and exercises down/up persistence before removing its test volume.
