#!/usr/bin/env bash
set -euo pipefail
: "${BEADS_REMOTE_PASSWORD:?Inject the remote SQL account password at runtime}"

workspace="${BEADS_WORKING_DIR:-/var/lib/beads}"
mkdir -p "$workspace"
cd "$workspace"
python3 /usr/local/bin/hub_runtime.py --sql

if [ ! -f "$workspace/.beads/metadata.json" ]; then
  bd init --server --external --server-host "${BEADS_DOLT_SERVER_HOST:-127.0.0.1}" \
    --server-port "${BEADS_DOLT_SERVER_PORT:-3307}" --database beads_hub \
    --prefix "${BEADS_PREFIX:-hub}" --non-interactive --skip-agents --skip-hooks
fi
python3 /usr/local/bin/hub_runtime.py
python3 /usr/local/bin/hub_runtime.py --configure-remote
unset BEADS_REMOTE_PASSWORD

# bdui start detaches; run the pinned package's actual server as the container process.
export HOST=0.0.0.0 PORT=3000
exec node /usr/local/lib/node_modules/beads-ui/server/index.js
