"""Deterministic evidence-state contract for synthetic video scenarios."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "video_media_cases.json"
REFERENCE = ROOT / "skills" / "qq-agent-runtime" / "references" / "visual-media.md"

EVIDENCE_FIELDS = frozenset({"metadata", "captions", "transcript", "audio", "frames"})
CASE_FIELDS = frozenset(
    {
        "id",
        "source",
        "evidence",
        "access",
        "blockers",
        "evidence_state",
        "answer_mode",
        "policy",
        "budget",
        "cleanup_expected",
    }
)
SOURCE_FIELDS = frozenset({"kind", "platform", "input"})
BUDGET_FIELDS = frozenset({"timestamp_windows", "max_frames", "max_tokens"})

POLICY_MARKERS = {
    "short_link_resolution": (("b23.tv",), ("解析短链", "展开重定向")),
    "caption_evidence": (("字幕",), ("才总结", "才总结或回答", "只引用实际读到")),
    "local_attachment": (("本地视频", "本地附件", "用户附带的视频"),),
    "audio_or_frame_evidence": (("提取音频", "获取音频"), ("抽帧", "视频帧")),
    "metadata_is_not_content": (("元数据",), ("不等于直接内容证据", "不能只凭标题")),
    "blocked_response": (("直接内容证据不足", "无法取得正片"), ("阻塞回复", "阻塞点")),
    "access_controls": (("登录",), ("cookie",), ("403",), ("429",), ("不要总结视频内容",)),
    "bounded_sampling": (("长视频",), ("token 预算", "token 上限"), ("时间戳",), ("帧数预算", "帧数")),
    "temporary_cleanup": (("清理临时", "临时文件清理", "删除临时"),),
}


def _load_cases() -> list[dict[str, Any]]:
    try:
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AssertionError(f"unable to load valid JSON fixture: {error}") from error

    assert isinstance(document, dict)
    assert set(document) == {"schema_version", "cases"}
    assert document["schema_version"] == 1
    assert isinstance(document["cases"], list)
    assert document["cases"], "fixture must contain at least one case"

    cases: list[dict[str, Any]] = []
    for case in document["cases"]:
        _validate_case(case)
        cases.append(case)
    return cases


def _validate_case(case: Any) -> None:
    assert isinstance(case, dict)
    assert set(case) == CASE_FIELDS
    assert isinstance(case["id"], str) and case["id"].replace("_", "").isalnum()

    source = case["source"]
    assert isinstance(source, dict) and set(source) == SOURCE_FIELDS
    assert source["kind"] in {"short_link", "video_page", "local_attachment"}
    assert isinstance(source["platform"], str) and source["platform"]
    assert isinstance(source["input"], str) and source["input"]

    evidence = case["evidence"]
    assert isinstance(evidence, dict) and set(evidence) == EVIDENCE_FIELDS
    assert all(isinstance(value, bool) for value in evidence.values())
    assert evidence["metadata"]

    assert case["access"] in {"available", "blocked"}
    assert isinstance(case["blockers"], list)
    assert all(isinstance(blocker, str) and blocker for blocker in case["blockers"])
    assert isinstance(case["policy"], list) and case["policy"]
    assert all(policy in POLICY_MARKERS for policy in case["policy"])
    assert isinstance(case["cleanup_expected"], bool)

    direct_evidence = any(
        evidence[name] for name in ("captions", "transcript", "audio", "frames")
    )
    expected_state = "blocked" if case["access"] == "blocked" else (
        "direct" if direct_evidence else "metadata-only"
    )
    assert case["evidence_state"] == expected_state
    assert case["answer_mode"] == (
        "content-summary" if expected_state == "direct" else "metadata-and-blocker"
    )
    if expected_state != "direct":
        assert case["blockers"], "non-direct cases must state what blocked evidence"
    if expected_state == "blocked":
        assert not direct_evidence, "blocked access cannot be treated as direct evidence"

    budget = case["budget"]
    if case["id"] == "bounded_long_video":
        assert isinstance(budget, dict) and set(budget) == BUDGET_FIELDS
        assert isinstance(budget["timestamp_windows"], list) and budget["timestamp_windows"]
        assert all(isinstance(window, str) and window for window in budget["timestamp_windows"])
        assert all(isinstance(budget[field], int) and budget[field] > 0 for field in ("max_frames", "max_tokens"))
        assert case["cleanup_expected"]
    else:
        assert budget is None
        assert not case["cleanup_expected"]


def _assert_reference_policy(policy: str, reference: str) -> None:
    for alternatives in POLICY_MARKERS[policy]:
        assert any(marker in reference for marker in alternatives), (
            f"visual-media reference must cover {policy}; expected one of {alternatives!r}"
        )


def test_video_media_fixture_cases_validate_evidence_states_and_reference_policy() -> None:
    cases = _load_cases()

    assert {case["id"] for case in cases} == {
        "captioned_bilibili_short_link",
        "local_attachment_with_audio_and_frames",
        "metadata_only_short_link",
        "access_control_failure",
        "bounded_long_video",
    }

    reference = REFERENCE.read_text(encoding="utf-8")
    for case in cases:
        for policy in case["policy"]:
            _assert_reference_policy(policy, reference)
