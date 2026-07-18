"""Redact secrets from text before sending to chat or logs."""
from __future__ import annotations

import re
from typing import Iterable

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?im)(QQBOT_SEND_(?:IMAGE|FILE|VOICE|AUDIO)\s*:\s*)(\S+)"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)(api[_-]?key|token|secret|password|passwd|auth)"
            r"[\"'\s:=]+([A-Za-z0-9_\-]{8,})"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)sk-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    (re.compile(r"(?i)ghp_[A-Za-z0-9]{30,}"), "[REDACTED]"),
    (re.compile(r"(\b\d{5,11}\b.*qr|qr.*\b\d{5,11}\b)"), "[REDACTED]"),
    (
        re.compile(r"-----BEGIN (?:RSA|EC|OPENSSH) PRIVATE KEY-----[\s\S]{0,200}?-----END"),
        "[REDACTED]",
    ),
]


def redact(text: str, extra: Iterable[str] | None = None) -> str:
    """Replace secret-like substrings with [REDACTED]."""
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    if extra:
        for val in extra:
            if val and len(val) > 3:
                out = out.replace(val, "[REDACTED]")
    return out


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences."""
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
