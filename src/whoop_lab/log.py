"""Logging setup with a redaction filter.

Tokens, the client secret, authorization codes and Authorization headers must
never reach a log line. Code should not log them in the first place; the filter
is the backstop for third-party loggers and mistakes.
"""

from __future__ import annotations

import logging
import re

_REDACTED = "[redacted]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"), rf"\1 {_REDACTED}"),
    (
        re.compile(
            r"(?i)(\"?\b(?:access_token|refresh_token|client_secret|authorization|code)\b\"?"
            r"\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s&,;}\"']+)"
        ),
        rf"\1{_REDACTED}",
    ),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs one line per request at INFO; keep it, the filter scrubs it.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers[:] = []
        uv_logger.propagate = True
