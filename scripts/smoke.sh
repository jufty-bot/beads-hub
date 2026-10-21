#!/usr/bin/env bash
set -euo pipefail

image="${1:-beads-hub:test}"
stack="beads-hub-smoke-$$"
root="$(cd "$(dirname "$0")/.." && pwd)"
cleanup() {
  docker rm -f "$stack-mcp" "$stack-ui" "$stack-dolt" >/dev/null 2>&1 || true
  docker volume rm "$stack-data" >/dev/null 2>&1 || true
  docker network rm "$stack" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create "$stack" >/dev/null
docker volume create "$stack-data" >/dev/null
docker run -d --name "$stack-dolt" --network "$stack" --network-alias dolt \
  -v "$stack-data:/var/lib/beads" "$image" /usr/local/bin/entrypoint-dolt.sh >/dev/null
docker run -d --name "$stack-ui" --network "container:$stack-dolt" \
  -v "$stack-data:/var/lib/beads" -e BEADS_REMOTE_PASSWORD=synthetic-smoke-password \
  "$image" >/dev/null
docker run -d --name "$stack-mcp" --network "container:$stack-dolt" \
  -v "$stack-data:/var/lib/beads" "$image" /usr/local/bin/entrypoint-mcp.py >/dev/null
if ! docker run --rm --network "container:$stack-dolt" \
  -v "$stack-data:/var/lib/beads" -v "$root/tests:/tmp/hub-tests:ro" \
  -e PYTHONPATH=/usr/local/bin "$image" python3 /tmp/hub-tests/smoke_runtime.py; then
  docker logs "$stack-dolt"
  docker logs "$stack-ui"
  docker logs "$stack-mcp"
  exit 1
fi

# A separate container verifies the documented remote pointer, not pod-local TCP.
docker run --rm --network "$stack" -e BEADS_DOLT_SERVER_HOST=dolt \
  -e BEADS_DOLT_SERVER_USER=beads -e BEADS_DOLT_PASSWORD=synthetic-smoke-password \
  -e BEADS_DIR=/tmp/pointer/.beads -e BEADS_WORKING_DIR=/tmp/pointer "$image" bash -c '
    set -euo pipefail
    mkdir -p /tmp/pointer
    cd /tmp/pointer
    bd init --server --external --server-host dolt --server-port 3307 --server-user beads \
      --database beads_hub --prefix hub --non-interactive --skip-agents --skip-hooks
    bd list --all --limit 0 --json
  '

docker restart "$stack-dolt" "$stack-ui" >/dev/null
docker run --rm --network "container:$stack-dolt" -v "$stack-data:/var/lib/beads" \
  "$image" python3 /usr/local/bin/hub_runtime.py
echo 'Smoke checks passed, including remote pointer and persistent restart.'
