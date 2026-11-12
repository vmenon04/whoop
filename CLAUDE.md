# whoop-lab

Single-user, self-hosted WHOOP archive plus randomized n-of-1 experiments. Docker Compose on a home server behind a Cloudflare Tunnel. Design: `docs/design.md`. Phases 0–2 (scaffold, auth, ingestion) are built; Grafana, tags, experiments and analysis are not.

## Commands

```sh
uv sync                                   # install
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                             # needs Docker running (testcontainers starts Postgres 16)
uv run pytest tests/test_events.py -k backoff
./scripts/gen_models.sh                   # regenerate src/whoop_lab/whoop/generated.py from the spec
uv run alembic revision -m "..."          # then write the migration by hand
docker compose up -d --build              # db, migrate, api, worker, grafana
docker compose exec worker whoop-lab backfill [--since YYYY-MM-DD]
docker compose exec worker whoop-lab sync [--days N]
```

Run ruff, mypy and pytest before every commit. Small commits, one concern each, conventional messages. No AI attribution anywhere. Do not push unless asked.

## Architecture rules

1. Two processes from one image: `api` (FastAPI) and `worker` (APScheduler). Only the worker calls the WHOOP API or refreshes tokens. One exception: `api` does the one-time authorization-code exchange in `/oauth/callback`.
2. Refresh tokens rotate. Every read-then-write of `oauth_token` happens in one transaction holding `TOKEN_LOCK_KEY` (`pg_advisory_xact_lock`), and the new pair is written in that transaction. A rejected refresh sets `status = 'reauth_required'`, alerts once and is never retried with the same token.
3. The webhook handler reads raw bytes, verifies `X-WHOOP-Signature` (base64 HMAC-SHA256 of timestamp + raw body, client secret as key, constant-time), rejects timestamps more than 5 minutes off, inserts into `webhook_events` (`ON CONFLICT (trace_id) DO NOTHING`) and returns 204. Nothing else.
4. The worker claims events with `FOR UPDATE SKIP LOCKED`, fetches, upserts. Backoff, max attempts, error on the row. `.deleted` events soft-delete.
5. A v2 recovery webhook carries the sleep UUID. Recoveries are keyed on `sleep_id`. There is no recovery-by-sleep endpoint: fetch the sleep, then `GET /v2/cycle/{cycle_id}/recovery`.
6. Cycles have no webhook. The reconcile job polls them and re-fetches the trailing 7 days of every collection. It is the safety net; never remove it.
7. Every record is stored as `raw jsonb` plus typed columns. Every write is an idempotent upsert keyed on the WHOOP ID. `score_state` is stored; only `SCORED` rows may reach analysis.
8. All WHOOP data calls go through `WhoopClient`, which handles `X-RateLimit-*` and 429.
9. Timestamps are UTC (`timestamptz`) with WHOOP's `timezone_offset` kept. An experiment day is a local calendar date (`LOCAL_TIMEZONE`). The day-to-outcome mapping will live only in the `daily_outcomes` view.
10. Config comes from env vars through `Settings`. Never commit secrets. Never log tokens, the client secret or an Authorization header.
11. Cloudflare Access protects every path except `/webhooks/whoop` and the static `/privacy` page (WHOOP's app registration needs a public privacy URL). No login system in the app. Nothing public may read the database.

## Conventions

- Column names on WHOOP tables are WHOOP's field names, score fields flattened; `sleep_needed.*` gets a `sleep_needed_` prefix. `start` and `end` need quoting in raw SQL.
- WHOOP field names come from `whoop/generated.py` (generated from `.claude/skills/whoop-api/reference/openapi.json`). Never edit it or hand-type field names.
- Upsert rule (`ingest/upsert.py`): newer `updated_at` overwrites; equal `updated_at` overwrites only if the payload differs or the row was soft-deleted; older is ignored. An exact replay changes nothing.
- Deletes are soft (`deleted_at`). Deleting a sleep also deletes its recovery. A later fetch that returns the record revives it.
- Reconciliation soft-deletes sleeps, recoveries and workouts inside the window that WHOOP no longer returns, skipping the first day of the window, the last hour, and any collection that came back empty.
- Migrations are hand-written; `tests/test_migrations.py` fails if they drift from the models.
- Functions take `Settings`, a session factory and an `httpx.AsyncClient` as arguments. No module-level engine or client.
- Tests use real Postgres and mock every outbound HTTP call with respx (`whoop_mock` fails on any unmocked request). Payload builders are in `tests/payloads.py`.
- Jobs are declared in `worker/main.py:build_scheduler`, in code, with no job store.
- Before touching WHOOP code read the `whoop-api` skill; before touching experiment or analysis code read the `experiment-analysis` skill. Where that skill is stricter than the design doc, the skill wins.
