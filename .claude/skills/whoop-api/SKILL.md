---
name: whoop-api
description: Verified facts and hard rules for the WHOOP v2 developer API. Use when writing or changing any code that touches WHOOP - OAuth, token refresh, the HTTP client, webhooks, backfill, sync or reconciliation - or the tables those write to.
---

# WHOOP API

Facts below marked VERIFIED were checked against developer.whoop.com on 2026-11-11.
Facts marked UNVERIFIED are from memory. Confirm them in the OpenAPI spec before relying on them, then update this file.

## Spec

The OpenAPI spec is saved at `reference/openapi.json` (fetched 2026-11-11 from `https://api.prod.whoop.com/developer/doc/openapi.json`). Read it instead of fetching the docs again. Pydantic models are generated from it into `src/whoop_lab/whoop/generated.py` with `./scripts/gen_models.sh`. Do not hand-type field names.

To refresh: download the spec again, replace the file, run the script, then fix whatever the tests flag.

## OAuth (VERIFIED)

- Authorization URL: `https://api.prod.whoop.com/oauth/oauth2/auth`
- Token URL: `https://api.prod.whoop.com/oauth/oauth2/token`
- The redirect URL must be registered in the WHOOP Developer Dashboard and match exactly.
- A self-generated `state` must be exactly 8 characters. Verify it on callback.
- Request the `offline` scope or no refresh token is issued.
- Access tokens are short-lived. Use `expires_in` from the response (sample value 3600). Never hardcode an hour.
- Refresh: POST to the token URL with `grant_type=refresh_token`, `refresh_token`, `client_id`, `client_secret`, `scope=offline`.
- **Refresh tokens rotate.** Each refresh returns a new refresh token and invalidates the old access token. If two refreshes race, only the first succeeds and the other one's token is dead.

Scopes to request: `offline read:recovery read:cycles read:sleep read:workout read:profile read:body_measurement`

### Token rules for this codebase

- Only the worker refreshes. The api process never calls the token URL after the initial code exchange.
- Refresh runs on a schedule, well before expiry, not lazily on a 401.
- The refresh happens inside one transaction that holds a Postgres advisory lock. The new access and refresh tokens are written in that same transaction before it commits.
- If the refresh response is lost after WHOOP processed it, the token is gone. Log loudly, alert through ntfy, and require a manual re-auth. Do not retry with the old refresh token in a loop.
- Never log tokens, the client secret or the Authorization header.

## Webhooks (VERIFIED)

