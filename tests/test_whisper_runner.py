"""Tests for the isolated whisper.cpp subprocess runner."""
from __future__ import annotations

import asyncio
import stat
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import WhisperConfig  # type: ignore
from qq_agent_bridge.whisper_runner import TranscriptionResult, WhisperRunner  # type: ignore


def make_cfg(binary: Path, **overrides: object) -> WhisperConfig:
    model = binary.parent / "model.bin"
    model.write_bytes(b"not a real model")
    values: dict[str, object] = {
        "binary": str(binary),
        "model": str(model),
        "cache_root": str(binary.parent / "cache"),
    }
    values.update(overrides)
    return WhisperConfig(**values)


def write_executable(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_fake_whisper(
    tmp_path: Path,
    *,
    output: str = "transcript",
    exit_code: int = 0,
    stderr: str = "",
) -> Path:
    return write_executable(
        tmp_path / "fake-whisper.py",
        f"""
        import pathlib
        import sys

        args = sys.argv[1:]
        prefix = pathlib.Path(args[args.index("-of") + 1])
        if {exit_code} == 0:
            prefix.with_suffix(".txt").write_text({output!r}, encoding="utf-8")
        if {stderr!r}:
            print({stderr!r}, file=sys.stderr)
        raise SystemExit({exit_code})
        """,
    )


def make_sleeping_whisper(tmp_path: Path, *, seconds: float) -> Path:
    return write_executable(
        tmp_path / "sleeping-whisper.py",
        f"""
        import time
        time.sleep({seconds!r})
        """,
    )


def make_recording_whisper(tmp_path: Path, *, marker: Path) -> Path:
    return write_executable(
        tmp_path / "recording-whisper.py",
        f"""
        import pathlib
        import time
        import sys

        marker = pathlib.Path({str(marker)!r})
        max_active = marker.parent / "max-active"
        active = int(marker.read_text() if marker.exists() else "0") + 1
        marker.write_text(str(active))
        previous_max = int(max_active.read_text() if max_active.exists() else "0")
        max_active.write_text(str(max(active, previous_max)))
        time.sleep(0.1)
        marker.write_text(str(active - 1))
        args = sys.argv[1:]
        pathlib.Path(args[args.index("-of") + 1]).with_suffix(".txt").write_text("done")
        """,
    )


def test_runner_reads_text_file_without_readline(tmp_path: Path) -> None:
    async def run() -> None:
        fake = make_fake_whisper(tmp_path, output="你好，世界")
        audio = tmp_path / "input.wav"
        audio.write_bytes(b"audio")

        result = await WhisperRunner(make_cfg(fake)).transcribe(audio)

        assert result == TranscriptionResult("你好，世界", "ok", "zh", None)

    asyncio.run(run())


def test_runner_reports_nonzero_exit_and_does_not_guess(tmp_path: Path) -> None:
    async def run() -> None:
        fake = make_fake_whisper(tmp_path, exit_code=2, stderr="model missing")
        audio = tmp_path / "input.wav"
        audio.write_bytes(b"audio")

        result = await WhisperRunner(make_cfg(fake)).transcribe(audio)

        assert result.status == "failed"
        assert result.text is None
        assert "model missing" in (result.error or "")

    asyncio.run(run())


def test_runner_reports_timeout(tmp_path: Path) -> None:
    async def run() -> None:
        fake = make_sleeping_whisper(tmp_path, seconds=2.0)
        audio = tmp_path / "input.wav"
        audio.write_bytes(b"audio")

        result = await WhisperRunner(make_cfg(fake, timeout_seconds=1.0)).transcribe(audio)

        assert result.status == "timeout"
        assert result.text is None

    asyncio.run(run())


def test_runner_limits_concurrency_to_one(tmp_path: Path) -> None:
    async def run() -> None:
        fake = make_recording_whisper(tmp_path, marker=tmp_path / "active")
        first_audio = tmp_path / "one.wav"
        second_audio = tmp_path / "two.wav"
        first_audio.write_bytes(b"one")
        second_audio.write_bytes(b"two")
        runner = WhisperRunner(make_cfg(fake, max_concurrent=1))

        first, second = await asyncio.gather(
            runner.transcribe(first_audio),
            runner.transcribe(second_audio),
        )

        assert first.status == second.status == "ok"
        assert (tmp_path / "max-active").read_text() == "1"

    asyncio.run(run())


def test_runner_clamps_direct_max_concurrent_config(tmp_path: Path) -> None:
    fake = make_fake_whisper(tmp_path)
    cfg = make_cfg(fake, max_concurrent=999999)

    runner = WhisperRunner(cfg)

    assert cfg.max_concurrent == 4
    assert runner._semaphore._value == 4


def test_runner_replaces_non_finite_direct_timeout_config(tmp_path: Path) -> None:
    fake = make_fake_whisper(tmp_path)
    cfg = make_cfg(fake, timeout_seconds=float("inf"))

    WhisperRunner(cfg)

    assert cfg.timeout_seconds == WhisperConfig.timeout_seconds


def test_runner_reports_unavailable_for_missing_regular_input(tmp_path: Path) -> None:
    async def run() -> None:
        fake = make_fake_whisper(tmp_path)

        result = await WhisperRunner(make_cfg(fake)).transcribe(tmp_path / "missing.wav")

        assert result == TranscriptionResult(None, "unavailable", None, "audio file unavailable")

    asyncio.run(run())


def test_runner_reuses_successful_cache_entry(tmp_path: Path) -> None:
    async def run() -> None:
        fake = make_fake_whisper(tmp_path, output="cached")
        audio = tmp_path / "input.wav"
        audio.write_bytes(b"audio")
        runner = WhisperRunner(make_cfg(fake))

        first = await runner.transcribe(audio)
        make_fake_whisper(tmp_path, exit_code=2, stderr="cache was not used")
        second = await runner.transcribe(audio)

        assert first == second == TranscriptionResult("cached", "ok", "zh", None)

    asyncio.run(run())
