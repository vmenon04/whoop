from __future__ import annotations

import base64
import hashlib
import hmac

from whoop_lab.whoop.signature import sign, verify_whoop_signature

SECRET = "test-client-secret"
BODY = b'{"user_id":10129,"id":"abc","type":"sleep.updated","trace_id":"t-1"}'
NOW = 1_800_000_000_000


def test_sign_matches_the_documented_recipe() -> None:
    timestamp = str(NOW)
    expected = base64.b64encode(
        hmac.new(SECRET.encode(), timestamp.encode() + BODY, hashlib.sha256).digest()
    ).decode()
    assert sign(BODY, timestamp, SECRET) == expected


def test_valid_signature() -> None:
    ts = str(NOW)
    assert verify_whoop_signature(BODY, ts, sign(BODY, ts, SECRET), SECRET, now_ms=NOW)


def test_wrong_secret() -> None:
    ts = str(NOW)
    assert not verify_whoop_signature(BODY, ts, sign(BODY, ts, "other"), SECRET, now_ms=NOW)


def test_tampered_body() -> None:
    ts = str(NOW)
    signature = sign(BODY, ts, SECRET)
    assert not verify_whoop_signature(BODY + b" ", ts, signature, SECRET, now_ms=NOW)


def test_timestamp_is_part_of_the_signed_message() -> None:
    signature = sign(BODY, str(NOW), SECRET)
    assert not verify_whoop_signature(BODY, str(NOW + 1), signature, SECRET, now_ms=NOW)


def test_stale_timestamp() -> None:
    ts = str(NOW - 5 * 60 * 1000 - 1)
    assert not verify_whoop_signature(BODY, ts, sign(BODY, ts, SECRET), SECRET, now_ms=NOW)


def test_timestamp_just_inside_the_window() -> None:
    ts = str(NOW - 5 * 60 * 1000 + 1000)
    assert verify_whoop_signature(BODY, ts, sign(BODY, ts, SECRET), SECRET, now_ms=NOW)


def test_far_future_timestamp() -> None:
    ts = str(NOW + 6 * 60 * 1000)
    assert not verify_whoop_signature(BODY, ts, sign(BODY, ts, SECRET), SECRET, now_ms=NOW)


def test_non_numeric_timestamp() -> None:
    assert not verify_whoop_signature(BODY, "yesterday", "x", SECRET, now_ms=NOW)


def test_non_ascii_signature_is_rejected_not_raised() -> None:
    assert not verify_whoop_signature(BODY, str(NOW), "sígnature", SECRET, now_ms=NOW)
