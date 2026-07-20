"""Resource staging tests for QQ attachments passed to the agent runtime."""
from __future__ import annotations

import asyncio
from io import BytesIO
import sys
from types import SimpleNamespace
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import qq_agent_bridge.resources as resources_module  # type: ignore
from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.resources import ResourceManager, format_resource_context  # type: ignore
from qq_agent_bridge.types import ChatEvent, ChatResource  # type: ignore
from qq_agent_bridge.whisper_runner import TranscriptionResult  # type: ignore


def make_cfg(workspace: Path) -> BridgeConfig:
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.default_workspace = str(workspace)
    cfg.resources.max_bytes = 1024
    return cfg


def configure_local_media_root(cfg: BridgeConfig, workspace: Path) -> Path:
    root = workspace / "onebot-media"
    root.mkdir()
    cfg.resources.local_media_roots = [str(root)]
    return root


def write_tiny_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(b"\x00\x00")


def tiny_wav_bytes() -> bytes:
    payload = BytesIO()
    with wave.open(payload, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(b"\x00\x00")
    return payload.getvalue()


def make_ev(resources: tuple[ChatResource, ...], mid: str = "m/1") -> ChatEvent:
    return ChatEvent(
        id=mid,
        platform="qq",
        chat_id="100",
        sender_id="200",
        is_group=True,
        mentioned_bot=True,
        text="/ask 看看附件",
        timestamp=1,
        resources=resources,
    )


class FakeTranscriber:
    def __init__(self, result: TranscriptionResult) -> None:
        self.result = result
        self.paths: list[Path] = []

    async def transcribe(self, path: Path) -> TranscriptionResult:
        self.paths.append(path)
        return self.result


def silk_voice() -> ChatResource:
    return ChatResource(
        kind="voice",
        url="https://qq.example/voice.silk",
        file_id="voice.silk",
        name="voice.silk",
        mime_type="audio/silk",
        duration_seconds=12,
    )


def wav_voice() -> ChatResource:
    return ChatResource(
        kind="voice",
        url="https://qq.example/voice.wav",
        name="voice.wav",
        mime_type="audio/wav",
        duration_seconds=12,
    )


def test_resource_manager_stages_downloadable_resource_under_workspace(tmp_path: Path) -> None:
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        assert url == "https://qq.example/cat.jpg"
        assert limit == 1024
        return b"image-bytes", "image/jpeg"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev((ChatResource(kind="image", url="https://qq.example/cat.jpg", name="cat.jpg"),))

    refs = asyncio.run(manager.prepare(ev))

    assert len(refs) == 1
    assert refs[0].kind == "image"
    assert refs[0].local_path is not None
    local = tmp_path / refs[0].local_path
    assert local.read_bytes() == b"image-bytes"
    assert local.name.endswith(".jpg")
    assert "cat.jpg" not in refs[0].local_path
    assert tmp_path in local.parents
    assert "downloads" in local.parts
    assert "qq-agent-bridge" in local.parts


def test_resource_manager_extracts_bounded_animation_frames_and_cleans_them(
    tmp_path: Path,
) -> None:
    extracted: list[Path] = []

    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        return b"GIF89a-animation", "image/gif"

    async def extract(source: Path, workspace: Path):
        extracted.append(source)
        frame_dir = source.parent / f"{source.stem}.frames"
        frame_dir.mkdir()
        frames = []
        for index in range(3):
            frame = frame_dir / f"frame-{index + 1:03d}.png"
            frame.write_bytes(f"frame-{index}".encode())
            frames.append(frame.relative_to(workspace).as_posix())
        return SimpleNamespace(
            status="ready",
            source_frame_count=12,
            duration_seconds=2.4,
            frame_paths=tuple(frames),
            frame_times=(0.0, 1.2, 2.4),
            error=None,
        )

    cfg = make_cfg(tmp_path)
    cfg.resources.animation_enabled = True
    cfg.resources.animation_max_frames = 6
    manager = ResourceManager(cfg, fetch=fetch, animation_extractor=extract)
    event = make_ev(
        (
            ChatResource(
                kind="image",
                url="https://qq.example/reaction.gif",
                name="reaction.gif",
                mime_type="image/gif",
            ),
        ),
        mid="animated-gif",
    )

    refs = asyncio.run(manager.prepare(event))

    assert len(refs) == 1
    assert extracted == [tmp_path / refs[0].local_path]
    assert refs[0].animation_status == "ready"
    assert refs[0].animation_frame_count == 12
    assert refs[0].animation_duration_seconds == 2.4
    assert len(refs[0].animation_frame_paths) == 3
    context = format_resource_context(refs)
    assert "animated image" in context
    assert "source_frames=12" in context
    assert "sampled frame 1" in context
    assert "t=1.200s" in context
    assert "首帧不能代表完整动图" in context

    manager.cleanup_prepared(refs)

    assert not any((tmp_path / path).exists() for path in refs[0].animation_frame_paths)


def test_animation_extraction_failure_keeps_original_and_marks_dynamic_evidence_unavailable(
    tmp_path: Path,
) -> None:
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        return b"RIFF-animation-webp", "image/webp"

    async def extract(source: Path, workspace: Path):
        return SimpleNamespace(
            status="unavailable",
            source_frame_count=None,
            duration_seconds=None,
            frame_paths=(),
            frame_times=(),
            error="ffprobe unavailable",
        )

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch, animation_extractor=extract)
    refs = asyncio.run(
        manager.prepare(
            make_ev(
                (
                    ChatResource(
                        kind="image",
                        url="https://qq.example/reaction.webp",
                        name="reaction.webp",
                    ),
                ),
                mid="animated-webp-failed",
            )
        )
    )

    assert refs[0].local_path is not None
    assert (tmp_path / refs[0].local_path).is_file()
    assert refs[0].animation_status == "unavailable"
    context = format_resource_context(refs)
    assert "dynamic evidence unavailable" in context
    assert "ffprobe unavailable" in context


