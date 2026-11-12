# WHOOP Experiment Platform: Design and Claude Code Prompt

Nov 11, 2026 · @Vasudev Menon

## Idea

A self-hosted service that archives your WHOOP data and runs randomized n-of-1 experiments on it. The WHOOP app reports scores. This answers causal questions the app cannot, such as whether a noon caffeine cutoff raises next-morning HRV.

Working name: `whoop-lab`.

**What it does**

- Keeps a complete, queryable copy of your recovery, sleep, cycle and workout data in Postgres on slimjim.
- Lets you define an experiment up front: hypothesis, intervention, outcome metric, duration.
- Randomizes each day into treatment or control and tells you which one tonight.
- Computes the effect with a pre-registered analysis and shows the running estimate in Grafana.

**What it does not do**

- Rebuild the WHOOP app. Grafana panels are for experiment results and data health, not a prettier recovery ring.
- Serve other users. One user, one token row, no auth system beyond Cloudflare Access.
- Use raw heart-rate streams. As far as I know the public API exposes scored summaries only. Confirm in the API reference before planning anything that needs beat-level data.

**Why it is worth building**

- The experiment layer is a real statistics problem: small n, autocorrelated outcomes, compliance. It is a better portfolio piece than a dashboard.
- The ingestion half is reusable. Anything else you want from the data later reads the same tables.

## WHOOP API facts the design depends on

A handful of API behaviors drive most of the design. The first five rows were checked against WHOOP's own docs on the date above. The last two come from third-party write-ups or memory and need confirming in the API reference.

| Fact | Detail | Design consequence |
| --- | --- | --- |
| [OAuth 2.0](https://developer.whoop.com/docs/developing/oauth) | Auth URL `https://api.prod.whoop.com/oauth/oauth2/auth`, token URL `https://api.prod.whoop.com/oauth/oauth2/token`. Redirect URL must be registered and HTTPS. A self-generated `state` must be 8 characters. | One-time browser flow at a tunnel hostname. |
| [Refresh tokens rotate](https://developer.whoop.com/docs/developing/oauth) | `offline` scope is required to get a refresh token. Each refresh returns a new refresh token and invalidates the old access token. Of two simultaneous refreshes, only the first succeeds. Sample `expires_in` is 3600 s. | Exactly one process refreshes, on a schedule, under a database lock. Persist the new refresh token in the same transaction. |
| [Webhooks](https://developer.whoop.com/docs/developing/webhooks/) | Events: `workout`, `sleep`, `recovery`, each `.updated` and `.deleted`. Body: `user_id`, `id`, `type`, `trace_id`. In v2, `id` is a UUID, and a recovery event carries the UUID of its sleep. Failed deliveries retry 5 times over about an hour. Respond 2XX within about a second. | Webhook handler only verifies, stores the event and returns. A worker fetches the record. Dedupe on `trace_id`. Handle deletes. |
| [Webhook signature](https://developer.whoop.com/docs/developing/webhooks/) | Headers `X-WHOOP-Signature` and `X-WHOOP-Signature-Timestamp`. Signature is base64 of HMAC-SHA256 over timestamp + raw body, keyed with the client secret. | Verify against the raw bytes before parsing JSON. Reject stale timestamps. |
| [Rate limits](https://developer.whoop.com/docs/developing/rate-limiting) | 100 requests per minute and 10,000 per day by default, reported in `X-RateLimit-*` headers. | Irrelevant in steady state. The backfill should still read the headers and back off. |
| No cycle webhooks ([Terra's write-up](https://tryterra.co/blog/whoop-api-data-access-permissions-limitations-2026)) | Cycles, day strain and body measurements have no webhook events. | Poll cycles on a schedule. The same poll doubles as reconciliation for missed webhooks. |
| Endpoints and fields (from memory, unverified) | Base `https://api.prod.whoop.com/developer/v2`. Collections for cycle, recovery, sleep and workout, paginated with `limit`, `start`, `end`, `nextToken`. Scores carry a `score_state` of scored, pending or unscorable. | Claude Code must read the OpenAPI spec at developer.whoop.com/api before writing the client. Store `score_state` and only analyze scored rows. |

Scopes to request: `offline`, `read:recovery`, `read:cycles`, `read:sleep`, `read:workout`, `read:profile`, `read:body_measurement`.

## System architecture

Four containers on slimjim behind one Cloudflare Tunnel hostname: an API, a worker, Postgres and Grafana. Postgres is the only state and also the job queue, so there is no Redis or broker.

&#91;embedded content: whoop-lab architecture · 4 containers behind one tunnel\]

Inbound traffic reaches only the API and Grafana, both through Cloudflare. The worker is the single component that talks to WHOOP, which is what keeps token rotation safe.

**Components**

| Component | Responsibility | Notes |
| --- | --- | --- |
| Cloudflare Tunnel | Public HTTPS for one hostname, for example `whoop.<your-domain>` | Reuse your existing `cloudflared` if it already runs on slimjim. No router ports. |
| Cloudflare Access | Login wall for everything except the webhook path | Bypass policy on `/webhooks/whoop` only. That path is protected by the HMAC check instead. |
| `api` (FastAPI) | OAuth start and callback, webhook receiver, tag form, experiment admin pages, health endpoint | Webhook handler does three things: verify signature, insert event row, return 204. |
| `worker` (same image, different entrypoint) | Token refresh, event processing, cycle polling, reconciliation, nightly assignment, nightly analysis, notifications | A single scheduler process. This is the only code that calls the WHOOP API or refreshes tokens. |
| Postgres 16 | Raw payloads, typed metrics, tags, experiments, assignments, results, event queue, token | Plain Postgres. A few rows per day does not need TimescaleDB. |
| Grafana | Experiment results and data-health panels | Dashboards and datasource provisioned from files in the repo. Read-only database role. |
| ntfy (optional) | Push "tonight's arm" and data-gap alerts to your phone | Self-hosted container or the public ntfy.sh with a private topic. |

**Data flow**

1. WHOOP posts a webhook. The API verifies the HMAC, inserts into `webhook_events` (unique on `trace_id`) and returns 204.
2. The worker claims unprocessed events with `SELECT ... FOR UPDATE SKIP LOCKED`, fetches the record from the WHOOP API and upserts raw JSON plus typed columns.
3. Every few hours the worker polls cycles, which have no webhook, and re-fetches the last 7 days of every collection to catch missed events and rescored records.
4. Each evening the worker randomizes tomorrow's arm for the active experiment and pushes it to your phone.
5. You log compliance and tags on the form the next day.
6. Each night the analysis job recomputes results for active experiments and writes them to `experiment_results`. Grafana reads that table.

**Decisions worth fixing now**

- **One writer for tokens.** Because refresh tokens rotate, the worker refreshes on a schedule inside a transaction holding a Postgres advisory lock. The API container never refreshes.
- **Raw first.** Every fetched record is stored as `jsonb` before parsing. Typed tables can be rebuilt from raw at any time.
- **Idempotent everywhere.** Sleeps and recoveries are rescored after the first event, so every write is an upsert keyed on the WHOOP ID.
- **Time.** Store UTC plus WHOOP's timezone offset. An experiment day is your local calendar date. The outcome for day D is the recovery tied to the sleep that ends on the morning of D+1.
- **Secrets.** Client secret, database password and ntfy topic live in an `.env` file that is not committed. The repo ships `.env.example`.
- **Backups.** Nightly `pg_dump` to a second disk or object storage. The API can backfill WHOOP data, but tags and assignments exist nowhere else.
- **Monitoring.** Alert if no scored recovery has arrived by noon local time, or if a token refresh fails.

## Data model

Three groups of tables: plumbing, WHOOP data, and experiments. Every WHOOP table has the same shape: WHOOP ID as primary key, typed columns for what you query, and a `raw jsonb` column holding the full payload.

| Table | Key columns | Purpose |
| --- | --- | --- |
| `oauth_token` | single row: `access_token`, `refresh_token`, `expires_at`, `updated_at` | Current token pair. Updated only by the worker's refresh job. |
| `webhook_events` | `trace_id` (unique), `type`, `resource_id`, `received_at`, `processed_at`, `attempts`, `error` | Durable inbox and work queue. |
| `sync_runs` | `kind`, `started_at`, `finished_at`, `rows_upserted`, `error` | Audit of polls and backfills. Feeds the data-health panel. |
| `cycles` | `id`, `start`, `end`, `timezone_offset`, `score_state`, `strain`, `avg_hr`, `max_hr`, `kilojoule`, `raw` | Physiological days. Polled, not pushed. |
| `sleeps` | `id` (UUID), `cycle_id`, `start`, `end`, `nap`, `score_state`, stage durations, `performance_pct`, `efficiency_pct`, `consistency_pct`, `respiratory_rate`, `raw` | Sleep records. Naps are excluded from outcomes. |
| `recoveries` | `sleep_id` (primary key), `cycle_id`, `score_state`, `recovery_score`, `hrv_rmssd_ms`, `resting_hr`, `spo2_pct`, `skin_temp_c`, `user_calibrating`, `raw` | Morning outcomes. Keyed on sleep because that is what the v2 webhook sends. |
| `workouts` | `id` (UUID), `start`, `end`, `sport`, `score_state`, `strain`, `avg_hr`, `max_hr`, `kilojoule`, zone durations, `raw` | Covariates such as training load. |
| `daily_tags` | `local_date`, `key`, `value`, unique on date and key | Things WHOOP cannot see: caffeine cutoff, alcohol, late meal, screens. |
| `experiments` | `id`, `name`, `hypothesis`, `intervention`, `control`, `outcome_metric`, `analysis_spec jsonb`, `block_size`, `washout_days`, `planned_days`, `status`, `started_on`, `locked_at` | One row per experiment. `analysis_spec` is frozen at `locked_at`. |
| `assignments` | `experiment_id`, `local_date`, `arm`, `block_index`, `seed`, `complied`, `note`, unique on experiment and date | The randomization record. Never edited after the date passes, except `complied` and `note`. |
| `experiment_results` | `experiment_id`, `computed_at`, `n_treatment`, `n_control`, `effect`, `ci_low`, `ci_high`, `p_value`, `method` | Append-only. Grafana plots the running estimate from this. |

One view does most of the analytic work: `daily_outcomes`, one row per local date, joining that night's main sleep and next-morning recovery to the day's strain, workouts and tags. Analysis code and Grafana both read it, so the day-to-outcome mapping is defined in exactly one place.

Field names for WHOOP columns above are from memory. Claude Code should take the real names from the OpenAPI spec.

## Experiment engine

The engine does three things: randomize days into arms, hold the analysis fixed before data arrives, and report an effect with honest uncertainty. Everything else is convenience.

**Lifecycle**

1. **Baseline.** Collect 3 to 4 weeks of data with no experiment running. WHOOP is still calibrating at first, and you need your own night-to-night variance of HRV to size experiments.
2. **Draft.** Write the hypothesis, intervention, control, outcome metric, covariates, duration and analysis method into an `experiments` row.
3. **Lock.** Locking freezes `analysis_spec`. After this, the code refuses edits to the spec.
4. **Run.** Each evening the worker assigns tomorrow's arm and notifies you. You mark compliance the next day.
5. **Report.** The nightly job appends a result row. The experiment closes when `planned_days` is reached, not when the estimate looks good.

**Randomization**

- Permuted blocks of 4: two treatment days and two control days in random order per block. Arms stay balanced even if you stop early.
- Stratify by weekday versus weekend if the intervention interacts with your work schedule.
- Seed stored per block, so the sequence is reproducible and auditable.
- For interventions with carryover, such as alcohol, set `washout_days`. Washout days get no assignment and are dropped from analysis.

**Outcomes**

- Primary: natural log of next-morning HRV (RMSSD). Log scale makes effects read as percent changes and tames the skew.
- Secondary: resting heart rate, sleep efficiency, deep-sleep minutes.
- Not primary: the recovery score. It is WHOOP's proprietary composite of the inputs above, so an effect on it is hard to interpret.

**Analysis**

- **Estimand.** Intention to treat: compare days by assigned arm, whatever you actually did. A per-protocol estimate is reported next to it and labeled as biased.
- **Test.** Randomization inference. Re-run the block randomization 10,000 times, recompute the difference in means each time, and read the p-value off that distribution. This is valid at small n and makes no normality or independence assumption beyond the randomization itself.
- **Interval.** Invert the randomization test for a 95% interval on the constant additive effect.
- **Precision.** A covariate-adjusted estimate regresses the outcome on arm plus the prior 7-day mean of the outcome and that day's strain. Report it second. It usually narrows the interval.
- **Missing data.** Unscored or missing mornings are dropped and counted. If more than 20% of days are missing, the report says the experiment is compromised.

**Power, roughly**

With 28 days split 14 and 14, the smallest effect you can reliably detect is about one night-to-night standard deviation of the outcome. Subtle effects need 6 to 8 weeks. The baseline period gives you the real standard deviation, and a small `power` command should print minimum detectable effect by duration before you lock anything.

**Rules**

- One experiment at a time. Two only if the interventions cannot plausibly interact.
- No peeking-driven stopping. The dashboard shows the running estimate, but the end date is fixed at lock.
- Do not start an experiment during illness, travel or an unusual training block. Tag those days so they can be examined afterward.

**First experiments**

| Experiment | Treatment vs control | Why it is a good early one |
| --- | --- | --- |
| Caffeine cutoff | Last caffeine by noon vs by 4pm | Easy compliance, plausible effect, no carryover. |
| Screens before bed | No screens in the last 60 minutes vs normal | Cheap to run. Tests whether the common advice holds for you. |
| Late meal | Last food 3+ hours before bed vs within 1 hour | Clear mechanism, visible in resting heart rate. |
| Bedroom temperature | Thermostat 2 to 3 °F lower vs normal | Needs roommate agreement. Effect likely shows in deep sleep. |

This is self-experimentation on sleep and lifestyle habits, not medical advice. Keep interventions to things you would do anyway.

## Build phases

Seven phases, numbered 0 to 6, each ending in something you can verify. Phases 1 to 3 are the archive and take roughly two weekends. Phases 4 to 6 are the experiment layer and can be built during the baseline period.

| Phase | Deliverable | Done when |
| --- | --- | --- |
| 0. Setup | WHOOP developer app registered, tunnel hostname routed, Access policy with webhook bypass, repo scaffolded with Compose | `GET /healthz` returns 200 through the tunnel. Everything except `/webhooks/whoop` asks for Access login. |
| 1. Auth | OAuth start and callback, token row, scheduled refresh under advisory lock | Token survives 24 hours of hourly refreshes with no manual step. A forced double refresh does not lose the token. |
| 2. Ingest | WHOOP client from the OpenAPI spec, backfill command, webhook receiver with HMAC check, event worker, cycle poll, reconciliation | Backfill loads full history. A new sleep appears in Postgres within a minute of the webhook. Replaying a webhook changes nothing. A bad signature gets 401. |
| 3. Observe | Grafana provisioned from repo files, data-health panels, `daily_outcomes` view, ntfy alerts | Dashboard comes up from a clean `docker compose up`. Killing the worker for a morning triggers the no-recovery alert. |
| 4. Tags | Phone-friendly daily form behind Access, compliance toggle | Logging a day takes under 20 seconds on your phone. |
| 5. Experiments | Experiment create and lock, block randomizer, evening assignment push, `power` command | Locked spec cannot be edited. Assignment sequence is reproducible from stored seeds. Arms are balanced within every block. |
| 6. Analysis | Randomization test, interval, adjusted estimate, results table, Grafana results panels | On simulated data with a planted effect, the estimate recovers it. On simulated null data, the false-positive rate is near 5%. |

The phase 6 test is the one that matters most. An analysis pipeline that has only ever seen real data cannot be told apart from one with a bug.

## Claude Code prompt

Paste this into Claude Code in an empty repo, in plan mode. It asks for a plan and phase 0 to 2 first, so you can review the foundation before the experiment layer is built on it. Replace the two placeholders in angle brackets.

```text
You are building `whoop-lab`: a single-user, self-hosted service that archives my WHOOP data and runs randomized n-of-1 experiments on it. It runs in Docker Compose on my home server behind a Cloudflare Tunnel at https://<whoop.mydomain.com>. I am the only user.

## Read first
Before writing any client code, fetch and read the current WHOOP docs. Do not rely on memory for endpoint paths, field names or scopes.
- API reference and OpenAPI spec: https://developer.whoop.com/api
- OAuth: https://developer.whoop.com/docs/developing/oauth
- Webhooks: https://developer.whoop.com/docs/developing/webhooks
- Rate limiting: https://developer.whoop.com/docs/developing/rate-limiting
Use the v2 API and v2 webhooks only. If anything below conflicts with the docs, the docs win. Tell me what differed.

## Stack
- Python 3.12, managed with uv
- FastAPI, httpx, pydantic v2, SQLAlchemy 2 (async) with Alembic migrations
- PostgreSQL 16. No TimescaleDB, no Redis, no message broker. Postgres is also the job queue.
- APScheduler in a separate worker process
- Jinja2 + HTMX for the few HTML pages. No JS build step.
- numpy, pandas, statsmodels for analysis
- pytest, ruff, mypy
- Grafana provisioned from files in the repo
- Docker Compose services: api, worker, db, grafana. cloudflared and ntfy as optional profiles.

## Architecture rules
1. Two processes from one image: `api` (FastAPI) and `worker` (scheduler). Only the worker calls the WHOOP API or refreshes tokens.
2. WHOOP refresh tokens rotate and concurrent refreshes fail. Refresh on a schedule in the worker, inside one transaction holding a Postgres advisory lock, and persist the new refresh token in that same transaction. Request the `offline` scope.
3. The webhook handler does exactly this: read raw body bytes, verify `X-WHOOP-Signature` (base64 HMAC-SHA256 of timestamp header + raw body, keyed with the client secret, constant-time compare), reject timestamps older than 5 minutes, insert a row into `webhook_events` with a unique `trace_id`, return 204. No WHOOP API calls in the request path.
4. The worker claims events with SELECT ... FOR UPDATE SKIP LOCKED, fetches the resource, and upserts. Retries with backoff, max attempts, error recorded on the row. Handle `.deleted` events.
5. In v2, a recovery webhook carries the UUID of its sleep. Key recoveries on sleep ID.
6. Cycles have no webhook. Poll them every few hours. The same job re-fetches the trailing 7 days of every collection as reconciliation.
7. Store every fetched record as raw jsonb plus typed columns. All writes are idempotent upserts keyed on the WHOOP ID. Store `score_state` and never analyze unscored rows.
8. Respect X-RateLimit headers and 429s, mainly for the backfill.
9. All timestamps UTC in the database, with WHOOP's timezone offset kept. An experiment day is my local calendar date (America/New_York, configurable). The outcome for day D is the recovery tied to the main sleep ending on the morning of D+1. Naps are excluded. Define this mapping once, in a SQL view called `daily_outcomes`, and have analysis and Grafana both read the view.
10. Config from environment variables via pydantic-settings. Ship `.env.example`. Never commit secrets. Never log tokens or the client secret.
11. Cloudflare Access protects every path except `/webhooks/whoop`. Do not build a login system. Document the Access policy in the README.

## Schema
Plumbing: oauth_token (single row), webhook_events, sync_runs.
WHOOP data: cycles, sleeps, recoveries, workouts. Take column names from the OpenAPI spec.
Experiments: daily_tags, experiments, assignments, experiment_results.
- experiments: name, hypothesis, intervention, control, outcome_metric, analysis_spec (jsonb), block_size, washout_days, planned_days, status, started_on, locked_at. Once locked_at is set, the application refuses any change to analysis_spec, outcome_metric or planned_days.
- assignments: experiment_id, local_date, arm, block_index, seed, complied, note. Unique on (experiment_id, local_date). Only `complied` and `note` may change after creation.
- experiment_results: append-only. experiment_id, computed_at, n_treatment, n_control, effect, ci_low, ci_high, p_value, method.

## Experiment engine
- Permuted-block randomization (default block of 4, two per arm), optional weekday/weekend stratification, seed stored per block so the sequence is reproducible.
- Washout days get no assignment and are excluded from analysis.
- Primary outcome default: natural log of next-morning HRV RMSSD. Secondary: resting HR, sleep efficiency, deep-sleep minutes.
- Primary analysis: intention to treat, difference in means by assigned arm, p-value by randomization inference that re-draws the actual block randomization scheme (10,000 draws, seeded). 95% interval by inverting the test under a constant additive effect.
- Secondary analysis: OLS of outcome on arm, prior 7-day mean of the outcome, and day strain. Also a per-protocol estimate, labeled as biased.
- Missing or unscored mornings are dropped and counted. Flag the experiment if more than 20% are missing.
- A `power` CLI command that takes an outcome and a duration and prints the minimum detectable effect from my baseline variance.
- Evening job: assign tomorrow's arm for the active experiment and send it through ntfy. Morning job: alert if no scored recovery by noon local time.

## Pages (server-rendered, phone-first)
- /oauth/start and /oauth/callback
- /today: tonight's arm, yesterday's compliance toggle, daily tag form. Must be usable in under 20 seconds on a phone.
- /experiments: list, create draft, lock, view results summary
- /healthz

## CLI
`whoop-lab backfill`, `whoop-lab sync`, `whoop-lab power`, `whoop-lab analyze <experiment>`, `whoop-lab simulate`.

## Testing requirements
- Webhook signature: valid, invalid, stale timestamp, replayed trace_id.
- Token refresh: rotation persisted, concurrent refresh attempts do not lose the token.
- Upserts are idempotent. Rescored records overwrite cleanly.
- Day-to-outcome mapping across a DST change and across a night with a nap.
- Randomizer: balance within every block, reproducibility from seed.
- Analysis validated on simulated data: recovers a planted effect, and on null data the false-positive rate is near 5% over many simulated experiments. Include autocorrelated noise in the simulator.
- WHOOP API mocked with respx. No test hits the network.

## How to work
1. Start in plan mode. Read the WHOOP docs, then give me a plan: repo layout, schema, and anything in this prompt you would change, with reasons. Wait for my approval.
2. Build phases 0 to 2 only: scaffold and Compose, auth, ingestion. Stop there and give me exact steps to register the WHOOP app, set the redirect and webhook URLs, and run the OAuth flow.
3. After I confirm data is flowing, continue with: Grafana and data health, tags, experiments, analysis.
4. Small commits, one concern each, conventional commit messages. Run ruff, mypy and pytest before every commit.
5. No AI attribution in commits or PRs: no Co-Authored-By lines, no session links, no generated-with trailers.
6. Write CLAUDE.md with the commands, the architecture rules above and the conventions you settle on. Write a README covering setup, the Cloudflare Tunnel and Access configuration, backup and restore.
7. If a requirement is ambiguous or the WHOOP docs contradict it, ask me. Do not guess.

## Not in scope
Multi-user support, a mobile app, a clone of WHOOP's dashboards, any use of unofficial or private WHOOP endpoints.

My details: GitHub user <github-username>. Server is Linux with Docker and an existing cloudflared tunnel.
```

## Recommended Claude Code setup

Set up five things before the first prompt: attribution off, a permission allowlist, two MCP servers, a format-and-lint hook, and one project skill. Everything else can wait until it annoys you.

**Settings (`.claude/settings.json` in the repo)**

```json
{
  "attribution": { "commit": "", "pr": "" },
  "permissions": {
    "allow": [
      "Bash(uv run pytest:*)",
      "Bash(uv run ruff:*)",
      "Bash(uv run mypy:*)",
      "Bash(uv run alembic:*)",
      "Bash(docker compose:*)",
      "Bash(git status)",
      "Bash(git diff:*)",
      "Bash(git add:*)",
      "Bash(git commit:*)",
      "WebFetch(domain:developer.whoop.com)"
    ],
    "deny": [
      "Read(./.env)",
      "Bash(git push:*)"
    ]
  }
}
```

- Empty attribution strings remove the Co-Authored-By trailer and the PR footer, per the [settings docs](https://code.claude.com/docs/en/settings). This matches your no-AI-attribution rule at the tool level, so it does not depend on the model remembering.
- Denying reads of `.env` keeps the WHOOP client secret and tokens out of the context window. Claude works from `.env.example`.
- Denying `git push` keeps pushes manual. Drop it once you trust the loop.
- Check the exact permission rule syntax against the current docs. It has changed across versions.

**MCP servers**

| Server | Use | When |
| --- | --- | --- |
| Postgres (read-only role) | Lets Claude inspect real schema and rows while debugging ingestion and writing the `daily_outcomes` view | After phase 2, pointed at the dev database |
| Context7 or similar docs server | Current docs for FastAPI, SQLAlchemy 2 async, Alembic, APScheduler | From the start |
| Grafana MCP | Reading and editing dashboards directly | Optional. Provisioned JSON files in the repo are usually enough. |

Give the Postgres server a read-only database role. Your recovery and sleep history is health data, and the session can see whatever that role can.

**Hook**

A `PostToolUse` hook on `Edit|Write` that runs `uv run ruff format` and `uv run ruff check --fix` on the changed file. Formatting stops being something the model spends turns on.

**Skills and commands**

- **A project skill, `whoop-api`.** A short SKILL.md in `.claude/skills/` holding the verified API facts from this doc plus the trimmed OpenAPI spec. It loads only when Claude touches the client, so the facts are not re-fetched or misremembered each session.
- **A project skill, `experiment-analysis`.** The analysis rules: intention to treat, randomization inference that mirrors the block scheme, no edits to locked specs, validate on simulated data first. This is where a well-meaning refactor is most likely to break correctness without any test going red.
- **A `/phase` command.** A custom slash command that takes a phase number, restates that phase's acceptance criteria from this doc and works only on it.

**Working habits**

- **Plan mode first** for each phase. Review the plan, then let it build.
- **`CLAUDE.md` stays short.** Commands, the eleven architecture rules, naming conventions. The prompt already asks Claude to write it.
- **A review subagent for phase 6.** Ask for an independent subagent to review the statistics code and the simulation tests without seeing the implementation discussion. The author should not grade its own analysis.
- **One phase per session.** Start a fresh session per phase and point it at `CLAUDE.md` and this doc. Context stays small and decisions stay written down.
- **Commit this doc** to the repo as `docs/design.md` (export as Markdown), so every session can read it.

## Sources

- [WHOOP: OAuth 2.0](https://developer.whoop.com/docs/developing/oauth)
- [WHOOP: Webhooks](https://developer.whoop.com/docs/developing/webhooks/)
- [WHOOP: API rate limiting](https://developer.whoop.com/docs/developing/rate-limiting) (read from search excerpt, page not opened)
- [Terra: WHOOP API data access, permissions and limitations](https://tryterra.co/blog/whoop-api-data-access-permissions-limitations-2026) (search excerpt)
- [Claude Code settings](https://code.claude.com/docs/en/settings) (search excerpt)
