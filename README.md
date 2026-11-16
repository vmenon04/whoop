# whoop

Keep your own copy of your WHOOP data, look at it in Grafana, and run randomized experiments on yourself, such as "does a noon caffeine cutoff raise my HRV?".

- **Archive.** Recovery, sleep, cycles and workouts from the official WHOOP API, kept in Postgres with the full raw payloads. Webhooks bring new data in within a minute, and a reconcile job re-checks the last week every three hours.
- **Dashboards.** Grafana dashboards for daily health, data quality and experiment results.
- **Experiments.** Pre-registered n-of-1 experiments: each day is randomly assigned to treatment or control, and the result is analysed with a randomization test. See [docs/experiments.md](docs/experiments.md).
- **Band data (experimental).** Reads a WHOOP 5.0 band directly over Bluetooth and computes nightly HRV, resting heart rate, respiratory rate and per-minute activity with published methods. See [docs/band.md](docs/band.md).

It is a single-user app with no login of its own: run it on your own computer, or put it online behind a login such as Cloudflare Access.

Not affiliated with or endorsed by WHOOP. Not medical advice.

## Quick start (on your own computer)

You need Docker with Compose and a WHOOP membership.

1. **Get the code and settings.**
   ```sh
   git clone https://github.com/vmenon04/whoop.git && cd whoop
   cp .env.example .env
   ```
   In `.env`, set the three passwords to long random strings (`openssl rand -hex 24`).

2. **Register a WHOOP developer app** at <https://developer-dashboard.whoop.com>.
   - Scopes: `read:recovery`, `read:cycles`, `read:sleep`, `read:workout`, `read:profile`, `read:body_measurement`.
   - Redirect URL: `http://localhost:8000/oauth/callback`.
   - Privacy policy URL: WHOOP asks for one. The app serves a short policy at `/privacy` once it is online. Until then, any page you control will do.
   - Leave the webhook URL empty for now; the reconcile job polls instead.

   Put the client ID and secret in `.env`. If the dashboard insists on an HTTPS redirect URL, go straight to [Putting it online](#putting-it-online).

3. **Start it.**
   ```sh
   docker compose up -d --build
   ```

4. **Connect WHOOP.** Open <http://localhost:8000/status> (use `localhost`, not `127.0.0.1`), press **Connect WHOOP** and approve. Then load your history:
   ```sh
   docker compose exec worker whoop-lab backfill
   ```

5. **Look at it.** <http://localhost:8000> opens the Health dashboard in Grafana. `/status` shows what has been loaded, `/today` holds the daily tags and today's experiment arm, and `/experiments` is where experiments are set up.

## Putting it online

Online, WHOOP can send webhooks and you can use the app from your phone. The app has no login, so put one in front of it. These steps use a Cloudflare Tunnel with Cloudflare Access, which needs no open router ports.

1. **Tunnel.** Route a hostname, say `whoop.example.com`, to this machine, with `/grafana` going to Grafana and everything else to the app:
   ```yaml
   ingress:
     - hostname: whoop.example.com
       path: ^/grafana(/.*)?$
       service: http://localhost:3001
     - hostname: whoop.example.com
       service: http://localhost:8000
     - service: http_status:404
   ```
   To run the connector inside this stack instead, set `CLOUDFLARE_TUNNEL_TOKEN` and start with `--profile cloudflared`. The services are then `http://api:8000` and `http://grafana:3000`.

2. **Access.** In Cloudflare Zero Trust, create three self-hosted applications:

   | Domain / path | Policy |
   | --- | --- |
   | `whoop.example.com` | Allow, your email address only |
   | `whoop.example.com/webhooks/whoop` | Bypass, everyone. The app checks WHOOP's signature on every request. |
   | `whoop.example.com/privacy` | Bypass, everyone. A static page with no data behind it. |

3. **Settings.** In `.env`, set `PUBLIC_BASE_URL=https://whoop.example.com` and delete the `GRAFANA_URL` line. Then run `docker compose up -d`.

4. **WHOOP app.** Change the redirect URL to `https://whoop.example.com/oauth/callback`, the privacy URL to `https://whoop.example.com/privacy`, and set the webhook URL to `https://whoop.example.com/webhooks/whoop` (model version v2). Reconnect from `/status`.

Check it: `curl -sI https://whoop.example.com/status` should redirect to the Access login. A plain `curl -X POST https://whoop.example.com/webhooks/whoop` should get a 401 from the app.

## Everyday use

| Task | Command |
| --- | --- |
| Logs | `docker compose logs -f worker api` |
| Sync the last week now | `docker compose exec worker whoop-lab sync` |
| Load history from a date | `docker compose exec worker whoop-lab backfill --since 2025-01-01` |
| Update | `git pull && docker compose up -d --build` (migrations run on start) |
| Back up | `./scripts/backup.sh` (writes `backups/`, keeps 30) |

- **Notifications** (optional): set `NTFY_URL` to an [ntfy](https://ntfy.sh) topic URL. You get tonight's experiment arm, a warning if no recovery is scored by noon, and a message if WHOOP sign-in is needed again.
- **If WHOOP sign-in expires,** `/status` shows **Reconnect WHOOP**. Data from the gap is picked up afterwards, as long as the gap is under a week; for longer ones run `backfill --since`.
- **Uptime monitoring:** `GET /uptime` answers 200 only while the database, the WHOOP token and the last sync are all healthy. Otherwise it answers 503 and names the failing check.

**Backups.** WHOOP data can be downloaded again, but your tags and experiments exist only here. Run `./scripts/backup.sh` from cron and copy `backups/` somewhere else. The dumps contain your WHOOP tokens, so treat them like `.env`. To restore:
```sh
docker compose up -d db
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < backups/<file>.dump
docker compose up -d
```
Then reconnect WHOOP from `/status` if asked: refresh tokens rotate, so a restored token may no longer work.

## How it works

Five containers from one Compose file:
- **`db`:** Postgres 16. It holds all state and also serves as the job queue.
- **`migrate`:** applies migrations on every start, then exits.
- **`api`:** the pages, the OAuth flow and the webhook receiver.
- **`worker`:** the only thing that calls WHOOP.
- **`grafana`:** reads the database through a read-only role.

[docs/architecture.md](docs/architecture.md) has the details.

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

Tests need Docker: they start a throwaway Postgres, and they mock every WHOOP call, so no test touches the network. Before changing analysis code, run `uv run whoop-lab simulate`, which checks the statistics on simulated experiments. Dashboards are generated by `scripts/gen_dashboards.py`: edit the script, not the JSON. `CLAUDE.md` lists the architecture rules and conventions.

CI runs on every push. A version tag (`v0.2.0`, matching `pyproject.toml`) publishes `ghcr.io/vmenon04/whoop` and `ghcr.io/vmenon04/whoop-grafana`, with the dashboards built in, for running from images instead of a checkout.

## License

MIT. The Bluetooth decoder under `src/whoop_lab/ble/vendor/whoop5` comes from [LeonardSEO/wearable5.0](https://github.com/LeonardSEO/wearable5.0) (MIT). WHOOP is a trademark of WHOOP, Inc.