def test_apng_chunk_is_detected_without_probing_regular_png_files(tmp_path: Path) -> None:
    png_signature = b"\x89PNG\r\n\x1a\n"
    apng = png_signature + (8).to_bytes(4, "big") + b"acTL" + b"\0" * 12
    static_png = png_signature + (0).to_bytes(4, "big") + b"IDAT" + b"\0" * 4
    extracted: list[str] = []

    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        return (apng if "animated" in url else static_png), "image/png"

    async def extract(source: Path, workspace: Path):
        extracted.append(source.name)
        return SimpleNamespace(
            status="ready",
            source_frame_count=4,
            duration_seconds=0.4,
            frame_paths=(),
            frame_times=(),
            error=None,
        )

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch, animation_extractor=extract)
    refs = asyncio.run(
        manager.prepare(
            make_ev(
                (
                    ChatResource(
                        kind="image",
                        url="https://qq.example/animated.png",
                        name="animated.png",
                    ),
                    ChatResource(
                        kind="image",
                        url="https://qq.example/static.png",
                        name="static.png",
                    ),
                ),
                mid="apng-detection",
            )
        )
    )

    assert len(refs) == 2
    assert len(extracted) == 1
    assert refs[0].animation_status == "ready"
    assert refs[1].animation_status is None


def test_resource_manager_keeps_unconverted_qq_voice_with_duration_context(tmp_path: Path) -> None:
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        raise AssertionError(f"raw Silk must not be downloaded: {url}")

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev(
        (
            silk_voice(),
        )
    )

    refs = asyncio.run(manager.prepare(ev))

    assert len(refs) == 1
    assert refs[0].kind == "voice"
    assert refs[0].duration_seconds == 12
    assert refs[0].local_path is None
    assert refs[0].transcript_status == "unavailable"
    context = format_resource_context(refs)
    assert "voice:" in context
    assert "duration=12s" in context
    assert "QQ voice limit=60s" in context
    assert "transcript: unavailable" in context


