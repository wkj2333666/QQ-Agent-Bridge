"""Tests for the isolated whisper.cpp subprocess runner."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import qq_agent_bridge.whisper_runner as whisper_runner  # type: ignore
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


def make_pipe_holding_whisper(tmp_path: Path, *, marker: Path) -> Path:
    return write_executable(
        tmp_path / "pipe-holding-whisper.py",
        f"""
        import pathlib
        import subprocess
        import sys
        import time

        child_code = (
            "import os, pathlib, time; "
            "pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); "
            "time.sleep(30)"
        )
        subprocess.Popen([
            sys.executable,
            "-c",
            child_code,
        ])
        time.sleep(30)
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


def test_runner_timeout_kills_descendants_holding_output_pipes(tmp_path: Path) -> None:
    async def run() -> None:
        child_pid_path = tmp_path / "child.pid"
        fake = make_pipe_holding_whisper(tmp_path, marker=child_pid_path)
        audio = tmp_path / "input.wav"
        audio.write_bytes(b"audio")

        result = await asyncio.wait_for(
            WhisperRunner(make_cfg(fake, timeout_seconds=1.0)).transcribe(audio),
            timeout=3.0,
        )

        assert result == TranscriptionResult(None, "timeout", None, "whisper timed out")
        child_pid = int(child_pid_path.read_text())
        deadline = asyncio.get_running_loop().time() + 1.0
        while True:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail(f"Whisper descendant {child_pid} survived timeout cleanup")
            await asyncio.sleep(0.02)

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


def test_runner_creates_private_cache_and_transcript_files(
    tmp_path: Path, monkeypatch: object
) -> None:
    async def run() -> None:
        fake = make_fake_whisper(tmp_path, output="private transcript")
        audio = tmp_path / "input.wav"
        audio.write_bytes(b"audio")
        runner = WhisperRunner(make_cfg(fake))
        transcript_modes: list[int] = []
        original_read_text = Path.read_text

        def record_transcript_mode(path: Path, *args: object, **kwargs: object) -> str:
            if path.name == "transcription.txt":
                transcript_modes.append(stat.S_IMODE(path.stat().st_mode))
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", record_transcript_mode)

        result = await runner.transcribe(audio)

        cache_root = Path(runner.cfg.cache_root)
        cache_path = cache_root / f"{runner._cache_key(audio, Path(runner.cfg.model), 'zh')}.json"
        assert result.status == "ok"
        assert stat.S_IMODE(cache_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600
        assert transcript_modes == [0o600]

    previous_umask = os.umask(0)
    try:
        asyncio.run(run())
    finally:
        os.umask(previous_umask)


def test_runner_cleans_expired_entries_on_subsequent_cache_activity(
    tmp_path: Path, monkeypatch: object
) -> None:
    async def run() -> None:
        fake = make_fake_whisper(tmp_path, output="cached")
        audio = tmp_path / "input.wav"
        audio.write_bytes(b"audio")
        runner = WhisperRunner(make_cfg(fake, cache_ttl_seconds=10))
        monkeypatch.setattr(whisper_runner.time, "time", lambda: 100.0)

        assert (await runner.transcribe(audio)).status == "ok"
        expired_path = Path(runner.cfg.cache_root) / f"{'a' * 64}.json"
        expired_path.write_text(
            json.dumps(
                {
                    "created_at": 0.0,
                    "result": {
                        "text": "expired",
                        "status": "ok",
                        "language": "zh",
                        "error": None,
                    },
                }
            ),
            encoding="utf-8",
        )

        assert (await runner.transcribe(audio)).status == "ok"
        assert not expired_path.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "created_at",
    [float("nan"), float("inf"), "not-a-timestamp", True, 101.0],
    ids=["nan", "infinity", "non-numeric", "boolean", "future"],
)
def test_runner_removes_invalid_cache_timestamp_when_loading(
    tmp_path: Path, monkeypatch: object, created_at: object
) -> None:
    fake = make_fake_whisper(tmp_path)
    runner = WhisperRunner(make_cfg(fake, cache_ttl_seconds=100))
    cache_root = Path(runner.cfg.cache_root)
    cache_root.mkdir()
    cache_key = "a" * 64
    cache_path = cache_root / f"{cache_key}.json"
    cache_path.write_text(
        json.dumps(
            {
                "created_at": created_at,
                "result": {
                    "text": "private transcript",
                    "status": "ok",
                    "language": "zh",
                    "error": None,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(whisper_runner.time, "time", lambda: 100.0)

    assert runner._load_cache(cache_root, cache_key) is None
    assert not cache_path.exists()


def test_runner_removes_invalid_cache_timestamps_when_trimming(
    tmp_path: Path, monkeypatch: object
) -> None:
    fake = make_fake_whisper(tmp_path)
    runner = WhisperRunner(make_cfg(fake, cache_ttl_seconds=100))
    cache_root = Path(runner.cfg.cache_root)
    cache_root.mkdir()
    timestamps = [float("nan"), float("inf"), "not-a-timestamp", True, 101.0]
    cache_paths = []
    for index, created_at in enumerate(timestamps):
        cache_path = cache_root / f"{index:064x}.json"
        cache_path.write_text(
            json.dumps(
                {
                    "created_at": created_at,
                    "result": {
                        "text": "private transcript",
                        "status": "ok",
                        "language": "zh",
                        "error": None,
                    },
                }
            ),
            encoding="utf-8",
        )
        cache_paths.append(cache_path)
    monkeypatch.setattr(whisper_runner.time, "time", lambda: 100.0)

    runner._trim_cache(cache_root)

    assert all(not cache_path.exists() for cache_path in cache_paths)


def test_runner_preserves_unrelated_json_during_cache_cleanup(
    tmp_path: Path, monkeypatch: object
) -> None:
    async def run() -> None:
        fake = make_fake_whisper(tmp_path)
        audio = tmp_path / "input.wav"
        audio.write_bytes(b"audio")
        runner = WhisperRunner(make_cfg(fake))
        monkeypatch.setattr(whisper_runner.time, "time", lambda: 100.0)
        cache_root = Path(runner.cfg.cache_root)
        cache_root.mkdir()
        unrelated_path = cache_root / "unrelated.json"
        unrelated_path.write_text('{"keep": true}', encoding="utf-8")

        assert (await runner.transcribe(audio)).status == "ok"
        assert unrelated_path.read_text(encoding="utf-8") == '{"keep": true}'

    asyncio.run(run())
