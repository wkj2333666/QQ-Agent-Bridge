"""Contract tests for the isolated whisper.cpp deployment helpers."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import textwrap
import wave


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_whisper_cpp.sh"
CHECKER = ROOT / "scripts" / "check_whisper_cpp.sh"
README = ROOT / "runtime" / "asr" / "README.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


WHISPER_CPP_COMMIT = "080bbbe85230f624f0b52127f1ae1218247989f9"
MODEL_SHA256 = "c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca"


def write_executable(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def make_fake_toolchain(tmp_path: Path) -> tuple[Path, Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    command_log = tmp_path / "commands.log"

    write_executable(
        tools / "git",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\\n' "$*" >> "$FAKE_COMMAND_LOG"
        if [[ "$1" == "clone" ]]; then
          mkdir -p "${@: -1}/.git"
          exit 0
        fi
        if [[ "$1" == "-C" ]]; then
          shift 2
          case "$1" in
            fetch|checkout) exit 0 ;;
            rev-parse) printf '%s\\n' "$FAKE_GIT_HEAD"; exit 0 ;;
          esac
        fi
        exit 1
        """,
    )
    write_executable(
        tools / "cmake",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "$1" == "-S" ]]; then
          while [[ $# -gt 0 ]]; do
            if [[ "$1" == "-B" ]]; then
              build_dir="$2"
              break
            fi
            shift
          done
          mkdir -p "$build_dir"
          exit 0
        fi
        if [[ "$1" == "--build" ]]; then
          build_dir="$2"
          mkdir -p "$build_dir/bin"
          cat > "$build_dir/bin/whisper-cli" <<'EOF'
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "--help" ]]; then exit 0; fi
        while [[ $# -gt 0 ]]; do
          if [[ "$1" == "-of" ]]; then
            printf 'fake transcript\\n' > "$2.txt"
            exit 0
          fi
          shift
        done
        EOF
          chmod +x "$build_dir/bin/whisper-cli"
          exit 0
        fi
        exit 1
        """,
    )
    write_executable(
        tools / "curl",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        while [[ $# -gt 0 ]]; do
          if [[ "$1" == "--output" ]]; then output="$2"; break; fi
          shift
        done
        printf '%s' "$FAKE_MODEL_CONTENT" > "$output"
        """,
    )
    write_executable(
        tools / "sha256sum",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "$(cat "$1")" == "good-model" ]]; then
          printf '%s  %s\\n' '{MODEL_SHA256}' "$1"
        else
          printf '%064d  %s\\n' 0 "$1"
        fi
        """,
    )
    return tools, command_log


def installer_env(home: Path, asr_root: Path, tools: Path, command_log: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "QAB_ASR_ROOT": str(asr_root),
            "PATH": f"{tools}{os.pathsep}{env['PATH']}",
            "FAKE_COMMAND_LOG": str(command_log),
            "FAKE_GIT_HEAD": WHISPER_CPP_COMMIT,
        }
    )
    return env


def run_script(script: Path, env: dict[str, str], *args: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *(str(arg) for arg in args)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def create_previous_release(asr_root: Path) -> Path:
    release = asr_root / "releases" / "old"
    (release / "bin").mkdir(parents=True)
    (release / "models").mkdir()
    (release / "bin" / "whisper-cli").write_text("old binary", encoding="utf-8")
    (release / "models" / "ggml-tiny-q8_0.bin").write_text("old model", encoding="utf-8")
    (asr_root / "current").symlink_to("releases/old")
    return release


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

    assert f'WHISPER_CPP_COMMIT="{WHISPER_CPP_COMMIT}"' in contents
    assert "git clone" in contents
    assert "git -C \"$SOURCE_DIR\" fetch" in contents
    assert "git -C \"$SOURCE_DIR\" checkout --detach" in contents
    assert "git -C \"$SOURCE_DIR\" rev-parse HEAD" in contents
    assert "mktemp -d" in contents
    assert "CMAKE_BUILD_TYPE=Release" in contents
    assert "whisper-cli" in contents
    assert "ggml-tiny-q8_0.bin" in contents
    assert "c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca" in contents
    assert "sha256sum" in contents


def test_installer_checksum_mismatch_preserves_current_release(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    asr_root = home / "asr"
    old_release = create_previous_release(asr_root)
    tools, command_log = make_fake_toolchain(tmp_path)
    env = installer_env(home, asr_root, tools, command_log)
    env["FAKE_MODEL_CONTENT"] = "bad-model"

    result = run_script(INSTALLER, env)

    assert result.returncode != 0
    assert "Model SHA-256 mismatch" in result.stderr
    assert os.readlink(asr_root / "current") == "releases/old"
    assert (asr_root / "current").resolve() == old_release
    assert (asr_root / "current" / "bin" / "whisper-cli").read_text(encoding="utf-8") == "old binary"
    assert sorted(path.name for path in (asr_root / "releases").iterdir()) == ["old"]


def test_installer_checkout_mismatch_preserves_current_release(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    asr_root = home / "asr"
    old_release = create_previous_release(asr_root)
    tools, command_log = make_fake_toolchain(tmp_path)
    env = installer_env(home, asr_root, tools, command_log)
    env["FAKE_GIT_HEAD"] = "deadbeef"
    env["FAKE_MODEL_CONTENT"] = "good-model"

    result = run_script(INSTALLER, env)

    assert result.returncode != 0
    assert "whisper.cpp checkout mismatch" in result.stderr
    assert os.readlink(asr_root / "current") == "releases/old"
    assert (asr_root / "current").resolve() == old_release
    assert (asr_root / "current" / "bin" / "whisper-cli").read_text(encoding="utf-8") == "old binary"
    assert sorted(path.name for path in (asr_root / "releases").iterdir()) == ["old"]


def test_installer_publishes_verified_release_by_switching_current_atomically(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    asr_root = home / "asr"
    create_previous_release(asr_root)
    tools, command_log = make_fake_toolchain(tmp_path)
    env = installer_env(home, asr_root, tools, command_log)
    env["FAKE_MODEL_CONTENT"] = "good-model"

    result = run_script(INSTALLER, env)

    assert result.returncode == 0, result.stderr
    assert (asr_root / "current").is_symlink()
    assert os.readlink(asr_root / "current") != "releases/old"
    assert (asr_root / "current" / "bin" / "whisper-cli").is_file()
    assert (asr_root / "current" / "models" / "ggml-tiny-q8_0.bin").read_text(encoding="utf-8") == "good-model"
    commands = command_log.read_text(encoding="utf-8")
    assert f"fetch --depth 1 origin {WHISPER_CPP_COMMIT}" in commands
    assert f"checkout --detach {WHISPER_CPP_COMMIT}" in commands
    assert "rev-parse HEAD" in commands


def test_checker_accepts_stubbed_release_and_rejects_missing_wav(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    asr_root = home / "asr"
    release = asr_root / "releases" / "ready"
    (release / "bin").mkdir(parents=True)
    (release / "models").mkdir()
    write_executable(
        release / "bin" / "whisper-cli",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "--help" ]]; then exit 0; fi
        while [[ $# -gt 0 ]]; do
          if [[ "$1" == "-of" ]]; then
            printf 'stub transcript\\n' > "$2.txt"
            exit 0
          fi
          shift
        done
        """,
    )
    (release / "models" / "ggml-tiny-q8_0.bin").write_text("stub model", encoding="utf-8")
    (asr_root / "current").symlink_to("releases/ready")
    env = os.environ.copy()
    env.update({"HOME": str(home), "QAB_ASR_ROOT": str(asr_root)})

    ready = run_script(CHECKER, env)
    missing = run_script(CHECKER, env, tmp_path / "missing.wav")

    assert ready.returncode == 0, ready.stderr
    assert "No WAV supplied" in ready.stdout
    assert missing.returncode == 1
    assert "WAV input is missing" in missing.stderr