def test_voice_uses_napcat_wav_before_download_and_adds_verified_transcript(tmp_path: Path) -> None:
    async def go() -> None:
        resolved: list[str] = []
        transcriber = FakeTranscriber(TranscriptionResult("你好", "ok", "zh", None))

        async def record_url(resource: ChatResource) -> str | None:
            assert resource == silk_voice()
            return "https://qq.example/voice.wav"

        async def fetch(url: str, limit: int) -> tuple[bytes, str]:
            resolved.append(url)
            assert limit == 1024
            return tiny_wav_bytes(), "audio/wav"

        manager = ResourceManager(
            make_cfg(tmp_path),
            fetch=fetch,
            record_url=record_url,
            transcriber=transcriber,  # type: ignore[arg-type]
        )
        refs = await manager.prepare(make_ev((silk_voice(),), mid="voice-verified"))

        assert resolved == ["https://qq.example/voice.wav"]
        assert len(refs) == 1
        assert refs[0].local_path is not None
        assert refs[0].local_path.endswith(".wav")
        assert refs[0].transcript == "你好"
        assert refs[0].transcript_status == "verified"
        assert refs[0].transcript_language == "zh"
        assert transcriber.paths == [tmp_path / refs[0].local_path]
        assert "verified by local Whisper, language=zh" in format_resource_context(refs)

    asyncio.run(go())


def test_voice_rejects_invalid_remote_converted_wav(tmp_path: Path) -> None:
    async def go() -> None:
        transcriber = FakeTranscriber(TranscriptionResult("unexpected", "ok", "zh", None))

        async def record_url(resource: ChatResource) -> str | None:
            return "https://qq.example/voice.wav"

        async def fetch(url: str, limit: int) -> tuple[bytes, str]:
            return b"not a wav", "audio/wav"

        manager = ResourceManager(
            make_cfg(tmp_path),
            fetch=fetch,
            record_url=record_url,
            transcriber=transcriber,  # type: ignore[arg-type]
        )
        refs = await manager.prepare(make_ev((silk_voice(),), mid="voice-remote-invalid-wav"))

        assert refs[0].local_path is None
        assert refs[0].transcript_status == "unavailable"
        assert refs[0].transcript_error == "QQ voice conversion returned invalid WAV"
        assert transcriber.paths == []

    asyncio.run(go())


def test_voice_does_not_follow_path_swap_after_trust_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def go() -> None:
        cfg = make_cfg(tmp_path)
        trusted_root = configure_local_media_root(cfg, tmp_path)
        source = trusted_root / "onebot-record.wav"
        outside = tmp_path / "outside.wav"
        write_tiny_wav(source)
        write_tiny_wav(outside)

        async def record_url(resource: ChatResource) -> str | None:
            return str(source)

        async def fetch(url: str, limit: int) -> tuple[bytes, str]:
            raise AssertionError(f"local OneBot record path must not be fetched: {url}")

        original_open = resources_module.os.open
        swapped = False

        def swap_before_open(path: str | bytes | int, *args: object, **kwargs: object):
            nonlocal swapped
            if Path(path) == source and not swapped:
                source.unlink()
                source.symlink_to(outside)
                swapped = True
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(resources_module.os, "open", swap_before_open)
        transcriber = FakeTranscriber(TranscriptionResult("unexpected", "ok", "zh", None))
        manager = ResourceManager(
            cfg,
            fetch=fetch,
            record_url=record_url,
            transcriber=transcriber,  # type: ignore[arg-type]
        )

        refs = await manager.prepare(make_ev((silk_voice(),), mid="voice-path-race"))

        assert refs[0].local_path is None
        assert refs[0].transcript_status == "unavailable"
        assert refs[0].transcript_error == "QQ voice local file unavailable"
        assert transcriber.paths == []

    asyncio.run(go())


