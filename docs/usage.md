# Usage

## Development

Python 3.11+, uv, Task, and Docker are required for the full validation workflow. `task install` runs `uv sync --locked`. `task fix`, `task lint`, `task check`, and `task test` cover formatting, lint, shell syntax, types, and token-free behavior tests. `task run -- <command>` runs inside the locked environment.

`task smoke` builds `beads-hub:test`, creates disposable containers and a named volume, and removes them on exit. It uses synthetic GitHub data with real Beads, Dolt, UI, and MCP processes. It verifies readiness, dependencies, claims, local fields, closure, reopening, renames, HTTP access, a separate remote SQL pointer, and restart persistence. It requires no GitHub token and does not publish ports or images. Do not run `tests/smoke_runtime.py` against a real hub: it intentionally writes its synthetic backlog.

## Select repositories

Set the ConfigMap's `GH_SYNC_QUERY` to semicolon-separated scopes:

```yaml
GH_SYNC_QUERY: 'user:@me;org:example-team;repo:another-owner/project'
```

Supported scopes:

| Scope | Coverage |
| --- | --- |
| `user:@me` | Repositories owned by the authenticated user, including accessible private ones |
| `user:NAME` | Repositories returned by GitHub's public user-repositories listing |
| `org:NAME` | Organization repositories visible to the credential |
| `repo:OWNER/REPO` | One explicitly named accessible repository |

Archived and disabled repositories are excluded. Overlapping scopes deduplicate by repository node ID. Search qualifiers such as `is:open`, `label:`, or free text are rejected. These are inventory scopes, not arbitrary GitHub search queries. Both issues and PRs are always included. Visibility is limited to the credential; inaccessible or filtered-out items are never considered closed merely because they disappear.

The hub polls full open-item inventories, then reads PR signals and all GitHub closing references. A new pass starts 120 seconds after the preceding pass ends. This is polling, not a fixed freshness SLA. Large portfolios may need a longer interval; edit the reconciler arguments in the StatefulSet. Manual and MCP triggers share the same lock.

## Deploy

The base requires a Kubernetes cluster, a default StorageClass that supports a 10Gi ReadWriteOnce PVC, and an externally managed GitHub credential. One Pod runs four containers: Dolt, UI/initialization, official MCP, and the reconciler. Keep replicas at one. The storage driver must support advisory filesystem locks shared between the containers.

1. Choose a registry image tag and build/push it:

   ```sh
   task build IMAGE=ghcr.io/example/beads-hub:0.1.0
   task push IMAGE=ghcr.io/example/beads-hub:0.1.0
   ```

2. Set **all four** StatefulSet image references to that image (or use a Kustomize overlay's `images` replacement for `beads-hub`). `IMAGE` selects the build/push task image; it does not rewrite manifests. Configure `GH_SYNC_QUERY` in the ConfigMap. Private registries may also require `imagePullSecrets`. The checked-in `beads-hub:test` tag is for an image loaded into a local cluster.

3. Create the namespace, then provision two Secrets in namespace `beads-hub` using your secret manager. `beads-github-auth` needs key `GH_TOKEN`; the token needs metadata, issues, pull-request, checks, and commit-status read access for the selected repositories. Scope private-repository and organization authorization appropriately. Only MCP and the reconciler receive that Secret. `beads-dolt-auth` needs a nonempty `password` key for the database-scoped remote SQL user `beads`; only hub initialization receives that Secret. Do not commit credentials or put them in command history.

   ```sh
   kubectl apply -f k8s/namespace.yaml
   ```

4. Render and validate before applying:

   ```sh
   task manifests
   task validate
   task deploy
   kubectl rollout status -n beads-hub statefulset/beads-hub
   task port-forward
   ```

   `task validate` uses `kubectl apply --dry-run=client -k k8s/`; kubectl still needs a reachable API server for schema discovery. `task deploy` applies the base only. No deployment, image push, or credential provisioning is performed by the test workflow.

Open the UI at `http://127.0.0.1:3000`, connect MCP to `http://127.0.0.1:8080/mcp`, and connect a matching `bd` CLI through `127.0.0.1:3307` using the [pointer bootstrap](../AGENTS.md). ClusterIP is reachable by other cluster workloads: configure your cluster's network policy and private access controls to match its trust boundary. Dolt's root account remains local to the Pod; remote clients use the password-protected `beads` account restricted to the hub database. Rotate that password by updating its Secret and restarting the hub container. SQL traffic is not TLS-enabled by this baseline; use port forwarding or a trusted private tunnel.

The optional `k8s/httproute.yaml` routes `/mcp` to port 8080 and `/` to port 3000 without rewriting paths. Configure its hostname, Gateway reference, and access controls, install Gateway API CRDs/controller, and apply it explicitly. It is excluded from the default Kustomization. SQL remains a TCP service, not an HTTPRoute. For legacy SSE, set `MCP_TRANSPORT=sse` and connect directly to `/sse`; the sample HTTPRoute covers Streamable HTTP only.

`NAMESPACE` on operational tasks selects the target for exec/port-forward. To change the deployed namespace, edit the Kustomization, Namespace resource, and optional route together; changing the task variable alone does not rewrite them.

## Operate and triage

```sh
task sync-now
kubectl logs -n beads-hub statefulset/beads-hub -c github-reconciler --tail=20
kubectl logs -n beads-hub statefulset/beads-hub -c beads-mcp --tail=20
```

One-shot sync emits JSON and exits nonzero on `partial`; a periodic worker logs the same error result and retries after its interval. A `busy` result means the running pass holds the shared lock. Discovery failures stop the pass before writes. Repository failures allow other repositories to progress and preserve the failed repository's previous snapshots when its source reads fail. Database write failures may leave a partially applied repository; subsequent passes repair it. There is no transactional snapshot of GitHub or of the entire portfolio.

Use local labels for classification and standard Beads priority for ranking:

```sh
bd list --label type:issue --json
bd ready --label 'type:pr,review:needed' --json
bd update <id> --add-label category:dependency-upgrade --priority 3
bd update <id> --claim
bd blocked --json
```

GitHub labels appear as `github:<percent-encoded-name>` to avoid collisions with local categories and comma-based CLI filters. For example, `bug` becomes `github:bug` and `help wanted` becomes `github:help%20wanted`. The reconciler owns source title/description; put durable reasoning in Beads notes and comments. Beads priority and issue type are local choices (imports start as `task`, priority 2).

Only authoritative `closingIssuesReferences` create blockers. GitHub resolves supported closing keywords, including recognized cross-repository references; arbitrary mentions and text in non-closing contexts do not create dependencies. Linked issues must already be indexed by the selected scopes. Removing a reference removes only edges managed by the hub. Existing manual edges are retained. Closing a PR releases its blocking effect through Beads' normal readiness rules.

For backups, preserve the PVC and use [Dolt backups](https://docs.dolthub.com/sql-reference/version-control/backups) appropriate to your storage. A consistent stopped-volume snapshot is also possible. Restore the workspace metadata and database together; test restoration before relying on a backup. Do not delete or reinitialize a PVC to work around startup errors. Initialization errors fail the container without a fallback destructive init.

## Current limits

This branch stores current source snapshots in Beads, not complete GitHub conversations, diffs, or source event history. Categorization and triage are human/agent actions through Beads; there is no model invocation or automatic taxonomy. Closed history is discovered only for items imported while open. No GitHub write-back, webhooks, incremental watermarks, rate-budget scheduler, HA, built-in authentication, or migration from gh-maintainer is implemented. API changes and moving paginated collections converge on later polls; a pass is not a point-in-time source snapshot. A failed or unavailable source never proves deletion.

The pinned UI watches local workspace files rather than polling SQL for every external mutation. Refresh the browser after remote CLI changes if they have not appeared. Its live-refresh behavior across remote clients is an upstream limitation; the smoke test verifies actual issue/PR delivery over its WebSocket API, not a full browser interaction suite.
