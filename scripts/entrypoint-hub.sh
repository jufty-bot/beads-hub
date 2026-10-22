#!/usr/bin/env bash
set -euo pipefail
: "${BEADS_REMOTE_PASSWORD:?Inject the remote SQL account password at runtime}"

workspace="${BEADS_WORKING_DIR:-/var/lib/beads}"
mkdir -p "$workspace"
cd "$workspace"
beads-hub-runtime --sql

if [ ! -f "$workspace/.beads/metadata.json" ]; then
  bd init --server --external --server-host "${BEADS_DOLT_SERVER_HOST:-127.0.0.1}" \
    --server-port "${BEADS_DOLT_SERVER_PORT:-3307}" --database beads_hub \
    --prefix "${BEADS_PREFIX:-hub}" --non-interactive --skip-agents --skip-hooks
fi
beads-hub-runtime
beads-hub-runtime --configure-remote
unset BEADS_REMOTE_PASSWORD

# bdui start detaches; run the pinned package's actual server as the container process.
export HOST=0.0.0.0 PORT=3000
exec node /usr/local/lib/node_modules/beads-ui/server/index.js