def test_voice_stages_local_record_path_without_fetch_and_adds_verified_transcript(tmp_path: Path) -> None:
    async def go() -> None:
        cfg = make_cfg(tmp_path)
        source = configure_local_media_root(cfg, tmp_path) / "onebot-record.wav"
        write_tiny_wav(source)
        transcriber = FakeTranscriber(TranscriptionResult("本地语音", "ok", "zh", None))

        async def record_url(resource: ChatResource) -> str | None:
            assert resource == silk_voice()
            return str(source)

        async def fetch(url: str, limit: int) -> tuple[bytes, str]:
            raise AssertionError(f"local OneBot record path must not be fetched: {url}")

        manager = ResourceManager(
            cfg,
            fetch=fetch,
            record_url=record_url,
            transcriber=transcriber,  # type: ignore[arg-type]
        )
        refs = await manager.prepare(make_ev((silk_voice(),), mid="voice-local-record"))

        assert len(refs) == 1
        assert refs[0].local_path is not None
        staged = tmp_path / refs[0].local_path
        assert staged.read_bytes() == source.read_bytes()
        assert staged.name.endswith(".wav")
        assert refs[0].transcript == "本地语音"
        assert refs[0].transcript_status == "verified"
        assert transcriber.paths == [staged]

    asyncio.run(go())


@pytest.mark.parametrize("payload", [b"RIFFlocal-wav", b"plain text"], ids=("fake-header", "text"))
def test_voice_rejects_invalid_wav_local_record_path(tmp_path: Path, payload: bytes) -> None:
    async def go() -> None:
        cfg = make_cfg(tmp_path)
        source = configure_local_media_root(cfg, tmp_path) / "onebot-record.wav"
        source.write_bytes(payload)

        async def record_url(resource: ChatResource) -> str | None:
            return str(source)

        async def fetch(url: str, limit: int) -> tuple[bytes, str]:
            raise AssertionError(f"local OneBot record path must not be fetched: {url}")

        transcriber = FakeTranscriber(TranscriptionResult("unexpected", "ok", "zh", None))
        manager = ResourceManager(
            cfg,
            fetch=fetch,
            record_url=record_url,
            transcriber=transcriber,  # type: ignore[arg-type]
        )

        refs = await manager.prepare(make_ev((silk_voice(),), mid="voice-local-invalid-wav"))

        assert len(refs) == 1
        assert refs[0].local_path is None
        assert refs[0].transcript_status == "unavailable"
        assert refs[0].transcript_error == "QQ voice local file unavailable"
        assert transcriber.paths == []

    asyncio.run(go())


@pytest.mark.parametrize("source_kind", ["missing", "directory"])
def test_voice_rejects_unavailable_local_record_path_safely(tmp_path: Path, source_kind: str) -> None:
    async def go() -> None:
        cfg = make_cfg(tmp_path)
        source = configure_local_media_root(cfg, tmp_path) / "onebot-record.wav"
        if source_kind == "directory":
            source.mkdir()

        async def record_url(resource: ChatResource) -> str | None:
            return str(source)

        async def fetch(url: str, limit: int) -> tuple[bytes, str]:
            raise AssertionError(f"local OneBot record path must not be fetched: {url}")

        transcriber = FakeTranscriber(TranscriptionResult("unexpected", "ok", "zh", None))
        manager = ResourceManager(
            cfg,
            fetch=fetch,
            record_url=record_url,
            transcriber=transcriber,  # type: ignore[arg-type]
        )

        refs = await manager.prepare(make_ev((silk_voice(),), mid=f"voice-local-{source_kind}"))

        assert len(refs) == 1
        assert refs[0].local_path is None
        assert refs[0].transcript_status == "unavailable"
        assert transcriber.paths == []

    asyncio.run(go())


@pytest.mark.parametrize(
    ("file_size", "max_total_bytes"),
    [(1025, 20 * 1024 * 1024), (9, 8)],
    ids=("max-bytes", "max-total-bytes"),
)
def test_voice_rejects_oversized_local_record_path_safely(
    tmp_path: Path, file_size: int, max_total_bytes: int
) -> None:
    async def go() -> None:
        cfg = make_cfg(tmp_path)
        source = configure_local_media_root(cfg, tmp_path) / "onebot-record.wav"
        source.write_bytes(b"x" * file_size)

        async def record_url(resource: ChatResource) -> str | None:
            return str(source)

        async def fetch(url: str, limit: int) -> tuple[bytes, str]:
            raise AssertionError(f"local OneBot record path must not be fetched: {url}")

        transcriber = FakeTranscriber(TranscriptionResult("unexpected", "ok", "zh", None))
        cfg.resources.max_total_bytes = max_total_bytes
        manager = ResourceManager(
            cfg,
            fetch=fetch,
            record_url=record_url,
            transcriber=transcriber,  # type: ignore[arg-type]
        )

        refs = await manager.prepare(make_ev((silk_voice(),), mid="voice-local-oversized"))

        assert len(refs) == 1
        assert refs[0].local_path is None
        assert refs[0].transcript_status == "unavailable"
        assert refs[0].transcript_error == "QQ voice download limit exceeded"
        assert transcriber.paths == []

    asyncio.run(go())


