#!/usr/bin/env bash
# Copy Bluetooth captures from this Mac to the server and import nightly metrics into
# ble_nights, where Grafana's "Nightly health" dashboard reads them.
#
#   ./scripts/push_ble.sh            # import captures from the last 3 days
#   ./scripts/push_ble.sh --all      # recompute every night (after a metrics change)
#
# Run it in a real terminal: the import needs sudo on the server, which asks for a
# password. Arguments are passed to `whoop-lab ble-import`.
# WHOOP_LAB_HOST (default slimjim) and WHOOP_LAB_DIR (default /srv/whoop-lab) override
# where the server copy lives.
set -euo pipefail

host="${WHOOP_LAB_HOST:-slimjim}"
dir="${WHOOP_LAB_DIR:-/srv/whoop-lab}"
cd "$(dirname "$0")/.."

if [ ! -d captures ]; then
    echo "No captures/ directory here; drain the band first." >&2
    exit 1
fi

# The worker container reads the files as its own user, so they must be readable.
# Symbolic modes: the rsync 2.6.9 that ships with macOS rejects numeric ones.
ssh "$host" "mkdir -p '$dir/captures'"
rsync -a --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r captures/ "$host:$dir/captures/"
ssh -t "$host" "cd '$dir' && sudo docker compose exec -T worker whoop-lab ble-import $(printf '%q ' "$@")"
