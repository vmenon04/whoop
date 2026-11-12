from __future__ import annotations

import json
import time
from typing import Any

import httpx
import respx
from sqlalchemy import select

from tests import payloads
from whoop_lab.db import SessionFactory
from whoop_lab.models import WebhookEvent
from whoop_lab.whoop.signature import sign

SECRET = "test-client-secret"


def event(trace_id: str = "trace-1", **overrides: Any) -> dict[str, Any]:
    body = {
        "user_id": payloads.USER_ID,
        "id": payloads.SLEEP_ID,
        "type": "sleep.updated",
        "trace_id": trace_id,
    }
    body.update(overrides)
    return body


def signed(body: bytes, *, age_ms: int = 0, secret: str = SECRET) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000) - age_ms)
    return {
        "X-WHOOP-Signature": sign(body, timestamp, secret),
        "X-WHOOP-Signature-Timestamp": timestamp,
        "Content-Type": "application/json",
    }


async def stored(session_factory: SessionFactory) -> list[WebhookEvent]:
    async with session_factory() as session:
        return list((await session.execute(select(WebhookEvent))).scalars())


async def test_valid_event_is_stored_and_acknowledged(
    api: httpx.AsyncClient, session_factory: SessionFactory, whoop_mock: respx.MockRouter
) -> None:
    body = json.dumps(event()).encode()
    response = await api.post("/webhooks/whoop", content=body, headers=signed(body))

    assert response.status_code == 204
    assert response.content == b""
    rows = await stored(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert (row.trace_id, row.type, row.resource_id) == (
        "trace-1",
        "sleep.updated",
        payloads.SLEEP_ID,
    )
    assert (row.status, row.attempts, row.user_id) == ("pending", 0, payloads.USER_ID)
    assert row.payload == event()
    assert whoop_mock.calls.call_count == 0  # nothing leaves the process in the request path


async def test_signature_is_checked_against_raw_bytes(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    # Odd whitespace and key order: re-serialising this JSON would change the bytes.
    body = (
        b'{ "trace_id" : "trace-ws",\n  "type":"workout.updated", "id":"'
        + payloads.WORKOUT_ID.encode()
        + b'" ,"user_id":10129 }'
    )
    response = await api.post("/webhooks/whoop", content=body, headers=signed(body))
    assert response.status_code == 204
    assert [row.trace_id for row in await stored(session_factory)] == ["trace-ws"]


async def test_invalid_signature_is_rejected_and_nothing_stored(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    body = json.dumps(event()).encode()
    response = await api.post(
        "/webhooks/whoop", content=body, headers=signed(body, secret="wrong-secret")
    )
    assert response.status_code == 401
    assert await stored(session_factory) == []


async def test_tampered_body_is_rejected(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    body = json.dumps(event()).encode()
    headers = signed(body)
    tampered = json.dumps(event(type="sleep.deleted")).encode()
    response = await api.post("/webhooks/whoop", content=tampered, headers=headers)
    assert response.status_code == 401
    assert await stored(session_factory) == []


async def test_missing_headers_are_rejected(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    body = json.dumps(event()).encode()
    assert (await api.post("/webhooks/whoop", content=body)).status_code == 401
    headers = signed(body)
    del headers["X-WHOOP-Signature-Timestamp"]
    assert (await api.post("/webhooks/whoop", content=body, headers=headers)).status_code == 401
    assert await stored(session_factory) == []


async def test_stale_timestamp_is_rejected(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    body = json.dumps(event()).encode()
    response = await api.post(
        "/webhooks/whoop", content=body, headers=signed(body, age_ms=5 * 60 * 1000 + 2000)
    )
    assert response.status_code == 401
    assert await stored(session_factory) == []


async def test_replayed_trace_id_is_acknowledged_but_stored_once(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    body = json.dumps(event("trace-dup")).encode()
    first = await api.post("/webhooks/whoop", content=body, headers=signed(body))
    again = await api.post("/webhooks/whoop", content=body, headers=signed(body))
    assert (first.status_code, again.status_code) == (204, 204)
    assert len(await stored(session_factory)) == 1


async def test_v1_style_integer_id_is_stored_as_text(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    body = json.dumps(event(id=10235)).encode()
    assert (
        await api.post("/webhooks/whoop", content=body, headers=signed(body))
    ).status_code == 204
    assert (await stored(session_factory))[0].resource_id == "10235"


async def test_signed_but_malformed_body_is_400(
    api: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    body = b'{"hello": "world"}'
    response = await api.post("/webhooks/whoop", content=body, headers=signed(body))
    assert response.status_code == 400
    assert await stored(session_factory) == []


async def test_oversized_body_is_rejected(api: httpx.AsyncClient) -> None:
    body = b"x" * (64 * 1024 + 1)
    response = await api.post("/webhooks/whoop", content=body, headers=signed(body))
    assert response.status_code == 413
