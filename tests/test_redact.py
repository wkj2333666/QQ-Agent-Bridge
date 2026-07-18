"""Simple smoke tests (no external deps)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.redactor import redact, strip_ansi  # type: ignore


def test_redact_basic() -> None:
    s = 'token="sk-abc123def456ghi789" password=foo'
    out = redact(s)
    assert "sk-abc" not in out
    assert "[REDACTED]" in out


def test_strip_ansi() -> None:
    s = "\x1b[31mred\x1b[0m text"
    assert strip_ansi(s) == "red text"


def test_redact_qrish() -> None:
    s = "scan qr for 1234567890"
    out = redact(s)
    assert "REDACTED" in out or "qr" not in out.lower()  # loose


def test_redact_bare_outgoing_directive_tokens() -> None:
    tokens = {
        "IMAGE": "image-directive-token",
        "FILE": "file-directive-token",
        "VOICE": "voice-directive-token",
        "AUDIO": "audio-directive-token",
    }
    text = "\n".join(
        f"QQBOT_SEND_{kind}: {token} downloads/outgoing/resource.bin"
        for kind, token in tokens.items()
    )

    out = redact(text)

    assert all(token not in out for token in tokens.values())
    assert out.count("[REDACTED]") == len(tokens)


if __name__ == "__main__":
    test_redact_basic()
    test_strip_ansi()
    test_redact_qrish()
    print("redact tests OK")
