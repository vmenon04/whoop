"""WHOOP webhook receiver. The one path that is not behind Cloudflare Access.

The handler does four things and nothing else:
1. read the raw body bytes
2. verify the signature and timestamp against those bytes
3. insert into webhook_events, ignoring a trace_id already seen
4. return 204
No WHOOP call, no token access, no business logic. The worker does the rest.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.dialects.postgresql import insert

from whoop_lab.models import WebhookEvent
from whoop_lab.whoop.signature import verify_whoop_signature

router = APIRouter()
log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024


class WebhookPayload(BaseModel):
    user_id: int
    id: int | str
    type: str
    trace_id: str


async def _read_body(request: Request) -> bytes | None:
    """Raw bytes, or None if the body is larger than any real WHOOP event."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/webhooks/whoop")
async def whoop_webhook(request: Request) -> Response:
    settings = request.app.state.settings
    raw = await _read_body(request)
    if raw is None:
        return Response(status_code=413)

    signature = request.headers.get("X-WHOOP-Signature", "")
    timestamp = request.headers.get("X-WHOOP-Signature-Timestamp", "")
    if not (
        signature
        and timestamp
        and verify_whoop_signature(
            raw,
            timestamp,
            signature,
            settings.whoop_client_secret.get_secret_value(),
            max_skew_ms=settings.webhook_max_skew_seconds * 1000,
        )
    ):
        log.warning("webhook: rejected (bad or missing signature, or stale timestamp)")
        return Response(status_code=401)

    try:
        payload = WebhookPayload.model_validate_json(raw)
    except ValidationError:
        log.warning("webhook: signed request with an unexpected body shape")
        return Response(status_code=400)

    async with request.app.state.session_factory() as session, session.begin():
        await session.execute(
            insert(WebhookEvent)
            .values(
                trace_id=payload.trace_id,
                type=payload.type,
                resource_id=str(payload.id),
                user_id=payload.user_id,
                payload=payload.model_dump(),
            )
            .on_conflict_do_nothing(index_elements=["trace_id"])
        )
    return Response(status_code=204)
