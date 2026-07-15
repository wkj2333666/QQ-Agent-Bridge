"""Contract tests for the isolated whisper.cpp deployment helpers."""
from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_whisper_cpp.sh"
CHECKER = ROOT / "scripts" / "check_whisper_cpp.sh"
README = ROOT / "runtime" / "asr" / "README.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_deployment_scripts_are_home_local_and_safe() -> None:
    for script in (INSTALLER, CHECKER):
        assert script.is_file()
        contents = read(script)
        assert "set -euo pipefail" in contents
        assert "${QAB_ASR_ROOT:-$HOME/.local/share/qq-agent-bridge/asr}" in contents

        lowered = contents.lower()
        for forbidden in ("sudo apt", "pip install", "mamba install"):
            assert forbidden not in lowered
        assert not re.search(r"\b(?:mkdir|install|cp|mv|rm)\b[^\n]*?/usr(?:/|$)", lowered)


def test_installer_pins_source_and_model_integrity() -> None:
    contents = read(INSTALLER)

    assert 'WHISPER_CPP_REF="v1.8.6"' in contents
    assert "git clone" in contents
    assert "mktemp -d" in contents
    assert "CMAKE_BUILD_TYPE=Release" in contents
    assert "whisper-cli" in contents
    assert "ggml-tiny-q8_0.bin" in contents
    assert "c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca" in contents
    assert "sha256sum" in contents


def test_checker_validates_runtime_and_optional_wav_without_project_writes() -> None:
    contents = read(CHECKER)

    assert "--help" in contents
    assert "-f" in contents
    assert "WAV" in contents
    assert "elapsed_seconds" in contents
    assert "whisper-cli" in contents


def test_runtime_artifacts_are_ignored_and_readme_enables_runner() -> None:
    ignored = read(ROOT / ".gitignore")
    for pattern in ("runtime/asr/cache/", "runtime/asr/**/*.bin", "runtime/asr/**/*.wav"):
        assert pattern in ignored

    contents = read(README)
    assert "c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca" in contents
    assert "whisper:" in contents
    assert "enabled: true" in contents
    assert "binary:" in contents
    assert "model:" in contents
