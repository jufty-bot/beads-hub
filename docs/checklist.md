# Implementation and validation checklist

- [x] Publish the clean Beads implementation history as `main` in [jufty-bot/beads-hub](https://github.com/jufty-bot/beads-hub); retain the earlier application and history in [gh-maintainer](https://github.com/jufty-bot/gh-maintainer).
- [x] Implement paginated repository scopes and issue/PR reconciliation in `scripts/gh_global_sync.py` and `scripts/github_source.py`.
- [x] Replace `gh api` subprocesses with a locked GitHubKit client; remove GitHub CLI from the image. Exercise SDK HTTP authentication, REST Link pagination beyond 1,000 items, GraphQL reference pagination/partial errors, rate/auth/server failures, and network timeouts in `tests/test_github_source.py`.
- [x] Attach selected author/assignee/milestone/date/repository facts and PR branches/SHAs/size/review/CI metadata, with observation time and reviewer coverage. Verify metadata refresh, clearing, closure evidence, readable summaries, and preservation of local fields in `tests/test_sync.py` and the real-runtime smoke test.
- [x] Preserve source identities, local categories/priority/claims/notes, and unrelated metadata; explicit closure and reopening.
- [x] Verify continuous polling across creation, source edits, approval, merge/closure, failed-pass recovery, and reopening in `test_polling_converges_across_item_lifecycle`. Reproduce and fix skipped upstream merge evidence for locally closed beads in `test_locally_closed_bead_still_observes_upstream_closure`. Expose the pause as `GH_SYNC_INTERVAL` and validate Compose with a 60-second override.
- [x] Implement actionable PR signals, cross-repository blockers, removed-reference cleanup, and manual-edge preservation.
- [x] Serialize periodic/manual/MCP sync through the shared filesystem lock.
- [x] Retain official beads-mcp schemas/resources and add a synchronous trigger.
- [x] Add foreground Dolt/UI roles, readiness waits, and a unified image.
- [x] Replace deployment manifests with `compose.yaml`: four services, health-gated startup, one named data volume, runtime credentials, localhost ports, and restart policies.
- [x] Add token-free tests for partial data, scope changes, pagination beyond 1,000 items, reference pagination, identity continuity, local triage, graph behavior, and lock contention.
- [x] Build the ARM64 image with the pinned upstream binaries and locked Python runtime.
- [x] Validate Compose configuration using `BEADS_REMOTE_PASSWORD=synthetic-smoke-password task validate`; the quiet check does not print credentials.
- [x] Complete `task smoke` using the checked-in Compose stack: all four services become healthy; real Dolt/Beads reconciliation, forced source closure with a live blocker, graph removal, claims, local metadata, reopening, renames, UI WebSocket data delivery, official MCP tools/resource/trigger over HTTP, authenticated remote SQL pointer, and host-local UI publishing pass. A full down/up cycle retains both work items. Disposable containers/networks/volumes are cleaned up.
- [x] Final `task fix`, `task lint`, `task check`, and `task test`: formatting/lint/shell syntax clean, strict types pass, 49 tests pass. `uv run --locked python -m py_compile scripts/*.py` and `git diff --check` pass. Local Markdown links resolve.
- [ ] Run a credentialed live pilot and backup/restore exercise; see [roadmap](roadmap.md).

Validated with Python 3.14.5 locally, Python 3.11 in the ARM64 Docker runtime, and Docker Compose 5.1.2. Beads/beads-mcp 1.3.1, Dolt 2.5.1, and beads-ui 0.12.6 were exercised together. The smoke workflow uses synthetic GitHub records and a synthetic SQL password; actual credentialed GitHub behavior, AMD64 builds, full browser interactions, and backup/restore remain unverified. No registry push, GitHub write, or credentialed source sync has been performed.
