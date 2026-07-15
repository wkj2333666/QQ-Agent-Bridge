"""Isolated, bounded subprocess execution for whisper.cpp transcriptions."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import tempfile
import time
from typing import Literal
from uuid import uuid4

from .config import WhisperConfig


_RUNNER_VERSION = "1"
_MAX_ERROR_CHARS = 500
_CACHE_FILENAME = re.compile(r"[0-9a-f]{64}\.json\Z")
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class TranscriptionResult:
    text: str | None
    status: Literal["ok", "unavailable", "timeout", "failed"]
    language: str | None = None
    error: str | None = None


class WhisperRunner:
    """Run whisper.cpp with a bounded lifetime and a successful-result cache."""

    def __init__(self, cfg: WhisperConfig) -> None:
        timeout_seconds = float(cfg.timeout_seconds)
        cfg.timeout_seconds = (
            min(3600.0, max(1.0, timeout_seconds))
            if math.isfinite(timeout_seconds)
            else WhisperConfig.timeout_seconds
        )
        max_concurrent = float(cfg.max_concurrent)
        cfg.max_concurrent = (
            min(4, max(1, int(max_concurrent)))
            if math.isfinite(max_concurrent)
            else WhisperConfig.max_concurrent
        )
        self.cfg = cfg
        self._semaphore = asyncio.Semaphore(cfg.max_concurrent)

    async def transcribe(
        self, path: Path, *, language: str | None = None
    ) -> TranscriptionResult:
        binary = Path(self.cfg.binary)
        model = Path(self.cfg.model)
        audio_path = Path(path)
        selected_language = language or self.cfg.language

        unavailable = self._validate_inputs(binary, model, audio_path)
        if unavailable is not None:
            return unavailable

        try:
            cache_root = Path(self.cfg.cache_root)
            cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            cache_root.chmod(0o700)
        except OSError as exc:
            return TranscriptionResult(None, "failed", None, f"cache unavailable: {exc}")

        cache_key = self._cache_key(audio_path, model, selected_language)
        if self.cfg.cache_enabled:
            self._trim_cache(cache_root)
            cached = self._load_cache(cache_root, cache_key)
            if cached is not None:
                return cached

        async with self._semaphore:
            # Recheck after waiting so equivalent queued requests share one result.
            if self.cfg.cache_enabled:
                self._trim_cache(cache_root)
                cached = self._load_cache(cache_root, cache_key)
                if cached is not None:
                    return cached
            result = await self._run(binary, model, audio_path, selected_language, cache_root)

        if self.cfg.cache_enabled and result.status == "ok" and result.text:
            self._store_cache(cache_root, cache_key, result)
        return result

    @staticmethod
    def _validate_inputs(
        binary: Path, model: Path, audio_path: Path
    ) -> TranscriptionResult | None:
        if not binary.is_file():
            return TranscriptionResult(None, "unavailable", None, "whisper binary unavailable")
        if not model.is_file():
            return TranscriptionResult(None, "unavailable", None, "whisper model unavailable")
        if not audio_path.is_file():
            return TranscriptionResult(None, "unavailable", None, "audio file unavailable")
        return None

    async def _run(
        self,
        binary: Path,
        model: Path,
        audio_path: Path,
        language: str,
        cache_root: Path,
    ) -> TranscriptionResult:
        try:
            with tempfile.TemporaryDirectory(dir=cache_root, prefix="run-") as run_directory:
                output_prefix = Path(run_directory) / "transcription"
                command = [
                    str(binary),
                    "-m",
                    str(model),
                    "-f",
                    str(audio_path),
                    "-l",
                    language,
                    "-otxt",
                    "-of",
                    str(output_prefix),
                    "-nt",
                    "-np",
                ]
                try:
                    kwargs: dict[str, object] = {
                        "stdout": asyncio.subprocess.PIPE,
                        "stderr": asyncio.subprocess.PIPE,
                    }
                    if os.name == "posix":
                        kwargs["start_new_session"] = True
                    proc = await asyncio.create_subprocess_exec(*command, **kwargs)
                except OSError as exc:
                    return TranscriptionResult(None, "failed", None, f"whisper start failed: {exc}")

                communicate_task = asyncio.create_task(proc.communicate())
                try:
                    stdout, stderr = await asyncio.wait_for(
                        asyncio.shield(communicate_task), timeout=self.cfg.timeout_seconds
                    )
                except asyncio.TimeoutError:
                    self._terminate_process(proc)
                    await self._cleanup_process(proc, communicate_task)
                    return TranscriptionResult(None, "timeout", None, "whisper timed out")
                except asyncio.CancelledError:
                    self._terminate_process(proc)
                    await self._cleanup_process(proc, communicate_task)
                    raise

                if proc.returncode != 0:
                    return TranscriptionResult(
                        None,
                        "failed",
                        None,
                        self._process_error(proc.returncode, stderr, stdout),
                    )

                output_file = output_prefix.with_suffix(".txt")
                if not output_file.is_file():
                    return TranscriptionResult(
                        None, "failed", None, "whisper did not produce a text file"
                    )
                try:
                    output_file.chmod(0o600)
                    text = output_file.read_text(encoding="utf-8")
                except OSError as exc:
                    return TranscriptionResult(None, "failed", None, f"unable to read transcript: {exc}")

                if not text.strip():
                    return TranscriptionResult(None, "failed", None, "whisper produced an empty transcript")
                return TranscriptionResult(text, "ok", language, None)
        except OSError as exc:
            return TranscriptionResult(None, "failed", None, f"whisper run failed: {exc}")

    @staticmethod
    def _terminate_process(proc: asyncio.subprocess.Process) -> None:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                return
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    async def _cleanup_process(
        proc: asyncio.subprocess.Process, communicate_task: asyncio.Task[tuple[bytes, bytes]]
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(communicate_task), timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            communicate_task.cancel()
            await asyncio.gather(communicate_task, return_exceptions=True)
        except asyncio.CancelledError:
            communicate_task.cancel()
            await asyncio.gather(communicate_task, return_exceptions=True)
            raise
        try:
            await asyncio.wait_for(proc.wait(), timeout=_PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    def _process_error(returncode: int, stderr: bytes, stdout: bytes) -> str:
        diagnostic = stderr.decode("utf-8", errors="replace").strip()
        if not diagnostic:
            diagnostic = stdout.decode("utf-8", errors="replace").strip()
        message = f"whisper exited with status {returncode}"
        if diagnostic:
            message = f"{message}: {diagnostic}"
        return message[:_MAX_ERROR_CHARS]

    def _cache_key(self, audio_path: Path, model: Path, language: str) -> str:
        model_stat = model.stat()
        digest = hashlib.sha256()
        with audio_path.open("rb") as audio_file:
            for chunk in iter(lambda: audio_file.read(1024 * 1024), b""):
                digest.update(chunk)
        payload = {
            "audio_sha256": digest.hexdigest(),
            "language": language,
            "model": str(model.resolve()),
            "model_mtime_ns": model_stat.st_mtime_ns,
            "model_size": model_stat.st_size,
            "runner_version": _RUNNER_VERSION,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _load_cache(self, cache_root: Path, cache_key: str) -> TranscriptionResult | None:
        cache_path = cache_root / f"{cache_key}.json"
        now = time.time()
        try:
            if not stat.S_ISREG(cache_path.lstat().st_mode):
                return None
            cache_path.chmod(0o600)
            entry = json.loads(cache_path.read_text(encoding="utf-8"))
            created_at = self._cache_created_at(entry["created_at"], now)
            result = TranscriptionResult(**entry["result"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            self._unlink(cache_path)
            return None
        if now - created_at > self.cfg.cache_ttl_seconds:
            self._unlink(cache_path)
            return None
        if result.status != "ok" or not result.text or not result.text.strip():
            return None
        return result

    def _store_cache(
        self, cache_root: Path, cache_key: str, result: TranscriptionResult) -> None:
        cache_path = cache_root / f"{cache_key}.json"
        temporary_path = cache_root / f".{cache_key}.{uuid4().hex}.tmp"
        entry = {"created_at": time.time(), "result": asdict(result)}
        try:
            fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as cache_file:
                os.fchmod(cache_file.fileno(), 0o600)
                json.dump(entry, cache_file, ensure_ascii=True)
                cache_file.flush()
                os.fsync(cache_file.fileno())
            os.replace(temporary_path, cache_path)
            self._trim_cache(cache_root)
        except OSError:
            self._unlink(temporary_path)

    def _trim_cache(self, cache_root: Path) -> None:
        entries: list[tuple[float, Path]] = []
        try:
            cache_paths = tuple(cache_root.iterdir())
        except OSError:
            return
        now = time.time()
        for cache_path in cache_paths:
            if not _CACHE_FILENAME.fullmatch(cache_path.name):
                continue
            try:
                if not stat.S_ISREG(cache_path.lstat().st_mode):
                    continue
                cache_path.chmod(0o600)
                entry = json.loads(cache_path.read_text(encoding="utf-8"))
                created_at = self._cache_created_at(entry["created_at"], now)
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                self._unlink(cache_path)
                continue
            if now - created_at > self.cfg.cache_ttl_seconds:
                self._unlink(cache_path)
            else:
                entries.append((created_at, cache_path))
        for _, cache_path in sorted(entries)[: -self.cfg.cache_max_items]:
            self._unlink(cache_path)

    @staticmethod
    def _cache_created_at(value: object, now: float) -> float:
        if isinstance(value, bool):
            raise ValueError("invalid cache timestamp")
        created_at = float(value)
        if not math.isfinite(created_at) or created_at > now:
            raise ValueError("invalid cache timestamp")
        return created_at

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass
