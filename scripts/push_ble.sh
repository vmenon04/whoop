#!/usr/bin/env bash
# Copy Bluetooth captures from this Mac to the server. The worker there checks every few
# minutes and imports captures from the last three days into ble_nights and ble_minutes
# whenever they change; nothing else needs running.
#
#   ./scripts/push_ble.sh
#
# To recompute every night (after a metrics version change), run on the server:
#   sudo docker exec whoop-worker-1 whoop-lab ble-import --all
#
# WHOOP_HOST (default slimjim) and WHOOP_CAPTURES (default /srv/whoop/captures) override
# where the captures go; the worker mounts that folder read-only at /captures.
set -euo pipefail

host="${WHOOP_HOST:-slimjim}"
dest="${WHOOP_CAPTURES:-/srv/whoop/captures}"
cd "$(dirname "$0")/.."

if [ ! -d captures ]; then
    echo "No captures/ directory here; drain the band first." >&2
    exit 1
fi

# The worker container reads the files as its own user, so they must be readable.
# Symbolic modes: the rsync 2.6.9 that ships with macOS rejects numeric ones.
rsync -a --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r captures/ "$host:$dest/"
echo "Copied to $host:$dest; the worker imports them within five minutes."
