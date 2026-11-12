# whoop-lab

Single-user, self-hosted WHOOP archive plus randomized n-of-1 experiments. Docker Compose on a home server behind a Cloudflare Tunnel. Design: `docs/design.md`. All phases are built: ingestion, `daily_outcomes`, Grafana, tags, experiments, analysis.

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
docker compose exec worker whoop-lab power --outcome ln_hrv_rmssd --days 28
docker compose exec worker whoop-lab analyze <name-or-id>
uv run whoop-lab simulate                 # analysis validation; needs no database
uv run python scripts/gen_dashboards.py   # regenerate grafana/dashboards/*.json
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
9. Timestamps are UTC (`timestamptz`) with WHOOP's `timezone_offset` kept. An experiment day is a local calendar date (`LOCAL_TIMEZONE`, copied into `app_config` by `setup-db`). The day-to-outcome mapping lives only in the `daily_outcomes` view (migration 0003); analysis and Grafana read the view and nothing re-derives it.
10. Config comes from env vars through `Settings`. The database URL is built from `POSTGRES_*` parts with `URL.create` (never by string interpolation), in `Settings.db_url` and Alembic alike. Never commit secrets. Never log tokens, the client secret or an Authorization header.
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

## Experiment and analysis conventions

The `experiment-analysis` skill is the authority; these are where its rules live in code.

- `experiments/service.py` is the only writer of `experiments` and `assignments`. Triggers in migration 0004 enforce the same rules: a locked experiment changes only in `status`, assignments change only in `complied` and `note`, `experiment_results` is append-only. `TRUNCATE` bypasses row triggers; only tests use it.
- Locking sets `started_on` to tomorrow. `planned_days` counts assigned days; every assigned day (either arm) is followed by `washout_days` days with no row, which do not count. A missed evening leaves a gap that is never filled in.
- Tomorrow's arm is drawn by `experiments/jobs.py:assign_tomorrow`, never before `ASSIGN_HOUR`. No page or query shows anything beyond the rows that exist.
- Each block gets a fresh seed stored on its rows; `randomize.arm_for(seed, block_size, position)` reproduces any arm.
- `analysis/inference.py` is the vectorised randomization test. `tests/test_inference.py` holds the skill's reference implementation verbatim and checks the two agree. `BlockDesign.from_assignments` rebuilds blocks from stored `stratum`, `block_index`, `block_position`, never from dates.
- A day is "due" once `local_date < today`. Days not yet due are neither analysed nor counted as missing.
- Methods written to `experiment_results.method`: `itt_randomization` (primary), `ols_adjusted_hc3_secondary`, `per_protocol_biased`, `itt_randomization_exploratory`. `analysis/pipeline.py:describe` is the only wording used for results.
- An experiment closes in `complete_if_finished`, on the calendar alone. Nothing may close one based on results.
- Any analysis change must pass `whoop-lab simulate` (also `tests/test_simulation.py`): null rejection 0.03 to 0.07, coverage 0.93 to 0.97, a failing calibration under the wrong block design, a plain label shuffle told apart from the real test, and the same calibration for the harder designs in `simulate.SCENARIOS`. The simulator assigns days with the functions in `experiments/randomize.py` that `assign_day` uses; keep it that way.
- Outcome metrics are listed in `experiments/outcomes.py`. The WHOOP recovery score is deliberately not one of them.
- Grafana dashboards are generated by `scripts/gen_dashboards.py`; edit the script, not the JSON. Grafana connects as `grafana_ro`, which `setup_db.py` grants `SELECT` on analysis tables only. A new table Grafana should read must be added to `GRAFANA_READABLE`.
- Page POSTs go through `api/deps.py:same_origin` and answer with a 303 redirect; `hx-boost` makes them feel instant. No JSON API.
