"""Redact secrets from text before sending to chat or logs."""
from __future__ import annotations

import re
from typing import Iterable

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|auth)[\"'\s:=]+([A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)(sk-[A-Za-z0-9]{20,})"),  # openai style
    re.compile(r"(?i)(ghp_[A-Za-z0-9]{30,})"),  # github
    re.compile(r"(\b\d{5,11}\b.*qr|qr.*\b\d{5,11}\b)"),  # rough qq qr hints
    re.compile(r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----[\s\S]{0,200}?-----END"),
]


def redact(text: str, extra: Iterable[str] | None = None) -> str:
    """Replace secret-like substrings with [REDACTED]."""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(r"\1[REDACTED]", out)
    if extra:
        for val in extra:
            if val and len(val) > 3:
                out = out.replace(val, "[REDACTED]")
    return out


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences."""
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
