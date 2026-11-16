#!/usr/bin/env bash
# Copy Bluetooth captures from this computer to the server that runs the stack. The worker
# there checks every few minutes and imports captures from the last three days whenever
# they change; nothing else needs running. Not needed if the stack runs on this computer.
#
#   WHOOP_HOST=myserver WHOOP_CAPTURES=/path/to/captures ./scripts/push_ble.sh
#
# WHOOP_HOST is an ssh destination. WHOOP_CAPTURES is the server folder the worker mounts
# at /captures (CAPTURES_DIR in the server's .env; default whoop/captures, relative to
# your home folder there).
set -euo pipefail

host="${WHOOP_HOST:?set WHOOP_HOST to the ssh name of the server}"
dest="${WHOOP_CAPTURES:-whoop/captures}"
cd "$(dirname "$0")/.."

if [ ! -d captures ]; then
    echo "No captures/ directory here; drain the band first." >&2
    exit 1
fi

# The worker container reads the files as its own user, so they must be readable.
# Symbolic modes: the rsync 2.6.9 that ships with macOS rejects numeric ones.
rsync -a --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r captures/ "$host:$dest/"
echo "Copied to $host:$dest; the worker imports them within five minutes."
