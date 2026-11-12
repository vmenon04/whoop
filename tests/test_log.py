from __future__ import annotations

import logging

import pytest

from whoop_lab.log import RedactingFilter, redact


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("Authorization: Bearer abc.DEF-123", "abc.DEF-123"),
        ("refresh_token=rt-12345&scope=offline", "rt-12345"),
        ('{"access_token": "at-999", "expires_in": 3600}', "at-999"),
        ("client_secret=shhh", "shhh"),
        ("GET /oauth/callback?code=AUTHCODE&state=abcd1234", "AUTHCODE"),
    ],
)
def test_redact_removes_secret(raw: str, secret: str) -> None:
    cleaned = redact(raw)
    assert secret not in cleaned
    assert "[redacted]" in cleaned


def test_redact_keeps_ordinary_text() -> None:
    text = "reconcile finished: 12 rows upserted, score_state=SCORED"
    assert redact(text) == text


def test_filter_scrubs_formatted_args() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token %s", ("Bearer zzz",), None)
    assert RedactingFilter().filter(record)
    assert "zzz" not in record.getMessage()