@pytest.mark.parametrize(
    "source_kind",
    [
        "empty-roots",
        "invalid-root-type",
        "outside-root",
        "symlink",
        "symlink-loop",
        "unknown-extension",
        "non-audio",
    ],
)
def test_voice_rejects_untrusted_local_record_path(tmp_path: Path, source_kind: str) -> None:
    async def go() -> None:
        cfg = make_cfg(tmp_path)
        trusted_root = configure_local_media_root(cfg, tmp_path)
        source = trusted_root / "onebot-record.wav"
        if source_kind == "empty-roots":
            cfg.resources.local_media_roots = []
            write_tiny_wav(source)
        elif source_kind == "invalid-root-type":
            cfg.resources.local_media_roots = "/"  # type: ignore[assignment]
            source = tmp_path / "outside.wav"
            write_tiny_wav(source)
        elif source_kind == "outside-root":
            source = tmp_path / "outside.wav"
            write_tiny_wav(source)
        elif source_kind == "symlink":
            target = tmp_path / "outside.wav"
            write_tiny_wav(target)
            source.symlink_to(target)
        elif source_kind == "symlink-loop":
            source.symlink_to(source)
        elif source_kind == "unknown-extension":
            source = trusted_root / "onebot-record.unknown"
            source.write_bytes(b"not audio")
        else:
            source = trusted_root / "onebot-record.txt"
            source.write_bytes(b"not audio")

        async def record_url(resource: ChatResource) -> str | None:
            return str(source)

        async def fetch(url: str, limit: int) -> tuple[bytes, str]:
            raise AssertionError(f"local OneBot record path must not be fetched: {url}")

        transcriber = FakeTranscriber(TranscriptionResult("unexpected", "ok", "zh", None))
        manager = ResourceManager(
            cfg,
            fetch=fetch,
            record_url=record_url,
            transcriber=transcriber,  # type: ignore[arg-type]
        )

        refs = await manager.prepare(make_ev((silk_voice(),), mid=f"voice-local-{source_kind}"))

        assert len(refs) == 1
        assert refs[0].local_path is None
        assert refs[0].transcript_status == "unavailable"
        assert refs[0].transcript_error == "QQ voice local file unavailable"
        assert transcriber.paths == []

    asyncio.run(go())


def test_file_id_voice_does_not_fetch_original_url_after_resolver_failure(tmp_path: Path) -> None:
    async def go() -> None:
        resolver_calls: list[str | None] = []
        fetch_calls: list[str] = []

        async def record_url(resource: ChatResource) -> str | None:
            resolver_calls.append(resource.file_id)
            return None

        async def fetch(url: str, limit: int) -> tuple[bytes, str]:
            fetch_calls.append(url)
            return b"decoded-looking-bytes", "audio/wav"

        resource = ChatResource(
            kind="voice",
            file_id="voice-token",
            url="https://qq.example/voice-token",
            name="voice",
            duration_seconds=12,
        )
        manager = ResourceManager(
            make_cfg(tmp_path),
            fetch=fetch,
            record_url=record_url,
        )

        refs = await manager.prepare(make_ev((resource,), mid="voice-resolver-failed"))

        assert resolver_calls == ["voice-token"]
        assert fetch_calls == []
        assert len(refs) == 1
        assert refs[0].kind == "voice"
        assert refs[0].local_path is None
        assert refs[0].transcript_status == "unavailable"

    asyncio.run(go())


