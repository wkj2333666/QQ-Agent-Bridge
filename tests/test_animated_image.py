"""Integration coverage for bounded animated-image sampling."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.animated_image import AnimatedImageExtractor  # type: ignore
from qq_agent_bridge.config import ResourcesConfig  # type: ignore


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg and ffprobe are required for the integration test",
)
def test_real_ffmpeg_extracts_bounded_frames_from_gif(tmp_path: Path) -> None:
    source = tmp_path / "animated.gif"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=32x32:rate=5:duration=1",
            "-loop",
            "0",
            str(source),
        ],
        check=True,
        timeout=10,
    )
    cfg = ResourcesConfig(
        animation_max_frames=3,
        animation_max_duration_seconds=2,
        animation_max_dimension=64,
        animation_timeout_seconds=10,
    )

    result = asyncio.run(AnimatedImageExtractor(cfg).extract(source, tmp_path))

    assert result.status == "ready"
    assert result.source_frame_count == 5
    assert result.duration_seconds == pytest.approx(1.0)
    assert len(result.frame_paths) == 3
    assert len(result.frame_times) == 3
    for relative in result.frame_paths:
        frame = tmp_path / relative
        assert frame.is_file()
        assert frame.stat().st_size > 0
        with Image.open(frame) as sampled:
            assert max(sampled.size) <= 64


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg and ffprobe are required for the integration test",
)
def test_real_ffmpeg_extracts_apng_even_when_named_png(tmp_path: Path) -> None:
    source = tmp_path / "animated.png"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=32x32:rate=5:duration=1",
            "-plays",
            "0",
            "-f",
            "apng",
            str(source),
        ],
        check=True,
        timeout=10,
    )
    cfg = ResourcesConfig(animation_max_frames=4, animation_timeout_seconds=10)

    result = asyncio.run(AnimatedImageExtractor(cfg).extract(source, tmp_path))

    assert result.status == "ready"
    assert result.source_frame_count == 5
    assert 2 <= len(result.frame_paths) <= 4


def test_pillow_fallback_reads_animated_webp_in_temporal_order(tmp_path: Path) -> None:
    source = tmp_path / "animated.webp"
    source_frames = [Image.new("RGB", (24, 24), color) for color in ("red", "blue", "green")]
    source_frames[0].save(
        source,
        save_all=True,
        append_images=source_frames[1:],
        duration=100,
        loop=0,
        lossless=True,
    )
    cfg = ResourcesConfig(
        animation_max_frames=3,
        animation_max_duration_seconds=2,
        animation_max_dimension=64,
        animation_timeout_seconds=10,
    )

    result = asyncio.run(AnimatedImageExtractor(cfg).extract(source, tmp_path))

    assert result.status == "ready"
    assert result.source_frame_count == 3
    assert result.duration_seconds == pytest.approx(0.3)
    assert result.frame_times == pytest.approx((0.0, 0.1, 0.2))
    pixels = []
    for relative in result.frame_paths:
        with Image.open(tmp_path / relative) as frame:
            pixels.append(frame.convert("RGB").getpixel((12, 12)))
    assert pixels[0][0] > pixels[0][1] + pixels[0][2]
    assert pixels[1][2] > pixels[1][0] + pixels[1][1]
    assert pixels[2][1] > pixels[2][0] + pixels[2][2]


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg and ffprobe are required for the integration test",
)
def test_rejects_animation_whose_source_canvas_exceeds_pixel_limit(tmp_path: Path) -> None:
    source = tmp_path / "oversized.gif"
    frames = [Image.new("P", (1200, 1200), color=index) for index in (1, 2)]
    frames[0].save(
        source,
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    cfg = ResourcesConfig(
        animation_max_source_pixels=1_000_000,
        animation_timeout_seconds=10,
    )

    result = asyncio.run(AnimatedImageExtractor(cfg).extract(source, tmp_path))

    assert result.status == "unavailable"
    assert result.error == "animation dimensions exceed safety limit"
    assert result.frame_paths == ()
