# How it works

## Containers

| Container | Does |
| --- | --- |
| `db` | Postgres 16. All state, and the job queue: there is no Redis or broker. |
| `migrate` | Runs `alembic upgrade head` and `whoop-lab setup-db` (time zone, Grafana's read-only role) on every start, then exits. |
| `api` | FastAPI: `/today`, `/experiments`, `/status`, OAuth start and callback, the webhook receiver, `/healthz`, `/uptime`. Pages are server-rendered with Jinja and HTMX, with no JavaScript build step. |
| `worker` | APScheduler, from the same image as `api`. Refreshes the WHOOP token, processes webhook events, polls cycles and reconciles the last 7 days every 3 hours, imports band captures, assigns tomorrow's experiment arm and appends results. |
| `grafana` | Three provisioned dashboards (Health, Data health, Experiment results), read through a role that can only `SELECT`. |

`api` and `grafana` listen on `127.0.0.1` only. Anything that reaches them from outside comes through your tunnel.

## Data flow

1. WHOOP posts a webhook. `api` verifies the HMAC signature against the raw body, inserts the event into `webhook_events` (deduplicated on `trace_id`) and returns 204. It does nothing else.
2. `worker` claims events with `FOR UPDATE SKIP LOCKED`, fetches the record from the WHOOP API and upserts it.
3. Cycles have no webhook, so the worker polls them. The same job re-fetches the trailing 7 days of every collection, which catches missed events, rescored records and deletions.
4. Each evening the worker draws tomorrow's arm for the running experiment and sends it to ntfy. You log compliance and tags on `/today`.
5. Each day the analysis appends a row to `experiment_results`, which Grafana plots.

## Decisions that matter

- **One process refreshes the token.** WHOOP's refresh tokens rotate, and of two simultaneous refreshes only one succeeds. The worker refreshes inside one transaction holding a Postgres advisory lock and saves the new pair in that transaction. A rejected refresh sets the token to "reauth required" and is never retried.
- **Raw first.** Every record is stored as `raw jsonb` plus typed columns, so the typed tables can always be rebuilt.
- **Idempotent writes.** Every write is an upsert keyed on WHOOP's ID. A newer `updated_at` wins, and replaying an event changes nothing.
- **Time.** Timestamps are UTC, and WHOOP's timezone offset is kept with each record. An experiment day is a calendar date in `LOCAL_TIMEZONE`. The mapping from a day to its outcome is defined once, in the `daily_outcomes` view, and both the analysis and Grafana read that view.
- **Only scored data is analysed.** `score_state` is stored, and only `SCORED` rows reach the analysis.

## Tables

| Group | Tables |
| --- | --- |
| Plumbing | `oauth_token` (one row), `webhook_events`, `sync_runs`, `app_config` |
| WHOOP | `cycles`, `sleeps`, `recoveries` (keyed on the sleep), `workouts`; column names are WHOOP's field names from the OpenAPI spec |
| Your input | `daily_tags` |
| Experiments | `experiments`, `assignments`, `experiment_results` (append-only); database triggers stop a locked plan from changing |
| Band | `ble_nights`, `ble_minutes` (see [band.md](band.md)) |
| Views | `daily_outcomes` |
