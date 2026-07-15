"""Isolated, bounded subprocess execution for whisper.cpp transcriptions."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Literal
from uuid import uuid4

from .config import WhisperConfig


_RUNNER_VERSION = "1"
_MAX_ERROR_CHARS = 500


@dataclass(frozen=True)
class TranscriptionResult:
    text: str | None
    status: Literal["ok", "unavailable", "timeout", "failed"]
    language: str | None = None
    error: str | None = None


class WhisperRunner:
    """Run whisper.cpp with a bounded lifetime and a successful-result cache."""

    def __init__(self, cfg: WhisperConfig) -> None:
        self.cfg = cfg
        self._semaphore = asyncio.Semaphore(max(1, int(cfg.max_concurrent)))

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
            cache_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return TranscriptionResult(None, "failed", None, f"cache unavailable: {exc}")

        cache_key = self._cache_key(audio_path, model, selected_language)
        if self.cfg.cache_enabled:
            cached = self._load_cache(cache_root, cache_key)
            if cached is not None:
                return cached

        async with self._semaphore:
            # Recheck after waiting so equivalent queued requests share one result.
            if self.cfg.cache_enabled:
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
                    proc = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except OSError as exc:
                    return TranscriptionResult(None, "failed", None, f"whisper start failed: {exc}")

                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=self.cfg.timeout_seconds
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    return TranscriptionResult(None, "timeout", None, "whisper timed out")
                except asyncio.CancelledError:
                    proc.kill()
                    await proc.communicate()
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
                    text = output_file.read_text(encoding="utf-8")
                except OSError as exc:
                    return TranscriptionResult(None, "failed", None, f"unable to read transcript: {exc}")

                if not text.strip():
                    return TranscriptionResult(None, "failed", None, "whisper produced an empty transcript")
                return TranscriptionResult(text, "ok", language, None)
        except OSError as exc:
            return TranscriptionResult(None, "failed", None, f"whisper run failed: {exc}")

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
        try:
            entry = json.loads(cache_path.read_text(encoding="utf-8"))
            created_at = float(entry["created_at"])
            result = TranscriptionResult(**entry["result"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if time.time() - created_at > self.cfg.cache_ttl_seconds:
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
            temporary_path.write_text(json.dumps(entry, ensure_ascii=True), encoding="utf-8")
            os.replace(temporary_path, cache_path)
            self._trim_cache(cache_root)
        except OSError:
            self._unlink(temporary_path)

    def _trim_cache(self, cache_root: Path) -> None:
        entries: list[tuple[float, Path]] = []
        for cache_path in cache_root.glob("*.json"):
            try:
                entry = json.loads(cache_path.read_text(encoding="utf-8"))
                created_at = float(entry["created_at"])
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                self._unlink(cache_path)
                continue
            if time.time() - created_at > self.cfg.cache_ttl_seconds:
                self._unlink(cache_path)
            else:
                entries.append((created_at, cache_path))
        for _, cache_path in sorted(entries)[: -self.cfg.cache_max_items]:
            self._unlink(cache_path)

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass
