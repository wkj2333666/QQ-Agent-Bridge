"""Outgoing resource directive parsing tests."""
from __future__ import annotations

import sys
import shutil
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.outgoing_resources import collect_outgoing_resources  # type: ignore


def make_cfg(workspace: Path) -> BridgeConfig:
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.default_workspace = str(workspace)
    return cfg


def make_outbox(workspace: Path, job_id: str = "job-1") -> Path:
    outbox = workspace / "downloads" / "qq-agent-bridge" / "outgoing" / job_id
    outbox.mkdir(parents=True)
    return outbox


def write_wav(path: Path, duration_seconds: int, sample_rate: int = 8000) -> None:
    frames = b"\0\0" * sample_rate * duration_seconds
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)


def test_collects_image_and_file_directives_inside_workspace(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    image = outbox / "plot.png"
    report = outbox / "report.pdf"
    image.write_bytes(b"png")
    report.write_bytes(b"pdf")

    text, resources, warnings = collect_outgoing_resources(
        (
            "整理好了\n"
            f"QQBOT_SEND_IMAGE: send-token {image.relative_to(tmp_path)}\n"
            f"QQBOT_SEND_FILE: send-token {report.relative_to(tmp_path)}\n"
        ),
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == "整理好了"
    assert warnings == []
    assert [res.kind for res in resources] == ["image", "file"]
    assert resources[0].path.name.endswith("plot.png")
    assert resources[1].path.name.endswith("report.pdf")
    assert all("sending" in res.path.parts for res in resources)
    assert resources[0].path != image
    assert resources[0].path.read_bytes() == b"png"
    assert resources[1].path.read_bytes() == b"pdf"


def test_collects_voice_directive_with_duration_under_qq_limit(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    voice = outbox / "reply.wav"
    write_wav(voice, 2)

    text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_VOICE: send-token {voice.relative_to(tmp_path)} duration=59",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert warnings == []
    assert len(resources) == 1
    assert resources[0].kind == "voice"
    assert resources[0].duration_seconds == 2
    assert resources[0].path.read_bytes() == voice.read_bytes()


def test_rejects_voice_directive_when_actual_duration_exceeds_qq_limit(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    voice = outbox / "too-long.wav"
    write_wav(voice, 61)

    text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_VOICE: send-token {voice.relative_to(tmp_path)} duration=12",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert resources == ()
    assert warnings == ["无法发送QQ语音：实际时长超过60秒限制"]


def test_rejects_voice_directive_over_qq_duration_limit(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    voice = outbox / "too-long.silk"
    voice.write_bytes(b"silk")

    text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_VOICE: send-token {voice.relative_to(tmp_path)} duration=61",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert resources == ()
    assert warnings == ["无法发送QQ语音：时长超过60秒限制"]


def test_rejects_voice_directive_without_duration_metadata(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    voice = outbox / "unknown.silk"
    voice.write_bytes(b"silk")

    text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_VOICE: send-token {voice.relative_to(tmp_path)}",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert resources == ()
    assert warnings == ["无法发送QQ语音：缺少时长元数据，需确认不超过60秒"]


def test_generic_audio_directive_is_sent_as_file(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    audio = outbox / "music.mp3"
    audio.write_bytes(b"mp3")

    _text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_AUDIO: send-token {audio.relative_to(tmp_path)}",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert warnings == []
    assert len(resources) == 1
    assert resources[0].kind == "file"
    assert resources[0].path.read_bytes() == b"mp3"


def test_preserves_extension_for_non_ascii_outgoing_filename(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    report = outbox / "张三.xlsx"
    report.write_bytes(b"xlsx")

    _text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_FILE: send-token {report.relative_to(tmp_path)}",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert warnings == []
    assert len(resources) == 1
    assert resources[0].path.suffix == ".xlsx"
    assert resources[0].path.read_bytes() == b"xlsx"


def test_rejects_outgoing_resource_paths_outside_workspace(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_FILE: send-token {outside}\nQQBOT_SEND_IMAGE: send-token ../outside.txt",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert resources == ()
    assert len(warnings) == 2
    assert all("工作区" in warning for warning in warnings)


def test_rejects_missing_outgoing_resource(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    text, resources, warnings = collect_outgoing_resources(
        "QQBOT_SEND_FILE: send-token downloads/qq-agent-bridge/outgoing/job-1/missing.txt",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert resources == ()
    assert warnings == ["无法发送资源：文件不存在或不是普通文件"]


def test_rejects_outgoing_resources_when_workspace_is_not_allowed(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    report = outbox / "report.pdf"
    report.write_bytes(b"pdf")
    cfg = make_cfg(tmp_path)
    cfg.workspaces = {str(tmp_path): False}

    text, resources, warnings = collect_outgoing_resources(
        f"正文\nQQBOT_SEND_FILE: send-token {report.relative_to(tmp_path)}",
        cfg,
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == "正文"
    assert resources == ()
    assert warnings == ["已拒绝发送资源：工作区未授权"]


def test_rejects_symlink_outgoing_resource_that_escapes_workspace(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    outside = tmp_path.parent / "secret-report.pdf"
    outside.write_bytes(b"secret")
    link = outbox / "link.pdf"
    link.symlink_to(outside)

    text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_FILE: send-token {link.relative_to(tmp_path)}",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert resources == ()
    assert warnings == ["已拒绝发送资源：路径不在工作区内"]


def test_rejects_existing_workspace_file_outside_current_outbox(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    secret = tmp_path / "config.yaml"
    secret.write_text("token: nope", encoding="utf-8")

    text, resources, warnings = collect_outgoing_resources(
        "QQBOT_SEND_FILE: send-token config.yaml",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert resources == ()
    assert warnings == ["已拒绝发送资源：路径不在本次任务输出目录内"]


def test_rejects_outbox_that_is_not_inside_workspace(tmp_path: Path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"pdf")
    outside_outbox = tmp_path.parent / "outside-outbox"
    outside_outbox.mkdir(exist_ok=True)

    text, resources, warnings = collect_outgoing_resources(
        "QQBOT_SEND_FILE: send-token report.pdf",
        make_cfg(tmp_path),
        outbox_dir=outside_outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert resources == ()
    assert warnings == ["已拒绝发送资源：输出目录未授权"]


def test_rejects_outbox_replaced_by_symlink_after_job_start(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    started = outbox.lstat()
    shutil.rmtree(outbox)
    outbox.symlink_to(tmp_path, target_is_directory=True)
    secret = tmp_path / "config.yaml"
    secret.write_text("token: nope", encoding="utf-8")

    text, resources, warnings = collect_outgoing_resources(
        "QQBOT_SEND_FILE: send-token config.yaml",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
        expected_outbox=(started.st_dev, started.st_ino),
    )

    assert text == ""
    assert resources == ()
    assert warnings == ["已拒绝发送资源：输出目录状态变化"]


def test_rejects_hardlink_inside_outbox(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    secret = tmp_path / "config.yaml"
    secret.write_text("token: nope", encoding="utf-8")
    link = outbox / "report.pdf"
    link.hardlink_to(secret)

    text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_FILE: send-token {link.relative_to(tmp_path)}",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert resources == ()
    assert warnings == ["无法发送资源：文件不是本次任务生成的独立文件"]


def test_rejects_outgoing_resource_with_wrong_token(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    report = outbox / "report.pdf"
    report.write_bytes(b"pdf")

    text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_FILE: wrong-token {report.relative_to(tmp_path)}",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert resources == ()
    assert warnings == ["已拒绝发送资源：令牌不匹配"]


def test_rejects_outgoing_resource_over_size_limit(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    report = outbox / "report.pdf"
    report.write_bytes(b"1234")
    cfg = make_cfg(tmp_path)
    cfg.resources.max_bytes = 3

    text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_FILE: send-token {report.relative_to(tmp_path)}",
        cfg,
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert resources == ()
    assert warnings == ["无法发送资源：文件超过大小限制"]


def test_limits_outgoing_resource_count(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    first = outbox / "first.pdf"
    second = outbox / "second.pdf"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    cfg = make_cfg(tmp_path)
    cfg.resources.max_items = 1

    text, resources, warnings = collect_outgoing_resources(
        (
            f"QQBOT_SEND_FILE: send-token {first.relative_to(tmp_path)}\n"
            f"QQBOT_SEND_FILE: send-token {second.relative_to(tmp_path)}"
        ),
        cfg,
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert [(res.kind, res.path.read_bytes()) for res in resources] == [("file", b"1")]
    assert warnings == ["无法发送资源：超过发送数量限制"]
