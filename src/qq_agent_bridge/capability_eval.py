"""Small helpers for optional real-agent capability evaluations."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CapabilityCase:
    name: str
    prompt: str
    criteria: tuple[str, ...] = ()
    mode: str = "task"
    required_substrings: tuple[str, ...] = ()
    forbidden_substrings: tuple[str, ...] = ()
    required_regexes: tuple[str, ...] = ()
    min_score: float = 0.8


@dataclass(frozen=True)
class JudgeVerdict:
    passed: bool
    score: float
    reason: str
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityEvalResult:
    passed: bool
    hard_failures: tuple[str, ...]
    judge: JudgeVerdict

    @property
    def failures(self) -> tuple[str, ...]:
        return self.hard_failures + self.judge.failures


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def run_hard_checks(case: CapabilityCase, output: str) -> tuple[str, ...]:
    failures: list[str] = []
    for expected in case.required_substrings:
        if expected not in output:
            failures.append(f"missing substring: {expected}")
    for forbidden in case.forbidden_substrings:
        if forbidden in output:
            failures.append(f"forbidden substring: {forbidden}")
    for pattern in case.required_regexes:
        if not re.search(pattern, output):
            failures.append(f"missing regex: {pattern}")
    return tuple(failures)


def parse_judge_verdict(output: str) -> JudgeVerdict:
    data = _extract_json_object(output)
    if data is None:
        return JudgeVerdict(False, 0.0, "judge did not return JSON", ("invalid judge JSON",))
    passed = bool(data.get("pass", data.get("passed", False)))
    score = _score(data.get("score"))
    reason = str(data.get("reason") or "").strip()
    failures = _string_tuple(data.get("failures"))
    return JudgeVerdict(passed, score, reason, failures)


def evaluate_capability_output(
    case: CapabilityCase,
    agent_output: str,
    judge_output: str,
) -> CapabilityEvalResult:
    hard_failures = run_hard_checks(case, agent_output)
    judge = parse_judge_verdict(judge_output)
    judge_failures = list(judge.failures)
    if judge.score < case.min_score:
        judge_failures.insert(0, f"judge score below threshold: {judge.score} < {case.min_score}")
    judge = JudgeVerdict(
        passed=judge.passed and judge.score >= case.min_score,
        score=judge.score,
        reason=judge.reason,
        failures=tuple(judge_failures),
    )
    return CapabilityEvalResult(not hard_failures and judge.passed, hard_failures, judge)


def build_judge_prompt(case: CapabilityCase, agent_output: str) -> str:
    criteria = "\n".join(f"- {item}" for item in case.criteria) or "- 判断是否满足用户请求"
    return f"""你是 QQ Agent Bridge 的能力评测裁判。请根据测试用例和 agent 输出判断是否通过。

只输出 JSON，不要输出 JSON 之外的任何文字。
JSON 格式：
{{"pass": true, "score": 0.0, "reason": "一句话原因", "failures": []}}

评分规则：
- pass 只能在 agent 输出确实满足全部标准时为 true。
- score 范围 0 到 1。
- 如有失败点，failures 写短字符串数组。
- 不要替 agent 补全事实；只能根据 agent 输出和测试用例判断。

测试名：{case.name}
用户请求：
{case.prompt}

通过标准：
{criteria}

agent 输出：
{agent_output}
"""


def _extract_json_object(output: str) -> dict[str, Any] | None:
    match = _JSON_BLOCK_RE.search(output)
    raw = match.group(1) if match else None
    if raw is None:
        match = _JSON_OBJECT_RE.search(output)
        raw = match.group(0) if match else None
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()
