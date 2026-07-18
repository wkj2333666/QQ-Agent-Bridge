"""Content contract for the lazy visual-media runtime reference."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "skills" / "qq-agent-runtime" / "references" / "visual-media.md"


def _reference_text() -> str:
    return REFERENCE.read_text(encoding="utf-8")


def _require_any(text: str, alternatives: tuple[str, ...], requirement: str) -> None:
    assert any(item in text for item in alternatives), (
        f"visual-media reference must cover {requirement}; "
        f"expected one of {alternatives!r}"
    )


def _require_related_phrases(
    text: str,
    first: tuple[str, ...],
    relation: tuple[str, ...],
    second: tuple[str, ...],
    requirement: str,
) -> None:
    first_pattern = "|".join(map(re.escape, first))
    relation_pattern = "|".join(map(re.escape, relation))
    second_pattern = "|".join(map(re.escape, second))
    within_sentence = r"[^。！？；\n]{0,80}"
    pattern = (
        rf"(?:{first_pattern}){within_sentence}(?:{relation_pattern})"
        rf"{within_sentence}(?:{second_pattern})"
    )
    assert re.search(pattern, text), (
        f"visual-media reference must connect {requirement} in one instruction; "
        f"expected related wording from {first!r}, {relation!r}, and {second!r}"
    )


def _require_ordered_stages(
    text: str, stages: tuple[tuple[str, tuple[str, ...]], ...]
) -> int:
    position = 0
    for requirement, alternatives in stages:
        matches = [text.find(item, position) for item in alternatives if item in text[position:]]
        assert matches, (
            f"visual-media reference must cover {requirement} after the preceding stage; "
            f"expected one of {alternatives!r}"
        )
        position = min(matches)
    return position


def test_visual_media_reference_defines_an_evidence_driven_video_workflow() -> None:
    text = _reference_text()

    metadata_position = _require_ordered_stages(
        text,
        (
            ("source identification", ("视频来源", "识别来源", "视频入口", "来源确认", "入口")),
            ("short-link resolution", ("解析短链", "展开短链", "短链解析", "短链接重定向")),
            ("metadata inspection", ("页面元数据", "读取元数据", "检查元数据", "页面信息")),
        ),
    )
    direct_evidence_answer_pattern = re.compile(
        r"(?:字幕|转写|音频|画面|抽帧|实际媒体)"
        r"[^。！？；\n]{0,80}(?:后|之后|再|才|仅基于|基于)"
        r"[^。！？；\n]{0,80}(?:总结|概括|回答|回复)"
    )
    assert any(
        match.start() >= metadata_position
        for match in direct_evidence_answer_pattern.finditer(text)
    ), (
        "visual-media reference must acquire direct video evidence after metadata "
        "inspection, then bind the answer to that evidence; expected a related "
        "字幕/转写/音频/画面/抽帧/实际媒体 and answer instruction"
    )


def test_visual_media_reference_requires_multiframe_evidence_for_animated_images() -> None:
    text = _reference_text()

    for needle in ("GIF", "APNG", "WebP", "多帧", "首帧", "ffmpeg"):
        assert needle in text
    assert "首帧不能代表完整动图" in text
    assert "动态证据不可用" in text


def test_visual_media_reference_names_bilibili_short_urls_and_a_cross_platform_workflow() -> None:
    text = _reference_text()

    _require_any(text, ("Bilibili", "哔哩哔哩", "B站"), "Bilibili")
    assert "b23.tv" in text
    instruction_pattern = re.compile(r"[^。！？；\n]+")
    platform_workflow_instruction = next(
        (
            instruction
            for instruction in instruction_pattern.findall(text)
            if any(
                platform in instruction
                for platform in (
                    "抖音",
                    "快手",
                    "小红书",
                    "腾讯视频",
                    "优酷",
                    "爱奇艺",
                    "中文视频平台",
                    "国内视频平台",
                    "跨平台",
                    "其他视频平台",
                )
            )
            and any(
                step in instruction
                for step in (
                    "解析短链",
                    "展开短链",
                    "短链解析",
                    "短链接重定向",
                    "读取元数据",
                    "检查元数据",
                    "页面元数据",
                    "获取字幕",
                    "音频转写",
                    "获取音频",
                    "下载音频",
                    "提取音频",
                    "抽帧",
                    "采样帧",
                    "视频帧",
                )
            )
        ),
        None,
    )
    assert platform_workflow_instruction, (
        "visual-media reference must connect a Chinese non-Bilibili platform or "
        "cross-platform scope to an operational video workflow in one instruction"
    )


def test_visual_media_reference_distinguishes_metadata_from_direct_content_evidence() -> None:
    text = _reference_text()

    _require_any(text, ("元数据", "页面信息", "公开信息"), "metadata evidence")
    _require_any(text, ("直接内容证据", "正片内容证据", "实际媒体证据", "内容证据"), "direct evidence")
    _require_any(text, ("背景资料", "未验证为视频内容", "背景证据", "背景"), "background-only evidence")
    _require_any(text, ("不能只凭标题", "标题不能", "不可根据标题", "标题不足以"), "title-only inference ban")


def test_visual_media_reference_uses_conditional_audio_and_frame_fallbacks() -> None:
    text = _reference_text()

    assert "字幕" in text
    _require_related_phrases(
        text,
        ("无字幕", "字幕缺失", "字幕不可用", "缺少字幕"),
        ("时", "后", "则", "需要"),
        ("获取音频", "下载音频", "提取音频", "可访问音频", "音频转写"),
        "missing captions and an audio fallback",
    )
    _require_any(text, ("转写", "语音识别", "transcrib"), "audio transcription")
    conditional_frame_pattern = (
        r"(?:必要时|如果需要|需要视觉信息|需要画面信息)"
        r"[^。！？；\n]{0,80}(?:抽帧|采样帧|视频帧)"
    )
    assert re.search(conditional_frame_pattern, text), (
        "visual-media reference must make frame inspection conditional; "
        "for example, '必要时抽帧'"
    )
    _require_any(text, ("多个时间段", "多个时段", "多段采样", "多帧"), "multi-range frame sampling")


def test_visual_media_reference_handles_local_video_attachments() -> None:
    text = _reference_text()

    _require_any(
        text,
        ("本地视频", "本地附件", "用户附带的视频", "用户提供的本地文件"),
        "local video attachments",
    )


def test_visual_media_reference_treats_access_controls_as_honest_blockers() -> None:
    text = _reference_text()

    _require_any(text, ("不要绕过", "禁止绕过", "不应绕过"), "access-control bypass ban")
    _require_any(text, ("不要伪造", "不得伪造", "不可伪造"), "cookie or session fabrication ban")
    instruction_pattern = re.compile(r"[^。！？\n]+")
    truthful_blocker_instruction = next(
        (
            instruction
            for instruction in instruction_pattern.findall(text)
            if all(
                marker.casefold() in instruction.casefold()
                for marker in ("登录", "cookie", "403", "429")
            )
            and any(
                phrase in instruction
                for phrase in (
                    "不要写“视频内容概括”",
                    "不输出视频内容概括",
                    "不声称视频内容",
                    "不作内容概括",
                    "不要总结视频内容",
                    "不总结视频内容",
                )
            )
            and any(
                phrase in instruction
                for phrase in (
                    "已验证元数据",
                    "已确认的元数据",
                    "仅列元数据",
                    "只列元数据",
                )
            )
            and any(
                phrase in instruction
                for phrase in ("阻塞点", "说明阻塞", "报告阻塞", "阻塞回复")
            )
        ),
        None,
    )
    assert truthful_blocker_instruction, (
        "visual-media reference must state in one instruction that 登录/cookie/403/429 "
        "failures do not permit content summaries and only allow verified metadata plus "
        "the blocker"
    )


def test_visual_media_reference_bounds_long_video_work_and_cleans_temporary_media() -> None:
    text = _reference_text()

    _require_any(text, ("长视频", "较长视频"), "long-video handling")
    _require_any(
        text,
        ("token 预算", "Token 预算", "token 上限", "上下文预算", "令牌预算"),
        "token budget",
    )
    _require_any(text, ("采样", "间隔抽帧", "关键时间段"), "bounded frame sampling")
    _require_any(
        text,
        ("清理临时", "删除临时", "临时文件清理", "清理下载", "删除缓存", "cleanup"),
        "temporary-media cleanup",
    )


def test_visual_media_reference_requires_a_truthful_blocked_response_without_direct_evidence() -> None:
    text = _reference_text()

    _require_any(text, ("直接内容证据不足", "没有直接证据", "无法取得正片", "无正片证据"), "no-direct-evidence state")
    _require_any(text, ("阻塞回复", "说明阻塞", "报告阻塞", "阻塞点"), "blocked response")
    _require_any(text, ("已验证元数据", "已确认的元数据", "仅列元数据", "元数据和限制"), "metadata-only response")
    _require_any(text, ("不要猜测", "不得猜测", "不要推断", "不能推断"), "no-guess response")


def test_visual_media_reference_allows_brief_qq_progress_for_real_stages_only() -> None:
    text = _reference_text()

    assert "QQBOT_PROGRESS" in text
    _require_any(text, ("短进度", "简短进度", "简短的进度"), "brief progress")
    _require_any(text, ("不要刷屏", "避免刷屏", "不刷屏"), "non-spam progress")