def test_failed_transcription_keeps_audio_and_marks_unavailable(tmp_path: Path) -> None:
    async def go() -> None:
        async def fetch(url: str, limit: int) -> tuple[bytes, str]:
            assert url == "https://qq.example/voice.wav"
            return b"wav", "audio/wav"

        manager = ResourceManager(
            make_cfg(tmp_path),
            fetch=fetch,
            transcriber=FakeTranscriber(
                TranscriptionResult(None, "unavailable", None, "model missing")
            ),  # type: ignore[arg-type]
        )
        refs = await manager.prepare(make_ev((wav_voice(),), mid="voice-unavailable"))

        assert len(refs) == 1
        assert refs[0].local_path is not None
        assert refs[0].transcript is None
        assert refs[0].transcript_status == "unavailable"
        assert refs[0].transcript_error == "model missing"
        assert "model missing" in format_resource_context(refs)

    asyncio.run(go())


def test_resource_manager_keeps_plain_url_without_downloading(tmp_path: Path) -> None:
    called = False

    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        nonlocal called
        called = True
        return b"", "text/plain"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev((ChatResource(kind="url", url="https://example.com/page", name="page"),))

    refs = asyncio.run(manager.prepare(ev))

    assert not called
    assert len(refs) == 1
    assert refs[0].kind == "url"
    assert refs[0].url == "https://example.com/page"
    assert refs[0].local_path is None


def test_default_http_fetch_rejects_loopback_targets(tmp_path: Path) -> None:
    async def go() -> None:
        manager = ResourceManager(make_cfg(tmp_path))
        with pytest.raises(ValueError, match="private network"):
            await manager._fetch_http("http://127.0.0.1:8080/metadata", 1024)

    asyncio.run(go())


def test_resource_manager_sanitizes_names_and_limits_count(tmp_path: Path) -> None:
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        return b"x", "application/octet-stream"

    cfg = make_cfg(tmp_path)
    cfg.resources.max_items = 1
    manager = ResourceManager(cfg, fetch=fetch)
    ev = make_ev(
        (
            ChatResource(kind="file", url="https://qq.example/1", name="../../secret.txt"),
            ChatResource(kind="file", url="https://qq.example/2", name="second.txt"),
        )
    )

    refs = asyncio.run(manager.prepare(ev))

    assert len(refs) == 1
    assert refs[0].local_path is not None
    assert Path(refs[0].local_path).name != "secret.txt"
    assert ".." not in refs[0].local_path


def test_resource_manager_does_not_pass_unstaged_attachment_urls(tmp_path: Path) -> None:
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        raise RuntimeError("download failed")

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev((ChatResource(kind="image", url="https://qq.example/private-image", name="cat.jpg"),))

    refs = asyncio.run(manager.prepare(ev))

    assert refs == ()


def test_resource_manager_formats_forward_chat_record_context_without_downloading(tmp_path: Path) -> None:
    called = False

    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        nonlocal called
        called = True
        return b"", "text/plain"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev(
        (
            ChatResource(
                kind="forward",
                file_id="forward-msg-1",
                name="群聊的聊天记录",
                raw_data={
                    "messages": [
                        {
                            "sender_id": "222",
                            "sender_name": "Alice",
                            "text": "第一条 https://example.com/a",
                        },
                        {
                            "sender_id": "333",
                            "sender_name": "Bob",
                            "text": "",
                            "resources": [
                                {
                                    "kind": "image",
                                    "name": "pic.jpg",
                                    "url": "https://qq.example/pic.jpg",
                                }
                            ],
                        },
                    ]
                },
            ),
        )
    )

    refs = asyncio.run(manager.prepare(ev))
    context = format_resource_context(refs)

    assert not called
    assert len(refs) == 1
    assert refs[0].kind == "forward"
    assert "QQ批量转发：群聊的聊天记录" in context
    assert "Alice(222): 第一条 https://example.com/a" in context
    assert "Bob(333): [image] pic.jpg https://qq.example/pic.jpg" in context


