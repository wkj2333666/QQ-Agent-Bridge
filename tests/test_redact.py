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
    assert all(f"QQBOT_SEND_{kind}: [REDACTED]" in out for kind in tokens)


def test_redact_bare_resource_token_wording_without_hiding_normal_prose() -> None:
    token = "bare-resource-token-value"
    sensitive = f"资源发送令牌：{token}"
    ordinary = "资源发送需要令牌，但这句普通说明应当保留。"

    out = redact(f"{sensitive}\n{ordinary}")

    assert token not in out
    assert "资源发送令牌：[REDACTED]" in out
    assert ordinary in out


def test_redact_replaces_full_openai_and_github_secrets() -> None:
    openai_secret = "sk-" + "aB3_" * 8
    github_secret = "ghp_" + "Z9x" * 12

    out = redact(f"openai={openai_secret} github={github_secret}")

    assert openai_secret not in out
    assert github_secret not in out
    assert out.count("[REDACTED]") == 2


def test_extra_redaction_is_case_insensitive_whitespace_flexible_and_escaped() -> None:
    memory = "Subject-Redacted Prefers (Paper)+ Reports"
    rendered = "subject-redacted\tPREFERS   (paper)+ reports"

    out = redact(f"before {rendered} after", extra=(memory,))

    assert rendered not in out
    assert out == "before [REDACTED] after"


if __name__ == "__main__":
    test_redact_basic()
    test_redact_qrish()
    print("redact tests OK")
