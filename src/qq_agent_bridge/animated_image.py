"""Bounded metadata probing and frame sampling for animated images."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from typing import Literal

from .config import ResourcesConfig

AnimationStatus = Literal["ready", "static", "unavailable"]


@dataclass(frozen=True)
class AnimationExtraction:
    status: AnimationStatus
    source_frame_count: int | None = None
    duration_seconds: float | None = None
    frame_paths: tuple[str, ...] = ()
    frame_times: tuple[float, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class _AnimationMetadata:
    frame_count: int | None
    duration_seconds: float | None
    width: int | None
    height: int | None


class AnimatedImageExtractor:
    def __init__(self, cfg: ResourcesConfig) -> None:
        self.cfg = cfg
        self.ffprobe = shutil.which(cfg.animation_ffprobe_binary)
        self.ffmpeg = shutil.which(cfg.animation_ffmpeg_binary)

    async def extract(self, source: Path, workspace: Path) -> AnimationExtraction:
        try:
            source = source.resolve(strict=True)
            workspace = workspace.resolve(strict=True)
            source.relative_to(workspace)
        except (OSError, ValueError):
            return AnimationExtraction("unavailable", error="animation source is unavailable")
        if not self.ffprobe or not self.ffmpeg:
            return await self._extract_with_pillow(
                source,
                workspace,
                "ffprobe/ffmpeg unavailable",
            )

        metadata = await self._probe(source)
        if isinstance(metadata, str):
            return await self._extract_with_pillow(source, workspace, metadata)
        frame_count = metadata.frame_count
        duration = metadata.duration_seconds
        if metadata.width is None or metadata.height is None:
            return await self._extract_with_pillow(
                source,
                workspace,
                "ffprobe did not report animation dimensions",
            )
        if metadata.width * metadata.height > self.cfg.animation_max_source_pixels:
            return AnimationExtraction(
                "unavailable",
                source_frame_count=frame_count,
                duration_seconds=duration,
                error="animation dimensions exceed safety limit",
            )
        if frame_count is not None and frame_count <= 1:
            return AnimationExtraction("static", source_frame_count=frame_count, duration_seconds=duration)
        if frame_count is None:
            return await self._extract_with_pillow(
                source,
                workspace,
                "ffprobe did not report an animation frame count",
            )

        sample_duration = min(
            duration if duration and duration > 0 else self.cfg.animation_max_duration_seconds,
            float(self.cfg.animation_max_duration_seconds),
        )
        frame_dir = Path(
            tempfile.mkdtemp(prefix=f".{source.stem}-frames-", dir=source.parent)
        ).resolve(strict=False)
        try:
            frame_dir.relative_to(workspace)
            os.chmod(frame_dir, 0o700)
            error = await self._extract_frames(
                source,
                frame_dir,
                sample_duration,
                frame_count,
            )
            if error:
                self._remove_sampled_frames(frame_dir)
                return await self._extract_with_pillow(source, workspace, error)
            frames = tuple(sorted(frame_dir.glob("frame-*.png")))
            if not frames:
                self._remove_empty_frame_dir(frame_dir)
                return AnimationExtraction(
                    "unavailable",
                    source_frame_count=frame_count,
                    duration_seconds=duration,
                    error="ffmpeg produced no animation frames",
                )
            relative_paths = tuple(frame.relative_to(workspace).as_posix() for frame in frames)
            times = self._sample_times(len(frames), duration, sample_duration)
            return AnimationExtraction(
                "ready",
                source_frame_count=frame_count,
                duration_seconds=duration,
                frame_paths=relative_paths,
                frame_times=times,
            )
        except (OSError, ValueError):
            self._remove_sampled_frames(frame_dir)
            return AnimationExtraction(
                "unavailable",
                source_frame_count=frame_count,
                duration_seconds=duration,
                error="animation frame staging unavailable",
            )

    async def _extract_with_pillow(
        self,
        source: Path,
        workspace: Path,
        prior_error: str,
    ) -> AnimationExtraction:
        frame_dir: Path | None = None
        try:
            frame_dir = Path(
                tempfile.mkdtemp(prefix=f".{source.stem}-pillow-frames-", dir=source.parent)
            ).resolve(strict=False)
            frame_dir.relative_to(workspace)
            os.chmod(frame_dir, 0o700)
        except (OSError, ValueError):
            if frame_dir is not None:
                self._remove_sampled_frames(frame_dir)
            return AnimationExtraction("unavailable", error="animation frame staging unavailable")
        worker = Path(__file__).with_name("pillow_animation_worker.py").resolve(strict=True)
        result = await self._run(
            [
                sys.executable,
                "-I",
                str(worker),
                str(source),
                str(frame_dir),
                str(self.cfg.animation_max_frames),
                str(self.cfg.animation_max_duration_seconds),
                str(self.cfg.animation_max_dimension),
                str(self.cfg.animation_max_source_pixels),
            ]
        )
        if isinstance(result, str):
            self._remove_sampled_frames(frame_dir)
            return AnimationExtraction(
                "unavailable",
                error=f"{prior_error}; Pillow fallback failed: {result}",
            )
        stdout, _stderr = result
        try:
            payload = json.loads(stdout.decode("utf-8", "replace"))
            status = payload["status"]
        except (KeyError, TypeError, json.JSONDecodeError):
            self._remove_sampled_frames(frame_dir)
            return AnimationExtraction(
                "unavailable",
                error=f"{prior_error}; Pillow fallback returned invalid metadata",
            )
        if status == "static":
            self._remove_sampled_frames(frame_dir)
            return AnimationExtraction(
                "static",
                source_frame_count=self._positive_int(payload.get("source_frame_count")),
            )
        if status != "ready":
            self._remove_sampled_frames(frame_dir)
            detail = str(payload.get("error") or "unsupported animation")[:160]
            return AnimationExtraction("unavailable", error=f"{prior_error}; {detail}")
        frames = tuple(sorted(frame_dir.glob("frame-*.png")))
        if not frames or len(frames) > self.cfg.animation_max_frames:
            self._remove_sampled_frames(frame_dir)
            return AnimationExtraction(
                "unavailable",
                error=f"{prior_error}; Pillow fallback produced invalid frames",
            )
        times = payload.get("frame_times")
        frame_times = (
            tuple(float(value) for value in times)
            if isinstance(times, list) and len(times) == len(frames)
            else ()
        )
        return AnimationExtraction(
            "ready",
            source_frame_count=self._positive_int(payload.get("source_frame_count")),
            duration_seconds=self._positive_float(payload.get("duration_seconds")),
            frame_paths=tuple(frame.relative_to(workspace).as_posix() for frame in frames),
            frame_times=frame_times,
        )

    async def _probe(self, source: Path) -> _AnimationMetadata | str:
        assert self.ffprobe
        cmd = [
            self.ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=nb_read_frames,nb_frames,duration,width,height",
            "-of",
            "json",
            str(source),
        ]
        result = await self._run(cmd)
        if isinstance(result, str):
            return result
        stdout, _stderr = result
        try:
            data = json.loads(stdout.decode("utf-8", "replace"))
            stream = data["streams"][0]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return "ffprobe returned invalid animation metadata"
        frame_count = self._positive_int(stream.get("nb_read_frames")) or self._positive_int(
            stream.get("nb_frames")
        )
        duration = self._positive_float(stream.get("duration"))
        return _AnimationMetadata(
            frame_count=frame_count,
            duration_seconds=duration,
            width=self._positive_int(stream.get("width")),
            height=self._positive_int(stream.get("height")),
        )

    async def _extract_frames(
        self,
        source: Path,
        frame_dir: Path,
        sample_duration: float,
        source_frame_count: int | None,
    ) -> str | None:
        assert self.ffmpeg
        max_frames = self.cfg.animation_max_frames
        dimension = self.cfg.animation_max_dimension
        output = frame_dir / "frame-%03d.png"
        scale = f"scale={dimension}:{dimension}:force_original_aspect_ratio=decrease"
        if source_frame_count and source_frame_count > 1:
            step = max(1, math.ceil(source_frame_count / max_frames))
            filters = f"select=not(mod(n\\,{step})),{scale}"
        else:
            fps = min(10.0, max(0.5, max_frames / max(sample_duration, 0.1)))
            filters = f"fps={fps:.6f},{scale}"
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-t",
            f"{sample_duration:.3f}",
            "-i",
            str(source),
            "-vf",
            filters,
            "-frames:v",
            str(max_frames),
            "-fps_mode",
            "vfr",
            str(output),
        ]
        result = await self._run(cmd)
        return result if isinstance(result, str) else None

    async def _run(self, cmd: list[str]) -> tuple[bytes, bytes] | str:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.cfg.animation_timeout_seconds
            )
        except asyncio.TimeoutError:
            await self._kill(proc)
            return "animation tool timeout"
        except asyncio.CancelledError:
            await self._kill(proc)
            raise
        except (FileNotFoundError, OSError):
            return "animation tool unavailable"
        if proc.returncode:
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            return (detail[-1][:240] if detail else "animation tool failed")
        return stdout, stderr

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process | None) -> None:
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except Exception:
            pass

    @staticmethod
    def _positive_int(value: object) -> int | None:
        try:
            parsed = int(str(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _positive_float(value: object) -> float | None:
        try:
            parsed = float(str(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _sample_times(
        frame_count: int,
        duration: float | None,
        sample_duration: float,
    ) -> tuple[float, ...]:
        if duration is None:
            return ()
        bounded_duration = min(duration, sample_duration)
        if frame_count <= 1:
            return (0.0,)
        return tuple(
            bounded_duration * index / (frame_count - 1) for index in range(frame_count)
        )

    @staticmethod
    def _remove_empty_frame_dir(frame_dir: Path) -> None:
        try:
            frame_dir.rmdir()
        except OSError:
            pass

    @staticmethod
    def _remove_sampled_frames(frame_dir: Path) -> None:
        try:
            for frame in frame_dir.glob("frame-*.png"):
                frame.unlink(missing_ok=True)
            frame_dir.rmdir()
        except OSError:
            pass
