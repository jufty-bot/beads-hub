# Implementation and validation checklist

- [x] Start `beads-hub` as an orphan branch; retain the original implementation on `chilly-ostrich`.
- [x] Implement paginated repository scopes and issue/PR reconciliation in `scripts/gh_global_sync.py`.
- [x] Preserve source identities, local categories/priority/claims/notes, and unrelated metadata; explicit closure and reopening.
- [x] Implement actionable PR signals, cross-repository blockers, removed-reference cleanup, and manual-edge preservation.
- [x] Serialize periodic/manual/MCP sync through the shared filesystem lock.
- [x] Retain official beads-mcp schemas/resources and add a synchronous trigger.
- [x] Add foreground Dolt/UI roles, readiness waits, unified image, PVC, Services, ConfigMap, StatefulSet, and optional HTTPRoute.
- [x] Add token-free tests for partial data, scope changes, pagination beyond 1,000 items, reference pagination, identity continuity, local triage, graph behavior, and lock contention.
- [x] Build the ARM64 image with the pinned upstream binaries and locked Python runtime.
- [x] Validate all six rendered base Kubernetes resources with kubeconform 0.7.0 in strict mode.
- [x] Complete `task smoke`: real Dolt/Beads reconciliation, readiness and forced source closure with a live blocker, graph removal, claims, local metadata, reopening, renames, UI WebSocket data delivery, official MCP tools/resource/trigger over HTTP, authenticated remote SQL pointer, and persistent restart. Disposable containers/volumes are cleaned up.
- [x] Final `task fix`, `task lint`, `task check`, and `task test`: formatting/lint/shell syntax clean, strict types pass, 34 tests pass. `uv run --locked python -m py_compile scripts/*.py` and `git diff --cached --check` pass. Local Markdown links resolve.
- [x] Validate the optional HTTPRoute against the official Gateway API v1.2.0 `v1` CRD schema.
- [ ] Run `task validate` against a reachable Kubernetes API server. Current environment: configured `127.0.0.1:6443` refuses connections; rendering and independent base schema validation pass.
- [ ] Validate the optional HTTPRoute against the target Gateway controller and authentication configuration.
- [ ] Run a credentialed live pilot and backup/restore exercise; see [roadmap](roadmap.md).

Validated on 2026-10-20 with Python 3.14.5 locally and Python 3.11 in the ARM64 Docker runtime. Beads/beads-mcp 1.3.1, Dolt 2.5.1, and beads-ui 0.12.6 were exercised together. The smoke workflow uses synthetic GitHub records and a synthetic SQL password; actual credentialed GitHub behavior, AMD64 builds, full browser interactions, Gateway-controller behavior, and backup/restore remain unverified. No cluster deployment, registry push, GitHub write, or credentialed source sync has been performed.
