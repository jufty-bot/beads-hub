# Design decisions

## Start a separate Beads implementation

The standalone [beads-hub repository](https://github.com/jufty-bot/beads-hub) contains the Beads-backed shared backlog and its clean implementation history. The earlier SQLite application and its history remain in [gh-maintainer](https://github.com/jufty-bot/gh-maintainer). No earlier application files or migration contracts carry into this design. This lets the new workflow be evaluated independently without claiming compatibility with earlier state.

## Use upstream interfaces and pin the tested releases

The image pins Beads/beads-mcp 1.3.1, Dolt 2.5.1, beads-ui 0.12.6, and uv 0.12.1. Python's transitive dependencies live in `uv.lock`. The Beads archive is verified against its published checksums. The image uses tagged upstream releases instead of executing moving install scripts. Node's Debian base and OS packages, plus UI transitive packages, are not fully digest/lock pinned; complete bit-for-bit image reproducibility is outside this initial implementation.

Source contracts: [Beads initialization](https://github.com/gastownhall/beads/blob/v1.3.1/cmd/bd/init.go), [official MCP](https://github.com/gastownhall/beads/tree/v1.3.1/integrations/beads-mcp), [UI package](https://www.npmjs.com/package/beads-ui/v/0.12.6). The UI launcher daemonizes, so its pinned `server/index.js` runs directly in the container. This internal entrypoint must be revalidated when updating the UI version.

## Enumerate repositories instead of global item search

The original draft used GraphQL search. GitHub search has a result cap and is not a safe global open-item inventory. This version accepts user/org/repo inventory scopes and paginates repository issue listings, which include PRs, then fetches PR signals via GraphQL. Full open inventories bootstrap and repair coverage; all-state incremental reads handle ordinary updates without adaptive search partitioning.

## Keep GitHub and local workflow ownership explicit

Source labels are namespaced and escaped. The hub updates its own label namespaces and metadata key while leaving local classification and triage intact. It uses source node identity for rename continuity. Upstream closure requires an explicit state read; a scope change, permission failure, or missing record does not imply closure. Claims and status overrides survive source refresh. See the [architecture contract](architecture.md) for field ownership and readiness policy.

## Use GitHubKit for Python source access

[GitHubKit](https://yanyongyu.github.io/githubkit/) supplies synchronous REST/GraphQL requests, token authentication, session handling, and REST Link pagination. It replaces subprocess calls to `gh api`; the image no longer requires GitHub CLI. We retain `bd` as the supported Beads interface. The SDK stays within the tested 0.16 minor series and its exact release is locked. Raw JSON is validated at our source boundary, keeping reconciliation independent of generated SDK model classes.

The client disables persistent HTTP caching and automatic SDK retries. Failed reads preserve the last good snapshot, and API cooldowns defer subsequent requests without blocking the process through a reset. Closing-reference cursors remain explicit so repeated cursors fail visibly. Selected source facts live in `metadata.hub.github`, never local assignee/priority/notes. Reviewer detail is bounded with explicit coverage rather than adding unbounded review/conversation ingestion.

## Keep a small incremental poller

The deployment uses polling with a few-minute freshness target. GitHubKit remains the source adapter; no additional connector platform or SQLite mirror is introduced. A single atomic JSON file holds repository cursors, successful coverage times, errors, and rate cooldowns. Cursors advance after both source projection and graph writes succeed so replay repairs partial database changes. Five-minute overlap and hourly full reconciliation address timestamp ties and missed/late updates. PR signals refresh independently of REST cursors; issue timestamps alone cannot establish current CI or mergeability. See [polling contracts](architecture.md#incremental-polling-and-progress).

Unchanged source fields and existing dependency edges avoid repeat mutations. Repository indexes and pass-local dependency reads reduce repeated local work. Progress for successful repositories is staged and atomically saved once per pass; failed repositories retain their old cursors. This avoids repeatedly serializing and fsyncing the entire state file while preserving safe replay if the pass crashes. Observation freshness belongs in progress state rather than forcing a Beads write on every poll. GitHub reads stay serial by default, with an opt-in bound of four threads for independent node lookups. This keeps the synchronous CLI/MCP lifecycle and ordered writes intact without adding an async scheduler or passing MCP workspace context into worker threads. Concurrency and cadence should be tuned only after measuring a credentialed portfolio.

## Use authoritative closing relationships

GitHub's paginated `closingIssuesReferences` supplies the intended closure relationship. The hub does not parse arbitrary body mentions or code examples into blockers. Only indexed targets can receive edges. An issue depends on its PR, and Beads computes readiness without changing the issue's stored status to blocked. Managed dependency provenance permits stale-edge removal while preserving manual edges.

## Separate process lifetimes and keep exposure private

Docker Compose runs four foreground services with one named volume and one SQL server. UI, MCP, and sync share Dolt's network namespace so internal clients retain loopback access to its localhost-only root account. Health checks order SQL startup, initialization, and dependent clients. Compose provides restart policies, shutdown ordering, persistent storage, and localhost port publishing without a custom supervisor. One filesystem lock serializes sync entrypoints. Initialization creates a separate database-scoped account whose password is injected at runtime, preserving the local root boundary. [Dolt's access documentation](https://www.dolthub.com/docs/sql-reference/server/access-management/) describes these SQL user/grant semantics.

Compose is the repository's deployment contract. It replaces the initial Kubernetes manifests and cluster-dependent tasks with a stack that can be built and verified on one Docker host. The smoke workflow uses the checked-in Compose file under an isolated project name, supplies synthetic credentials, and exercises down/up persistence before removing its test volume.
