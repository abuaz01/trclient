"""Logging that cannot leak session cookies or the PIN, even at DEBUG level."""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    (re.compile(r"((?:tr_session|tr_refresh|tr_claims|tr_device|tr_external_id|aws-waf-token)=)[^;\s\"']+"), r"\1***"),
    (re.compile(r"((?<![A-Za-z_])\"?(?:pin|code|password)\"?\s*[:=]\s*\"?)[^\"',}\s]+", re.IGNORECASE), r"\1***"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger
