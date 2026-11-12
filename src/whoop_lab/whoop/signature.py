"""WHOOP webhook signature check.

signature = base64( HMAC-SHA256( key = client secret,
                                 message = timestamp header value + raw body ) )
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

MAX_SKEW_MS = 5 * 60 * 1000


def sign(raw_body: bytes, timestamp: str, client_secret: str) -> str:
    digest = hmac.new(client_secret.encode(), timestamp.encode() + raw_body, hashlib.sha256)
    return base64.b64encode(digest.digest()).decode()


def verify_whoop_signature(
    raw_body: bytes,
    timestamp: str,
    signature: str,
    client_secret: str,
    *,
    max_skew_ms: int = MAX_SKEW_MS,
    now_ms: int | None = None,
) -> bool:
    """True only for a correctly signed request whose timestamp is within the allowed skew."""
    try:
        sent_ms = int(timestamp)
    except ValueError:
        return False
    current = int(time.time() * 1000) if now_ms is None else now_ms
    if abs(current - sent_ms) > max_skew_ms:
        return False
    expected = sign(raw_body, timestamp, client_secret)
    return hmac.compare_digest(expected.encode(), signature.encode())