def test_voice_without_transcriber_stages_audio_and_degrades_gracefully(tmp_path: Path) -> None:
    """Voice downloads without whisper binary — audio stays staged, no crash, no hallucinated transcript."""
    async def go() -> None:
        async def fetch(url: str, limit: int) -> tuple[bytes, str]:
            assert url == "https://qq.example/voice.wav"
            return tiny_wav_bytes(), "audio/wav"

        manager = ResourceManager(make_cfg(tmp_path), fetch=fetch, transcriber=None)
        refs = await manager.prepare(make_ev((wav_voice(),), mid="voice-no-whisper"))

        assert len(refs) == 1
        assert refs[0].local_path is not None, "voice file should still be staged"
        assert refs[0].transcript is None, "no transcript without transcriber"
        assert refs[0].transcript_status is None, "no transcription attempted when transcriber is missing"
        context = format_resource_context(refs)
        assert "verified" not in context, "should not claim verified transcript"
        assert "transcript:" not in context, "should not emit misleading transcript line without transcriber"

    asyncio.run(go())


def test_forward_chat_record_context_truncates_long_user_text(tmp_path: Path) -> None:
    manager = ResourceManager(make_cfg(tmp_path))
    long_text = "很长" * 2000
    ev = make_ev(
        (
            ChatResource(
                kind="forward",
                name="超长聊天记录",
                raw_data={
                    "messages": [
                        {
                            "sender_id": "222",
                            "sender_name": "Alice",
                            "text": long_text,
                        }
                    ]
                },
            ),
        )
    )

    refs = asyncio.run(manager.prepare(ev))
    context = format_resource_context(refs)

    assert len(context) < 1200
    assert long_text not in context
    assert "..." in context


# ── resource pipeline e2e tests ──────────────────────────────────────────────


def test_image_with_valid_url_is_downloaded_and_appears_in_context(
    tmp_path: Path,
) -> None:
    """Image resource with valid URL is fetched, staged, and referenced in context."""
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        assert url == "https://qq.example/photo.jpg"
        assert limit == 1024
        return b"tiny-image-payload", "image/jpeg"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev(
        (ChatResource(kind="image", url="https://qq.example/photo.jpg", name="photo.jpg"),)
    )

    refs = asyncio.run(manager.prepare(ev))

    assert len(refs) == 1
    assert refs[0].kind == "image"
    assert refs[0].local_path is not None
    assert refs[0].local_path.endswith(".jpg")
    local = tmp_path / refs[0].local_path
    assert local.read_bytes() == b"tiny-image-payload"
    assert local.exists()

    context = format_resource_context(refs)
    assert "- image:" in context
    assert refs[0].local_path in context


def test_image_with_empty_url_is_silently_skipped(tmp_path: Path) -> None:
    """Image resource with url="" is not treated as an error — prepare skips it."""
    fetch_called = False

    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        nonlocal fetch_called
        fetch_called = True
        return b"", "image/jpeg"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev(
        (ChatResource(kind="image", url="", name="empty.jpg"),)
    )

    refs = asyncio.run(manager.prepare(ev))

    assert refs == ()
    assert not fetch_called
    # format_resource_context must not crash on an empty tuple
    assert format_resource_context(()) == ""
    assert format_resource_context(refs) == ""


def test_file_resource_is_downloaded_and_staged(tmp_path: Path) -> None:
    """File resource is downloaded, staged under workspace, and appears in context."""
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        assert url == "https://qq.example/doc.txt"
        return b"file-payload", "text/plain"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev(
        (ChatResource(kind="file", url="https://qq.example/doc.txt", name="doc.txt"),)
    )

    refs = asyncio.run(manager.prepare(ev))

    assert len(refs) == 1
    assert refs[0].kind == "file"
    assert refs[0].local_path is not None
    local = tmp_path / refs[0].local_path
    assert local.read_bytes() == b"file-payload"
    assert local.exists()

    context = format_resource_context(refs)
    assert "- file:" in context
    assert refs[0].local_path in context


def test_url_resource_is_passed_through_without_download(tmp_path: Path) -> None:
    """URL resource keeps its original url; fetch is never called."""
    fetch_called = False

    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        nonlocal fetch_called
        fetch_called = True
        return b"", "text/plain"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev(
        (ChatResource(kind="url", url="https://example.com/article", name="article"),)
    )

    refs = asyncio.run(manager.prepare(ev))

    assert not fetch_called
    assert len(refs) == 1
    assert refs[0].kind == "url"
    assert refs[0].url == "https://example.com/article"
    assert refs[0].local_path is None

    context = format_resource_context(refs)
    assert "- url:" in context
    assert "https://example.com/article" in context


