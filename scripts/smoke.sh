#!/usr/bin/env bash
set -euo pipefail

image="${1:-beads-hub:test}"
stack="beads-hub-smoke-$$"
root="$(cd "$(dirname "$0")/.." && pwd)"
export IMAGE="$image" GH_TOKEN= GH_SYNC_QUERY=user:@me MCP_TRANSPORT=streamable-http
export BEADS_REMOTE_PASSWORD=synthetic-smoke-password
export BEADS_UI_PORT=0 BEADS_MCP_PORT=0 BEADS_SQL_PORT=0
compose() {
  docker compose --project-name "$stack" --file "$root/compose.yaml" "$@"
}
cleanup() {
  compose down --volumes >/dev/null 2>&1 || true
}
trap cleanup EXIT
if ! compose up -d --build --wait --wait-timeout 180; then
  compose logs
  exit 1
fi
dolt_id="$(compose ps -q dolt)"
ui_address="$(compose port dolt 3000)"
case "$ui_address" in
  127.0.0.1:*) curl -fsS "http://$ui_address/healthz" ;;
  *) echo 'UI port must be bound to localhost' >&2; exit 1 ;;
esac
if ! docker run --rm --network "container:$dolt_id" \
  -v "${stack}_data:/var/lib/beads" -v "$root/tests:/tmp/hub-tests:ro" \
  -e PYTHONPATH=/usr/local/bin "$image" python3 /tmp/hub-tests/smoke_runtime.py; then
  compose logs
  exit 1
fi

# A separate container verifies the documented remote pointer over the Compose network.
docker run --rm --network "${stack}_default" -e BEADS_DOLT_SERVER_HOST=dolt \
  -e BEADS_DOLT_SERVER_USER=beads -e BEADS_DOLT_PASSWORD=synthetic-smoke-password \
  -e BEADS_DIR=/tmp/pointer/.beads -e BEADS_WORKING_DIR=/tmp/pointer "$image" bash -c '
    set -euo pipefail
    mkdir -p /tmp/pointer
    cd /tmp/pointer
    bd init --server --external --server-host dolt --server-port 3307 --server-user beads \
      --database beads_hub --prefix hub --non-interactive --skip-agents --skip-hooks
    bd list --all --limit 0 --json
  '

compose down
compose up -d --wait --wait-timeout 180
compose exec -T hub python3 /usr/local/bin/hub_runtime.py
compose exec -T hub python3 -c \
  'import json, subprocess; assert len(json.loads(subprocess.check_output(["bd", "list", "--all", "--limit", "0", "--json"], text=True))) == 2'
echo 'Smoke checks passed, including remote pointer and persistent restart.'
