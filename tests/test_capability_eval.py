"""Capability-eval harness tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.capability_eval import (  # type: ignore
    CapabilityCase,
    build_judge_prompt,
    evaluate_capability_output,
    parse_judge_verdict,
    run_hard_checks,
)


def test_parse_judge_verdict_accepts_fenced_json() -> None:
    verdict = parse_judge_verdict(
        """
        ```json
        {"pass": true, "score": 0.92, "reason": "完成", "failures": []}
        ```
        """
    )

    assert verdict.passed
    assert verdict.score == 0.92
    assert verdict.reason == "完成"
    assert verdict.failures == ()


def test_parse_judge_verdict_fails_closed_on_invalid_output() -> None:
    verdict = parse_judge_verdict("看起来还行")

    assert not verdict.passed
    assert verdict.score == 0
    assert verdict.failures


def test_hard_checks_report_missing_required_and_forbidden_text() -> None:
    case = CapabilityCase(
        name="anti-hallucination",
        prompt="不要编造",
        criteria=("不知道时说明限制",),
        required_substrings=("无法确认",),
        forbidden_substrings=("我已经搜索了全网",),
        required_regexes=(r"https?://",),
    )

    failures = run_hard_checks(case, "我已经搜索了全网，但无法确认")

    assert "forbidden substring: 我已经搜索了全网" in failures
    assert "missing regex: https?://" in failures


def test_evaluate_capability_output_requires_hard_checks_and_judge_score() -> None:
    case = CapabilityCase(
        name="qq-style",
        prompt="像 QQ bot 一样回答",
        criteria=("短", "自然"),
        required_substrings=("收到",),
        min_score=0.8,
    )

    passed = evaluate_capability_output(
        case,
        agent_output="收到，我处理一下。",
        judge_output='{"pass": true, "score": 0.81, "reason": "自然", "failures": []}',
    )
    failed = evaluate_capability_output(
        case,
        agent_output="收到，我处理一下。",
        judge_output='{"pass": true, "score": 0.7, "reason": "略弱", "failures": []}',
    )

    assert passed.passed
    assert not failed.passed
    assert "judge score below threshold" in failed.failures[0]


def test_build_judge_prompt_demands_json_and_includes_case_material() -> None:
    case = CapabilityCase(
        name="resource-send",
        prompt="生成文件并发送",
        criteria=("必须真实生成文件", "必须输出发送指令"),
    )

    prompt = build_judge_prompt(case, "QQBOT_SEND_FILE: token file.txt")

    assert "只输出 JSON" in prompt
    assert '"pass"' in prompt
    assert "resource-send" in prompt
    assert "生成文件并发送" in prompt
    assert "QQBOT_SEND_FILE: token file.txt" in prompt
