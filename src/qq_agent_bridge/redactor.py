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
        re.compile(r"(?i)((?:资源发送令牌|resource\s+send(?:ing)?\s+token)\s*[：:]\s*)(\S+)"),
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

_MAX_EXTRA_REDACTIONS = 128
_MAX_EXTRA_VALUE_CHARS = 2_048


def redact(text: str, extra: Iterable[str] | None = None) -> str:
    """Replace secret-like substrings with [REDACTED]."""
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    if extra:
        applied = 0
        for val in extra:
            pattern = _extra_redaction_pattern(val)
            if pattern is None:
                continue
            out = pattern.sub("[REDACTED]", out)
            applied += 1
            if applied >= _MAX_EXTRA_REDACTIONS:
                break
    return out


def _extra_redaction_pattern(value: str) -> re.Pattern[str] | None:
    normalized = str(value or "").strip()
    if not 3 < len(normalized) <= _MAX_EXTRA_VALUE_CHARS:
        return None
    fragments = normalized.split()
    if not fragments:
        return None
    expression = r"\s+".join(re.escape(fragment) for fragment in fragments)
    return re.compile(expression, re.IGNORECASE)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences."""
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
