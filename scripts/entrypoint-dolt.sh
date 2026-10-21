#!/usr/bin/env bash
set -euo pipefail

workspace="${BEADS_WORKING_DIR:-/var/lib/beads}"
mkdir -p "$workspace/dolt"
dolt config --global --add user.name beads-hub
dolt config --global --add user.email beads-hub@localhost
exec dolt sql-server --data-dir "$workspace/dolt" --host 0.0.0.0 --port 3307
