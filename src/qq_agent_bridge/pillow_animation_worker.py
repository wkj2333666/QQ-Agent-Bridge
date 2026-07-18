"""Isolated Pillow worker for bounded animated-image frame extraction."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from PIL import Image, UnidentifiedImageError

MAX_SCAN_FRAMES = 2000
def _emit(**payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def _duration_seconds(image: Image.Image) -> float:
    raw = image.info.get("duration", 100)
    try:
        milliseconds = float(raw)
    except (TypeError, ValueError):
        milliseconds = 100.0
    return min(10.0, max(0.01, milliseconds / 1000.0))


def _sample_positions(count: int, maximum: int) -> tuple[int, ...]:
    if count <= maximum:
        return tuple(range(count))
    return tuple(round(index * (count - 1) / (maximum - 1)) for index in range(maximum))


def extract(
    source: Path,
    output_dir: Path,
    max_frames: int,
    max_duration: float,
    max_dimension: int,
    max_source_pixels: int,
) -> int:
    try:
        with Image.open(source) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_source_pixels:
                _emit(status="unavailable", error="image dimensions exceed safety limit")
                return 0
            source_frames = int(getattr(image, "n_frames", 1) or 1)
            if source_frames <= 1 or not bool(getattr(image, "is_animated", False)):
                _emit(status="static", source_frame_count=source_frames)
                return 0

            timeline: list[tuple[int, float]] = []
            elapsed = 0.0
            scan_limit = min(source_frames, MAX_SCAN_FRAMES)
            for frame_index in range(scan_limit):
                if timeline and elapsed >= max_duration:
                    break
                image.seek(frame_index)
                timeline.append((frame_index, elapsed))
                elapsed += _duration_seconds(image)
            if not timeline:
                _emit(status="unavailable", error="animation has no readable frames")
                return 0

            output_dir.mkdir(parents=False, exist_ok=True, mode=0o700)
            positions = _sample_positions(len(timeline), max_frames)
            frame_times: list[float] = []
            for output_index, position in enumerate(positions, start=1):
                frame_index, timestamp = timeline[position]
                image.seek(frame_index)
                frame = image.convert("RGBA")
                frame.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                frame.save(output_dir / f"frame-{output_index:03d}.png", format="PNG")
                frame_times.append(timestamp)

            complete_scan = len(timeline) == source_frames
            _emit(
                status="ready",
                source_frame_count=source_frames,
                duration_seconds=elapsed if complete_scan else None,
                frame_times=frame_times,
            )
            return 0
    except (EOFError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        _emit(status="unavailable", error=f"Pillow could not decode animation: {type(exc).__name__}")
        return 0


def main() -> int:
    if len(sys.argv) != 7:
        return 2
    return extract(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        max(2, min(16, int(sys.argv[3]))),
        max(1.0, min(120.0, float(sys.argv[4]))),
        max(256, min(2048, int(sys.argv[5]))),
        max(1_000_000, min(100_000_000, int(sys.argv[6]))),
    )


if __name__ == "__main__":
    raise SystemExit(main())
