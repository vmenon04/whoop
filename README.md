# whoop-lab

A self-hosted service that keeps a complete copy of your WHOOP data in Postgres and runs randomized n-of-1 experiments on it. One user, four containers, one Cloudflare Tunnel hostname.

## How it fits together

| Service | What it does |
| --- | --- |
| `api` | `/today`, `/experiments`, status page, OAuth start/callback, webhook receiver, `/healthz`, `/uptime`. Never calls WHOOP except for the one-time code exchange. |
| `worker` | Refreshes the token, processes webhook events, polls cycles and reconciles the last 7 days every 3 hours. Draws tomorrow's arm each evening, alerts on a missing morning recovery at noon, and appends results at 13:00. |
| `db` | Postgres 16. All state, and the job queue. |
| `migrate` | Runs `alembic upgrade head` and `whoop-lab setup-db` (timezone, Grafana's read-only role) on every `up`, then exits. |
| `grafana` | Four dashboards provisioned from `grafana/`: data health, experiment results, nightly health and day view. Served under `/grafana`, read-only database role, viewable without a second login. |

`api` and `grafana` are published on `127.0.0.1` only (ports 8000 and 3001 by default; `API_PORT` and `GRAFANA_PORT` in `.env` change them). The tunnel is the only way in.

## Setup

Requirements on the server: Docker with Compose, and a Cloudflare Tunnel.

```sh
git clone https://github.com/vmenon04/whoop-lab.git && cd whoop-lab
cp .env.example .env        # then edit: the three passwords now, WHOOP credentials after step 3
chmod 600 .env
```

### 1. Cloudflare Tunnel

Route `whoop.vasumenon.com` to this stack. Grafana lives on the same hostname under `/grafana`, so the path rule must come first.

Locally managed tunnel (`config.yml`):

```yaml
ingress:
  - hostname: whoop.vasumenon.com
    path: ^/grafana(/.*)?$
    service: http://localhost:3001
  - hostname: whoop.vasumenon.com
    service: http://localhost:8000
  - service: http_status:404
```

Then `cloudflared tunnel route dns <tunnel-name> whoop.vasumenon.com` and restart cloudflared.

Dashboard-managed tunnel: add two public hostnames for `whoop.vasumenon.com`, the first with path `^/grafana(/.*)?$` to `http://localhost:3001`, the second with no path to `http://localhost:8000`.

If you would rather have this stack run its own connector, set `CLOUDFLARE_TUNNEL_TOKEN` and start with `--profile cloudflared`; the services are then `http://api:8000` and `http://grafana:3000`.

### 2. Cloudflare Access

The app has no login. Access is the login, and it must be in place before the stack is reachable.

In Zero Trust, under Access, create two self-hosted applications:

| Application | Domain / path | Policy |
| --- | --- | --- |
| whoop-lab | `whoop.vasumenon.com` | Action **Allow**, Include **Emails**: your address |
| whoop-lab webhook | `whoop.vasumenon.com/webhooks/whoop` | Action **Bypass**, Include **Everyone** |
| whoop-lab privacy | `whoop.vasumenon.com/privacy` | Action **Bypass**, Include **Everyone** |

Access applies the most specific path, so the bypasses cover only those two paths. The webhook is protected by the HMAC signature check instead. `/privacy` is a static page with no data behind it; WHOOP's app registration asks for a public privacy policy URL. Everything else, including `/oauth/*`, `/healthz` and `/grafana`, asks for login.

Check it:

```sh
curl -sI https://whoop.vasumenon.com/healthz | head -1                      # 302 to the Access login
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://whoop.vasumenon.com/webhooks/whoop   # 401 from the app
```

### 3. WHOOP developer app

1. Sign in at <https://developer-dashboard.whoop.com> and create an app.
2. Scopes: `read:recovery`, `read:cycles`, `read:sleep`, `read:workout`, `read:profile`, `read:body_measurement`. (`offline` is requested by the app at sign-in.)
3. Redirect URL: `https://whoop.vasumenon.com/oauth/callback` (must match exactly). Privacy policy URL: `https://whoop.vasumenon.com/privacy`.
4. Webhook URL: `https://whoop.vasumenon.com/webhooks/whoop`, model version **v2**.
5. Copy the client ID and client secret into `.env` as `WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET`.

### 4. Start and connect

```sh
docker compose up -d --build
docker compose ps                         # migrate should show "exited (0)"
```

Open <https://whoop.vasumenon.com/>, pass the Access login, press **Connect WHOOP** and approve. Then load your history:

```sh
docker compose exec worker whoop-lab backfill
```

The status page shows row counts, the last webhook and recent syncs.

## Running an experiment

1. **Baseline first.** Collect three to four weeks of data with nothing running. Then size the experiment from your own variability:
   ```sh
   docker compose exec worker whoop-lab power --days 28
   ```
   It prints the smallest effect a design of that length can reliably detect.
2. **Draft.** On `/experiments`, write the hypothesis, what a treatment day and a control day mean, the primary outcome and the number of assigned days.
3. **Lock.** Locking freezes the whole plan, including the analysis and its random seed. The first assigned day is the next day.
4. **Run.** Each evening after 20:00 (`ASSIGN_HOUR`) the worker draws tomorrow's arm and sends it through ntfy. `/today` shows today's arm, asks whether you followed yesterday's, and has the tag buttons.
5. **Read.** Results are appended every day at 13:00 and shown on the experiment's page and in Grafana. The experiment ends when its planned days are used up, whatever the numbers show.

How it is analysed:

- **Outcome mapping:** the outcome for day D is the recovery from the main sleep ending on the morning of D+1 (before 14:00), in `LOCAL_TIMEZONE`. A sleep that ends later in the day is never the main sleep. Naps, unscored and deleted records never count. This is defined once, in the `daily_outcomes` view.
- **Primary result:** intention to treat. Days are compared by the arm they were assigned, whether or not you complied. The p-value and the 95% interval come from re-drawing the block randomization 10,000 times.
- **Secondary results, labelled as such:** a covariate-adjusted model, a per-protocol estimate (biased, because compliance is not randomized), and the other outcomes (exploratory).
- **Missing mornings** are dropped and counted. More than 20% missing, or a gap of more than 3 between arms, marks the result as compromised.

`LOCAL_TIMEZONE` cannot change while an experiment is running; the stack refuses to start if it does.

Before trusting any change to the analysis code, run the validation (about 10 seconds):

```sh
docker compose exec worker whoop-lab simulate
```

## Operating it

| Task | Command |
| --- | --- |
| Logs | `docker compose logs -f worker api` |
| Reconcile now | `docker compose exec worker whoop-lab sync` |
| Recompute results now | `docker compose exec worker whoop-lab analyze "<experiment name>"` |
| Backfill from a date | `docker compose exec worker whoop-lab backfill --since 2025-01-01` |
| Update | `git pull && docker compose up -d --build` |
| Queue state | `docker compose exec db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB" -c "select status, count(*) from webhook_events group by 1"'` |

**If WHOOP rejects a token refresh** the worker stops calling WHOOP, the status page shows **Reconnect WHOOP**, and if `NTFY_URL` is set you get one notification. Sign in again from the status page. Missed data is picked up by the next reconcile (it looks back 7 days; for a longer outage run `whoop-lab backfill --since <date>`).

**Uptime monitoring.** `GET /uptime` answers 200 with `{"status": "ok"}` only while the database answers, the WHOOP token is usable and the worker has finished a sync without error in the last two reconcile intervals (6.5 hours by default). Otherwise it answers 503 and names the failing check (`database`, `token` or `sync`). It returns no data. `/healthz` is the lighter check: the api process and the database only.

- A monitor on the server or LAN (Uptime Kuma, for example) can call `http://127.0.0.1:8000/uptime` directly.
- A monitor outside goes through Cloudflare Access. Create a service token in Zero Trust, add a **Service Auth** policy for it on an application scoped to `whoop.vasumenon.com/uptime`, and have the monitor send the `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers. Prefer this to a Bypass policy: the endpoint reads the database, and a bypass would also tell anyone whether the service is healthy.

**Notifications** are optional. Set `NTFY_URL` to a full topic URL, either `https://ntfy.sh/<long-random-topic>` or a self-hosted ntfy (`--profile ntfy` starts one on `127.0.0.1:8080`).

## Band data in Grafana

Nightly metrics computed from the band's own Bluetooth records (see the `whoop-ble` skill) go to the `ble_nights` table and the **Nightly health** dashboard, beside WHOOP's values for the same nights. Per-minute summaries of the whole day (heart rate, state, activity, steps, movement, skin temperature) go to `ble_minutes` and the **Day view** dashboard. After draining the band on the Mac:

```sh
./scripts/push_ble.sh          # rsync captures/ to the server, then ble-import the last 3 days
./scripts/push_ble.sh --all    # recompute every night, e.g. after a metrics version change
```

Run it in a normal terminal: the import uses `sudo docker` on the server, which asks for a password. Captures are copied to `captures/` next to the server checkout (git-ignored, readable by the worker container) and mounted read-only into the worker. Create that directory as your own user before the first `docker compose up`, or Docker creates it owned by root and rsync cannot write to it. Rows are keyed by night and metrics version; the dashboard shows the current version, so regenerate it (`scripts/gen_dashboards.py`) after a version change.

## Backup and restore

WHOOP data can be re-downloaded. Tags, experiment assignments and results (once built) exist only here, so back up the database.

```sh
./scripts/backup.sh                       # writes backups/whoop_lab-<utc time>.dump, keeps 30
```

Nightly from cron, copying off the machine or to a second disk:

```cron
15 3 * * * cd /path/to/whoop-lab && ./scripts/backup.sh && rsync -a backups/ /mnt/backup/whoop-lab/
```

The dump contains the WHOOP access and refresh tokens. Treat it like `.env`.

Restore into a fresh stack:

```sh
docker compose up -d db
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < backups/whoop_lab-<time>.dump
docker compose up -d
```

If the restored token is older than the last refresh it is dead (refresh tokens rotate). The status page will ask you to reconnect; nothing else is lost.

## Development

```sh
uv sync
uv run ruff check . && uv run mypy && uv run pytest     # Docker must be running
```

Tests start a throwaway Postgres 16 with testcontainers and mock every WHOOP call. No test touches the network. See `CLAUDE.md` for the architecture rules and conventions.

This is self-experimentation tooling, not medical advice.