- Events: `workout.updated`, `workout.deleted`, `sleep.updated`, `sleep.deleted`, `recovery.updated`, `recovery.deleted`.
- Body fields: `user_id`, `id`, `type`, `trace_id`.
- Use v2 webhooks only. In v2, `id` is a UUID string.
- **A v2 recovery event's `id` is the UUID of the associated sleep**, not a cycle ID. Recoveries are keyed on sleep ID everywhere in this codebase.
- A webhook is a notification. It carries no data. Fetch the resource afterward.
- There is no "created" event. New records arrive as `.updated`, and the same record is updated again when rescored.
- WHOOP retries a failed delivery 5 times over about an hour. Failure means non-2XX or a timeout.
- Respond 2XX within about one second.
- Cycles, day strain and body measurements have no webhook (stated on WHOOP's webhook page). Poll them.
- WHOOP's own docs say webhooks can be missed and recommend a reconciliation job.

### Signature

- Headers: `X-WHOOP-Signature` and `X-WHOOP-Signature-Timestamp` (milliseconds since epoch).
- Signature = base64( HMAC-SHA256( key = client secret, message = timestamp header value + raw request body ) ).

```python
import base64, hashlib, hmac, time

MAX_SKEW_MS = 5 * 60 * 1000

def verify_whoop_signature(raw_body: bytes, timestamp: str, signature: str, client_secret: str) -> bool:
    try:
        skew = abs(int(time.time() * 1000) - int(timestamp))
    except ValueError:
        return False
    if skew > MAX_SKEW_MS:
        return False
    digest = hmac.new(client_secret.encode(), timestamp.encode() + raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)
```

### Webhook handler rules

The handler does exactly four things, in this order:

1. Read the raw body bytes. Verify against those bytes, never against re-serialized JSON.
2. Verify the signature and timestamp. On failure return 401 and store nothing.
3. Insert into `webhook_events` with `ON CONFLICT (trace_id) DO NOTHING`.
4. Return 204.

No WHOOP API call, no token access and no business logic in the request path.

## Rate limits (VERIFIED)

- Defaults: 100 requests per minute and 10,000 per day.
- Headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (seconds).
- On 429, sleep for `X-RateLimit-Reset` seconds plus jitter, then retry. The backfill is the only job likely to hit this.

## Endpoints and fields (VERIFIED against the spec)

- Base URL: `https://api.prod.whoop.com/developer`, paths under `/v2`.
- Collections: `/v2/cycle`, `/v2/recovery`, `/v2/activity/sleep`, `/v2/activity/workout`. Single records: `/v2/cycle/{cycleId}`, `/v2/activity/sleep/{sleepId}`, `/v2/activity/workout/{workoutId}`. Profile: `/v2/user/profile/basic`. Body: `/v2/user/measurement/body`. Revoke: `DELETE /v2/user/access`.
- **There is no recovery-by-sleep endpoint.** The only single-recovery call is `GET /v2/cycle/{cycleId}/recovery`. For a recovery webhook: fetch the sleep, read its `cycle_id`, fetch the cycle's recovery.
- Recovery has no ID of its own: `cycle_id`, `sleep_id` (UUID), `user_id`, `created_at`, `updated_at`, `score_state`, `score`.
- Collection parameters: `limit` (max 25, default 10), `start` (inclusive), `end` (exclusive, defaults to now), `nextToken`. Responses carry `records` and `next_token`. Sorted by start time descending (recoveries by the start of their sleep).
- `score_state`: `SCORED`, `PENDING_SCORE` or `UNSCORABLE`. The `score` object is absent unless `SCORED`.
- Recovery score: `user_calibrating`, `recovery_score`, `resting_heart_rate`, `hrv_rmssd_milli` are required when scored; `spo2_percentage` and `skin_temp_celsius` are optional (4.0 hardware and later).
- Sleep: `nap` boolean, `cycle_id`, `score.stage_summary` (durations in ms), `score.sleep_needed`, and four optional percentages/rates.
- Cycle: `end` is absent while the cycle is open. `step_count` is nullable.
- Workout: `sport_name`. `sport_id` and every `v1_id` are deprecated ("will not exist past 09/01/2025"); do not add columns for them.
- Timestamps are ISO 8601 with a separate `timezone_offset` string such as `-05:00`.
- `X-RateLimit-Limit` is compound, e.g. `100, 100;window=60, 10000;window=86400`. Use `X-RateLimit-Remaining` and `X-RateLimit-Reset` and do not parse `Limit`.
- The OAuth page does not state how the client authenticates at the token URL; its sample sends `client_id` and `client_secret` in the body. This codebase sends them form-encoded. UNVERIFIED until the first live sign-in succeeds.
- The 5-minute webhook timestamp window is this project's rule. WHOOP documents no tolerance.

## Ingestion rules for this codebase

- Store the full response for every record in a `raw jsonb` column before parsing.
- Every write is an upsert keyed on the WHOOP ID. Replaying any event or sync must change nothing.
- Store `score_state`. A `PENDING_SCORE` row is kept and re-fetched later. Only `SCORED` rows reach analysis.
- Handle `.deleted` events by soft-deleting (a `deleted_at` column), so an assignment's outcome history stays explainable.
- The reconciliation job re-fetches the trailing 7 days of every collection. It is the safety net for missed webhooks, so it must not be removed as redundant.
- Paginate until `next_token` is absent. Never assume one page.
- All WHOOP calls go through one client class with retry, backoff and rate-limit handling. No ad hoc httpx calls elsewhere.
- Tests mock WHOOP with respx. No test touches the network.

## Out of bounds

- No private, unofficial or app-internal WHOOP **cloud** endpoints. This skill covers the official developer API only.
- Talking to the owner's own band over local Bluetooth LE is in scope for the project, but not for this skill: it is governed by the `whoop-ble` skill and its safety rules.
- No v1 endpoints or v1 webhook model.
