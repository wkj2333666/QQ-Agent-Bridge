"""Deterministic evidence-state contract for synthetic video scenarios."""
from __future__ import annotations

import json
from copy import deepcopy
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
        "direct_evidence",
        "media_lifecycle",
        "policy",
        "budget",
        "cleanup_expected",
    }
)
SOURCE_FIELDS = frozenset({"kind", "platform", "input"})
BUDGET_FIELDS = frozenset({"timestamp_windows", "max_frames", "max_tokens"})
DIRECT_EVIDENCE_FIELDS = frozenset({"kind", "excerpts"})
DIRECT_EVIDENCE_EXCERPT_FIELDS = frozenset({"start", "end", "text"})
DIRECT_EVIDENCE_KINDS = frozenset({"captions", "transcript", "audio", "frames"})
MEDIA_LIFECYCLE_FIELDS = frozenset({"original_user_attachment", "temporary_derivatives"})
ORIGINAL_ATTACHMENT_FIELDS = frozenset({"path", "cleanup_after_processing"})
TEMPORARY_DERIVATIVE_FIELDS = frozenset({"kind", "path", "cleanup_after_processing"})
LOCAL_ATTACHMENT_DERIVATIVE_KINDS = frozenset({"audio", "frames"})
DIRECT_EVIDENCE_POLICIES = {
    "captions": "caption_evidence",
    "transcript": "caption_evidence",
    "audio": "audio_or_frame_evidence",
    "frames": "audio_or_frame_evidence",
}
LONG_VIDEO_BUDGET = {
    "timestamp_windows": ["00:00-00:45", "12:00-12:45", "24:00-24:45"],
    "max_frames": 9,
    "max_tokens": 1800,
}

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


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return False
    minutes, seconds = (int(part) for part in parts)
    return minutes >= 0 and 0 <= seconds < 60


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

    direct_payload = case["direct_evidence"]
    if expected_state == "direct":
        assert isinstance(direct_payload, dict)
        assert set(direct_payload) == DIRECT_EVIDENCE_FIELDS
        assert direct_payload["kind"] in DIRECT_EVIDENCE_KINDS
        assert evidence[direct_payload["kind"]]
        assert isinstance(direct_payload["excerpts"], list) and direct_payload["excerpts"]
        for excerpt in direct_payload["excerpts"]:
            assert isinstance(excerpt, dict)
            assert set(excerpt) == DIRECT_EVIDENCE_EXCERPT_FIELDS
            assert all(
                isinstance(excerpt[field], str) and excerpt[field]
                for field in DIRECT_EVIDENCE_EXCERPT_FIELDS
            )
            assert _is_timestamp(excerpt["start"])
            assert _is_timestamp(excerpt["end"])
            assert excerpt["text"].startswith("Synthetic ")
        assert DIRECT_EVIDENCE_POLICIES[direct_payload["kind"]] in case["policy"]
    else:
        assert direct_payload is None

    if expected_state != "direct":
        assert "metadata_is_not_content" in case["policy"]
        assert "blocked_response" in case["policy"]
    if case["access"] == "blocked":
        assert "access_controls" in case["policy"]
        assert "blocked_response" in case["policy"]

    media_lifecycle = case["media_lifecycle"]
    if source["kind"] == "local_attachment":
        assert isinstance(media_lifecycle, dict)
        assert set(media_lifecycle) == MEDIA_LIFECYCLE_FIELDS

        original_attachment = media_lifecycle["original_user_attachment"]
        assert isinstance(original_attachment, dict)
        assert set(original_attachment) == ORIGINAL_ATTACHMENT_FIELDS
        assert original_attachment["path"] == source["input"]
        assert original_attachment["cleanup_after_processing"] is False

        temporary_derivatives = media_lifecycle["temporary_derivatives"]
        assert isinstance(temporary_derivatives, list) and temporary_derivatives
        derivative_kinds = set()
        for derivative in temporary_derivatives:
            assert isinstance(derivative, dict)
            assert set(derivative) == TEMPORARY_DERIVATIVE_FIELDS
            assert derivative["kind"] in LOCAL_ATTACHMENT_DERIVATIVE_KINDS
            assert isinstance(derivative["path"], str) and derivative["path"]
            assert derivative["path"] != original_attachment["path"]
            assert derivative["cleanup_after_processing"] is True
            derivative_kinds.add(derivative["kind"])
        assert derivative_kinds == LOCAL_ATTACHMENT_DERIVATIVE_KINDS
        assert len(temporary_derivatives) == len(derivative_kinds)
        assert case["cleanup_expected"] is True
        assert "temporary_cleanup" in case["policy"]
    else:
        assert media_lifecycle is None

    budget = case["budget"]
    if case["id"] == "bounded_long_video":
        assert isinstance(budget, dict) and set(budget) == BUDGET_FIELDS
        assert isinstance(budget["timestamp_windows"], list) and budget["timestamp_windows"]
        assert all(isinstance(window, str) and window for window in budget["timestamp_windows"])
        assert all(type(budget[field]) is int and budget[field] > 0 for field in ("max_frames", "max_tokens"))
        assert budget == LONG_VIDEO_BUDGET
        assert case["cleanup_expected"]
        assert "bounded_sampling" in case["policy"]
        assert "temporary_cleanup" in case["policy"]
    else:
        assert budget is None
        if source["kind"] != "local_attachment":
            assert not case["cleanup_expected"]


def _assert_reference_policy(policy: str, reference: str) -> None:
    for alternatives in POLICY_MARKERS[policy]:
        assert any(marker in reference for marker in alternatives), (
            f"visual-media reference must cover {policy}; expected one of {alternatives!r}"
        )


