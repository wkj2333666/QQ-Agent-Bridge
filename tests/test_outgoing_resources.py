"""Outgoing resource directive parsing tests."""
from __future__ import annotations

import sys
import shutil
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.outgoing_resources import (  # type: ignore
    collect_outgoing_resources,
    inspect_outgoing_resources,
)


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


def test_recovers_existing_file_when_prose_is_glued_to_directive_path(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    report = outbox / "视频总结.md"
    report.write_text("summary", encoding="utf-8")
    rel = report.relative_to(tmp_path).as_posix()

    result = inspect_outgoing_resources(
        f"文件发你啦\nQQBOT_SEND_FILE: send-token {rel}主人，已经整理好了",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.clean_text == "文件发你啦"
    assert len(result.resources) == 1
    assert result.resources[0].path.read_text(encoding="utf-8") == "summary"
    assert result.warnings == ()
    assert (result.attempted, result.unresolved, result.recovered) == (1, 0, 1)


def test_structured_inspection_reports_unresolved_missing_directive(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    result = inspect_outgoing_resources(
        "QQBOT_SEND_FILE: send-token downloads/qq-agent-bridge/outgoing/job-1/missing.pdf",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.attempted == 1
    assert result.unresolved == 1
    assert result.warnings == ("无法发送资源：文件不存在或不是普通文件",)


def test_recovers_unique_top_level_outbox_file_after_broken_directive(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    report = outbox / "report.pdf"
    missing = outbox / "missing.pdf"
    report.write_bytes(b"pdf")

    result = inspect_outgoing_resources(
        f"QQBOT_SEND_FILE: send-token {missing.relative_to(tmp_path)}",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert len(result.resources) == 1
    assert result.resources[0].path.read_bytes() == b"pdf"
    assert result.warnings == ()
    assert (result.unresolved, result.recovered) == (0, 1)


def test_unique_discovery_does_not_recover_after_token_mismatch(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    (outbox / "report.pdf").write_bytes(b"pdf")

    result = inspect_outgoing_resources(
        "QQBOT_SEND_FILE: wrong-token missing.pdf",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.unresolved == 1
    assert result.warnings == ("已拒绝发送资源：令牌不匹配",)


def test_unique_discovery_does_not_recover_outbox_external_workspace_file(
    tmp_path: Path,
) -> None:
    outbox = make_outbox(tmp_path)
    (outbox / "report.pdf").write_bytes(b"pdf")
    outside_outbox = tmp_path / "elsewhere.pdf"
    outside_outbox.write_bytes(b"elsewhere")

    result = inspect_outgoing_resources(
        f"QQBOT_SEND_FILE: send-token {outside_outbox.relative_to(tmp_path)}",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.unresolved == 1
    assert result.warnings == ("已拒绝发送资源：路径不在本次任务输出目录内",)


def test_unique_discovery_does_not_recover_mixed_missing_and_token_mismatch(
    tmp_path: Path,
) -> None:
    outbox = make_outbox(tmp_path)
    missing = outbox / "missing.pdf"
    (outbox / "report.pdf").write_bytes(b"pdf")

    result = inspect_outgoing_resources(
        "\n".join(
            (
                f"QQBOT_SEND_FILE: send-token {missing.relative_to(tmp_path)}",
                "QQBOT_SEND_FILE: wrong-token missing.pdf",
            )
        ),
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.unresolved == 2
    assert result.warnings == (
        "无法发送资源：文件不存在或不是普通文件",
        "已拒绝发送资源：令牌不匹配",
    )


def test_does_not_guess_when_multiple_top_level_files_exist(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    (outbox / "notes.md").write_text("notes", encoding="utf-8")
    (outbox / "report.pdf").write_bytes(b"pdf")
    missing = outbox / "missing.pdf"

    result = inspect_outgoing_resources(
        f"QQBOT_SEND_FILE: send-token {missing.relative_to(tmp_path)}",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.unresolved == 1
    assert result.warnings == ("无法发送资源：文件不存在或不是普通文件",)


def test_unique_discovery_ignores_nested_temporary_files(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    (outbox / "tmp").mkdir()
    (outbox / "tmp" / "frame.png").write_bytes(b"frame")
    missing = outbox / "missing.pdf"

    result = inspect_outgoing_resources(
        f"QQBOT_SEND_FILE: send-token {missing.relative_to(tmp_path)}",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.warnings == ("无法发送资源：文件不存在或不是普通文件",)


def test_recovers_unique_top_level_file_when_agent_omits_directive(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    report = outbox / "report.pdf"
    report.write_bytes(b"pdf")
    result = inspect_outgoing_resources(
        "文件已经整理好",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.clean_text == "文件已经整理好"
    assert len(result.resources) == 1
    assert result.resources[0].path.read_bytes() == b"pdf"
    assert result.attempted == 0
    assert result.recovered == 1


def test_text_only_output_without_outbox_file_stays_text_only(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    result = inspect_outgoing_resources(
        "普通文本回答",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.clean_text == "普通文本回答"
    assert result.resources == ()
    assert result.recovered == 0


def test_unique_discovery_rejects_symlink(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    target = tmp_path / "report.pdf"
    target.write_bytes(b"pdf")
    (outbox / "report.pdf").symlink_to(target)

    result = inspect_outgoing_resources(
        "普通文本回答",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.recovered == 0


def test_unique_discovery_rejects_hard_link(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    target = tmp_path / "report.pdf"
    target.write_bytes(b"pdf")
    (outbox / "report.pdf").hardlink_to(target)

    result = inspect_outgoing_resources(
        "普通文本回答",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.recovered == 0


def test_unique_discovery_ignores_hidden_file(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    (outbox / ".report.pdf").write_bytes(b"pdf")

    result = inspect_outgoing_resources(
        "普通文本回答",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.recovered == 0


def test_unique_discovery_rejects_oversized_file(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    (outbox / "report.pdf").write_bytes(b"pdf")
    cfg = make_cfg(tmp_path)
    cfg.resources.max_bytes = 2

    result = inspect_outgoing_resources(
        "普通文本回答",
        cfg,
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.resources == ()
    assert result.recovered == 0


def test_unique_discovery_classifies_images_and_audio_as_files(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    image = outbox / "plot.png"
    image.write_bytes(b"png")
    result = inspect_outgoing_resources(
        "普通文本回答",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert [resource.kind for resource in result.resources] == ["image"]

    second_outbox = make_outbox(tmp_path, job_id="job-2")
    audio = second_outbox / "music.mp3"
    audio.write_bytes(b"mp3")
    second_result = inspect_outgoing_resources(
        "普通文本回答",
        make_cfg(tmp_path),
        outbox_dir=second_outbox,
        token="send-token",
        job_id="job-2",
    )

    assert [resource.kind for resource in second_result.resources] == ["file"]


def test_unique_discovery_can_be_disabled(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    (outbox / "report.pdf").write_bytes(b"pdf")

    result = inspect_outgoing_resources(
        "普通文本回答",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
        discover_unique=False,
    )

    assert result.resources == ()
    assert result.recovered == 0


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


def test_duplicate_directives_stage_and_count_resource_once(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    report = outbox / "report.pdf"
    report.write_bytes(b"pdf")
    cfg = make_cfg(tmp_path)
    cfg.resources.max_items = 1
    directive = f"QQBOT_SEND_FILE: send-token {report.relative_to(tmp_path)}"

    result = inspect_outgoing_resources(
        f"{directive}\n{directive}",
        cfg,
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert result.attempted == 2
    assert len(result.resources) == 1
    assert result.resources[0].path.read_bytes() == b"pdf"
    assert result.warnings == ()
    assert len(list(result.resources[0].path.parent.iterdir())) == 1


def test_collects_animated_image_without_changing_its_format(tmp_path: Path) -> None:
    outbox = make_outbox(tmp_path)
    animation = outbox / "reaction.gif"
    animation.write_bytes(b"GIF89a-animated")

    text, resources, warnings = collect_outgoing_resources(
        f"QQBOT_SEND_IMAGE: send-token {animation.relative_to(tmp_path)}",
        make_cfg(tmp_path),
        outbox_dir=outbox,
        token="send-token",
        job_id="job-1",
    )

    assert text == ""
    assert warnings == []
    assert len(resources) == 1
    assert resources[0].kind == "image"
    assert resources[0].path.suffix == ".gif"
    assert resources[0].path.read_bytes() == animation.read_bytes()


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
