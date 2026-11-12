#!/usr/bin/env sh
# Dump the database to ./backups (custom format). Run from cron on the server.
# The dump contains the WHOOP tokens: keep it as private as .env.
set -eu
cd "$(dirname "$0")/.."
mkdir -p backups
umask 077
out="backups/whoop_lab-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$out.tmp"
mv "$out.tmp" "$out"
# Keep the newest 30 dumps.
ls -1t backups/whoop_lab-*.dump | tail -n +31 | xargs -r rm --
echo "wrote $out"
