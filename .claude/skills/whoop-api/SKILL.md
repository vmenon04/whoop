---
name: whoop-api
description: Verified facts and hard rules for the WHOOP v2 developer API. Use when writing or changing any code that touches WHOOP - OAuth, token refresh, the HTTP client, webhooks, backfill, sync or reconciliation - or the tables those write to.
---

# WHOOP API

Facts below marked VERIFIED were checked against developer.whoop.com on 2026-11-11.
Facts marked UNVERIFIED are from memory. Confirm them in the OpenAPI spec before relying on them, then update this file.

## First time in this repo

1. Fetch the OpenAPI spec linked from https://developer.whoop.com/api.
2. Save it to `.claude/skills/whoop-api/reference/openapi.json`.
3. Resolve every UNVERIFIED item below against it and edit this file.
4. Generate pydantic models from the spec. Do not hand-type field names.

After that, read `reference/openapi.json` instead of fetching the docs again.

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
- Cycles, day strain and body measurements have no webhook (reported by third parties, treat as true until the docs say otherwise). Poll them.

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

## Endpoints and fields (UNVERIFIED)

- Base URL: `https://api.prod.whoop.com/developer/v2`
- Collections: cycle, recovery, activity/sleep, activity/workout. Profile and body measurement under user/.
- Collection parameters: `limit` (max 25), `start`, `end`, `nextToken`. Responses carry `records` and `next_token`.
- Score objects carry `score_state`: `SCORED`, `PENDING_SCORE` or `UNSCORABLE`.
- Recovery score: `recovery_score`, `hrv_rmssd_milli`, `resting_heart_rate`, `spo2_percentage`, `skin_temp_celsius`, `user_calibrating`.
- Sleep has a `nap` boolean and a stage summary in milliseconds.
- Timestamps are ISO 8601 UTC with a separate `timezone_offset`.

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

- No unofficial, private or app-internal WHOOP endpoints.
- No v1 endpoints or v1 webhook model.