def test_video_media_fixture_cases_validate_evidence_states_and_reference_policy() -> None:
    cases = _load_cases()

    cases_by_id = {case["id"]: case for case in cases}
    assert set(cases_by_id) == {
        "captioned_bilibili_short_link",
        "local_attachment_with_audio_and_frames",
        "metadata_only_short_link",
        "access_control_failure",
        "bounded_long_video",
    }
    assert len(cases_by_id) == len(cases)

    expected_cases = {
        "captioned_bilibili_short_link": {
            "source": ("short_link", "bilibili"),
            "evidence": {"metadata": True, "captions": True, "transcript": False, "audio": False, "frames": False},
            "access": "available",
            "evidence_state": "direct",
            "answer_mode": "content-summary",
            "cleanup_expected": False,
        },
        "local_attachment_with_audio_and_frames": {
            "source": ("local_attachment", "local"),
            "evidence": {"metadata": True, "captions": False, "transcript": False, "audio": True, "frames": True},
            "access": "available",
            "evidence_state": "direct",
            "answer_mode": "content-summary",
            "cleanup_expected": True,
        },
        "metadata_only_short_link": {
            "source": ("short_link", "bilibili"),
            "evidence": {"metadata": True, "captions": False, "transcript": False, "audio": False, "frames": False},
            "access": "available",
            "evidence_state": "metadata-only",
            "answer_mode": "metadata-and-blocker",
            "cleanup_expected": False,
        },
        "access_control_failure": {
            "source": ("video_page", "bilibili"),
            "evidence": {"metadata": True, "captions": False, "transcript": False, "audio": False, "frames": False},
            "access": "blocked",
            "evidence_state": "blocked",
            "answer_mode": "metadata-and-blocker",
            "cleanup_expected": False,
        },
        "bounded_long_video": {
            "source": ("video_page", "bilibili"),
            "evidence": {"metadata": True, "captions": True, "transcript": False, "audio": False, "frames": True},
            "access": "available",
            "evidence_state": "direct",
            "answer_mode": "content-summary",
            "cleanup_expected": True,
        },
    }
    for case_id, expected in expected_cases.items():
        case = cases_by_id[case_id]
        assert (case["source"]["kind"], case["source"]["platform"]) == expected["source"]
        assert case["evidence"] == expected["evidence"]
        assert case["access"] == expected["access"]
        assert case["evidence_state"] == expected["evidence_state"]
        assert case["answer_mode"] == expected["answer_mode"]
        assert case["cleanup_expected"] is expected["cleanup_expected"]
    assert cases_by_id["bounded_long_video"]["budget"] == LONG_VIDEO_BUDGET
    assert cases_by_id["local_attachment_with_audio_and_frames"]["media_lifecycle"] == {
        "original_user_attachment": {
            "path": "synthetic-attachment.mp4",
            "cleanup_after_processing": False,
        },
        "temporary_derivatives": [
            {
                "kind": "audio",
                "path": "temporary/synthetic-attachment-audio.wav",
                "cleanup_after_processing": True,
            },
            {
                "kind": "frames",
                "path": "temporary/synthetic-attachment-frame-0001.jpg",
                "cleanup_after_processing": True,
            },
        ],
    }

    reference = REFERENCE.read_text(encoding="utf-8")
    for case in cases:
        for policy in case["policy"]:
            _assert_reference_policy(policy, reference)


def test_video_media_fixture_semantics_cannot_be_weakened_by_policy_lists() -> None:
    cases = {case["id"]: case for case in _load_cases()}
    invalid_cases = []

    metadata_only = deepcopy(cases["metadata_only_short_link"])
    metadata_only["policy"].remove("metadata_is_not_content")
    invalid_cases.append(metadata_only)

    blocked = deepcopy(cases["access_control_failure"])
    blocked["policy"].remove("access_controls")
    invalid_cases.append(blocked)

    blocked_without_metadata_guard = deepcopy(cases["access_control_failure"])
    blocked_without_metadata_guard["policy"].remove("metadata_is_not_content")
    invalid_cases.append(blocked_without_metadata_guard)

    direct = deepcopy(cases["captioned_bilibili_short_link"])
    direct["policy"].remove("caption_evidence")
    invalid_cases.append(direct)

    long_video = deepcopy(cases["bounded_long_video"])
    long_video["policy"].remove("temporary_cleanup")
    invalid_cases.append(long_video)

    local_attachment = deepcopy(cases["local_attachment_with_audio_and_frames"])
    local_attachment["media_lifecycle"]["temporary_derivatives"][0]["cleanup_after_processing"] = False
    invalid_cases.append(local_attachment)

    local_attachment_original = deepcopy(cases["local_attachment_with_audio_and_frames"])
    local_attachment_original["media_lifecycle"]["original_user_attachment"]["cleanup_after_processing"] = True
    invalid_cases.append(local_attachment_original)

    local_attachment_without_policy = deepcopy(cases["local_attachment_with_audio_and_frames"])
    local_attachment_without_policy["policy"].remove("temporary_cleanup")
    invalid_cases.append(local_attachment_without_policy)

    local_attachment_without_cleanup = deepcopy(cases["local_attachment_with_audio_and_frames"])
    local_attachment_without_cleanup["cleanup_expected"] = False
    invalid_cases.append(local_attachment_without_cleanup)

    boolean_budget = deepcopy(cases["bounded_long_video"])
    boolean_budget["budget"]["max_frames"] = True
    invalid_cases.append(boolean_budget)

    missing_payload = deepcopy(cases["captioned_bilibili_short_link"])
    missing_payload["direct_evidence"] = None
    invalid_cases.append(missing_payload)

    for case in invalid_cases:
        try:
            _validate_case(case)
        except AssertionError:
            continue
        raise AssertionError(f"invalid fixture case was accepted: {case['id']}")