def test_forward_resource_text_is_captured(tmp_path: Path) -> None:
    """Forward resource text is captured without any download."""
    manager = ResourceManager(make_cfg(tmp_path))
    ev = make_ev(
        (
            ChatResource(
                kind="forward",
                name="群聊的聊天记录",
                raw_data={
                    "messages": [
                        {
                            "sender_id": "111",
                            "sender_name": "Alice",
                            "text": "第一条消息",
                        },
                        {
                            "sender_id": "222",
                            "sender_name": "Bob",
                            "text": "第二条消息 https://example.com/link",
                        },
                    ]
                },
            ),
        )
    )

    refs = asyncio.run(manager.prepare(ev))

    assert len(refs) == 1
    assert refs[0].kind == "forward"
    assert refs[0].local_path is None
    assert refs[0].text is not None
    assert "QQ批量转发" in refs[0].text
    assert "Alice" in refs[0].text
    assert "第一条消息" in refs[0].text

    context = format_resource_context(refs)
    assert "Alice" in context
    assert "第一条消息" in context
    assert "Bob" in context
    assert "https://example.com/link" in context


def test_multiple_resources_are_all_prepared_not_just_the_first(
    tmp_path: Path,
) -> None:
    """Event with image + file + url — all three appear in prepared results."""
    fetch_count = 0

    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        nonlocal fetch_count
        fetch_count += 1
        if "photo" in url:
            return b"image-payload", "image/jpeg"
        if "doc" in url:
            return b"file-payload", "text/plain"
        raise AssertionError(f"unexpected fetch: {url}")

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev(
        (
            ChatResource(
                kind="image", url="https://qq.example/photo.jpg", name="photo.jpg"
            ),
            ChatResource(
                kind="file", url="https://qq.example/doc.txt", name="doc.txt"
            ),
            ChatResource(
                kind="url", url="https://example.com/link", name="link"
            ),
        )
    )

    refs = asyncio.run(manager.prepare(ev))

    assert len(refs) == 3
    assert fetch_count == 2  # image + file downloaded; url skipped

    kinds = [r.kind for r in refs]
    assert "image" in kinds
    assert "file" in kinds
    assert "url" in kinds

    # Image was downloaded and staged
    image_ref = next(r for r in refs if r.kind == "image")
    assert image_ref.local_path is not None
    assert (tmp_path / image_ref.local_path).read_bytes() == b"image-payload"

    # File was downloaded and staged
    file_ref = next(r for r in refs if r.kind == "file")
    assert file_ref.local_path is not None
    assert (tmp_path / file_ref.local_path).read_bytes() == b"file-payload"

    # URL was passed through
    url_ref = next(r for r in refs if r.kind == "url")
    assert url_ref.url == "https://example.com/link"
    assert url_ref.local_path is None

    context = format_resource_context(refs)
    assert "- image:" in context
    assert "- file:" in context
    assert "- url:" in context


def test_resource_download_failure_does_not_block_remaining_resources(
    tmp_path: Path,
) -> None:
    """A failed download is silently skipped; subsequent valid resources still process."""
    async def fetch(url: str, limit: int) -> tuple[bytes, str]:
        if "unreachable" in url:
            raise RuntimeError("connection refused")
        return b"good-payload", "image/jpeg"

    manager = ResourceManager(make_cfg(tmp_path), fetch=fetch)
    ev = make_ev(
        (
            ChatResource(
                kind="image", url="https://unreachable.example/bad.jpg", name="bad.jpg"
            ),
            ChatResource(
                kind="image", url="https://qq.example/good.jpg", name="good.jpg"
            ),
        )
    )

    refs = asyncio.run(manager.prepare(ev))

    # Only the second (valid) resource should survive
    assert len(refs) == 1
    assert refs[0].kind == "image"
    assert refs[0].local_path is not None
    local = tmp_path / refs[0].local_path
    assert local.read_bytes() == b"good-payload"

    # The failed resource must not leak into context
    context = format_resource_context(refs)
    assert "bad.jpg" not in context
    assert "- image:" in context