def test_checker_transcribes_valid_wav_and_writes_transcript(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    asr_root = home / "asr"
    release = asr_root / "releases" / "ready"
    (release / "bin").mkdir(parents=True)
    (release / "models").mkdir()
    cli_args = tmp_path / "cli-args.log"
    write_executable(
        release / "bin" / "whisper-cli",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\n' "$*" >> "$FAKE_CLI_ARGS"
        if [[ "${1:-}" == "--help" ]]; then exit 0; fi
        while [[ $# -gt 0 ]]; do
          if [[ "$1" == "-of" ]]; then
            printf 'transcribed test audio\\n' > "$2.txt"
            exit 0
          fi
          shift
        done
        exit 1
        """,
    )
    (release / "models" / "ggml-tiny-q8_0.bin").write_text("stub model", encoding="utf-8")
    (asr_root / "current").symlink_to("releases/ready")
    wav_path = tmp_path / "valid.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\0\0" * 160)
    env = os.environ.copy()
    env.update({"HOME": str(home), "QAB_ASR_ROOT": str(asr_root), "FAKE_CLI_ARGS": str(cli_args)})

    result = run_script(CHECKER, env, wav_path)

    assert result.returncode == 0, result.stderr
    assert "WAV smoke check: exit=0" in result.stdout
    assert "Transcript:" in result.stdout
    assert "transcribed test audio" in result.stdout
    calls = cli_args.read_text(encoding="utf-8").splitlines()
    assert any("-f " + str(wav_path) in call for call in calls)
    assert any("-of " in call for call in calls)


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
    assert WHISPER_CPP_COMMIT in contents
    assert MODEL_SHA256 in contents
    assert "whisper:" in contents
    assert "enabled: true" in contents
    assert "/current/bin/whisper-cli" in contents
    assert "/current/models/ggml-tiny-q8_0.bin" in contents
    example_config = read(ROOT / "config.example.yaml")
    assert "/current/bin/whisper-cli" in example_config
    assert "/current/models/ggml-tiny-q8_0.bin" in example_config
