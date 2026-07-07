"""Optional semantic capability evals against a real configured agent runtime."""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.agent_runtime import build_agent_adapter  # type: ignore
from qq_agent_bridge.capability_eval import (  # type: ignore
    CapabilityCase,
    build_judge_prompt,
    evaluate_capability_output,
)
from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.prompting import build_agent_prompt  # type: ignore
from qq_agent_bridge.types import ChatEvent  # type: ignore


_CAPABILITY_ENV = "QQ_AGENT_BRIDGE_CAPABILITY_EVAL"


def _require_capability_eval() -> None:
    if os.environ.get(_CAPABILITY_ENV) != "1":
        pytest.skip(f"set {_CAPABILITY_ENV}=1 to run real agent capability evals")


def _make_cfg(workspace: Path, *, judge: bool = False) -> BridgeConfig:
    prefix = "QQ_AGENT_BRIDGE_CAPABILITY_JUDGE" if judge else "QQ_AGENT_BRIDGE_CAPABILITY"
    runtime = os.environ.get(f"{prefix}_RUNTIME") or os.environ.get("QQ_AGENT_BRIDGE_E2E_RUNTIME", "cursor-cli")
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.runtime = runtime
    cfg.agent.default_workspace = str(workspace)
    cfg.agent.binary = os.environ.get(f"{prefix}_BINARY") or os.environ.get("QQ_AGENT_BRIDGE_E2E_BINARY", "")
    cfg.agent.env_runner = os.environ.get(f"{prefix}_ENV_RUNNER") or os.environ.get("QQ_AGENT_BRIDGE_E2E_ENV_RUNNER", "")
    cfg.agent.env_name = os.environ.get(f"{prefix}_ENV_NAME") or os.environ.get("QQ_AGENT_BRIDGE_E2E_ENV_NAME", "")
    cfg.agent.require_env = False
    bwrap = os.environ.get(f"{prefix}_BWRAP", os.environ.get("QQ_AGENT_BRIDGE_E2E_BWRAP", "1"))
    cfg.agent.use_bwrap = bwrap != "0"
    if runtime == "cursor-cli" and cfg.agent.use_bwrap and not shutil.which(cfg.agent.bwrap_binary):
        pytest.skip("cursor-cli capability eval needs bwrap; set QQ_AGENT_BRIDGE_CAPABILITY_BWRAP=0 to override")
    cfg.agent.force_task_tools = runtime == "cursor-cli" and cfg.agent.use_bwrap
    cfg.agent.max_runtime_seconds = int(os.environ.get(f"{prefix}_TIMEOUT", os.environ.get("QQ_AGENT_BRIDGE_E2E_TIMEOUT", "120")))
    cfg.agent.max_output_chars = 12000
    cfg.resources.root = "downloads/qq-agent-bridge"
    if runtime == "custom-cli":
        for item in ("ask", "task", "plan", "code"):
            value = (
                os.environ.get(f"{prefix}_{item.upper()}_CMD", "").strip()
                or os.environ.get(f"QQ_AGENT_BRIDGE_E2E_{item.upper()}_CMD", "").strip()
            )
            if value:
                cfg.agent.command[item] = shlex.split(value)
        if "ask" not in cfg.agent.command:
            pytest.skip(f"set {prefix}_ASK_CMD for custom-cli capability eval")
    return cfg


def _make_ev(text: str) -> ChatEvent:
    return ChatEvent(
        id="agent-capability-eval",
        platform="qq",
        chat_id="capability-eval-user",
        sender_id="capability-eval-user",
        is_group=False,
        mentioned_bot=True,
        text=text,
        timestamp=1,
    )


async def _run_agent(cfg: BridgeConfig, prompt: str, mode: str, model: str | None) -> str:
    adapter = build_agent_adapter(cfg)
    return await adapter.run(prompt, cfg.agent.default_workspace, mode, model=model)


def _model_for(mode: str, *, judge: bool = False) -> str | None:
    if judge:
        return os.environ.get("QQ_AGENT_BRIDGE_CAPABILITY_JUDGE_MODEL") or os.environ.get(
            "QQ_AGENT_BRIDGE_CAPABILITY_CHAT_MODEL",
            "auto",
        )
    if mode == "task":
        return os.environ.get("QQ_AGENT_BRIDGE_CAPABILITY_TASK_MODEL") or os.environ.get(
            "QQ_AGENT_BRIDGE_E2E_TASK_MODEL",
        )
    return os.environ.get("QQ_AGENT_BRIDGE_CAPABILITY_CHAT_MODEL") or os.environ.get(
        "QQ_AGENT_BRIDGE_E2E_CHAT_MODEL",
        "auto",
    )


def _runner_mode_for(cfg: BridgeConfig, prompt_mode: str) -> str:
    if cfg.agent.runtime == "cursor-cli" and cfg.agent.use_bwrap and prompt_mode in {"ask", "plan"}:
        return "task"
    return prompt_mode


def _run_case(case: CapabilityCase, workspace: Path) -> None:
    target_cfg = _make_cfg(workspace)
    judge_cfg = _make_cfg(workspace, judge=True)
    prompt = build_agent_prompt(
        case.mode,
        case.prompt,
        _make_ev(case.prompt),
        profile_prompt="你是 QQ bot 能力评测对象。按 QQ 聊天机器人身份回答，不要解释测试框架。",
    )

    target_runner_mode = _runner_mode_for(target_cfg, case.mode)
    agent_output = asyncio.run(_run_agent(target_cfg, prompt, target_runner_mode, _model_for(case.mode)))
    judge_prompt = build_judge_prompt(case, agent_output)
    judge_runner_mode = _runner_mode_for(judge_cfg, "ask")
    judge_output = asyncio.run(_run_agent(judge_cfg, judge_prompt, judge_runner_mode, _model_for("ask", judge=True)))
    result = evaluate_capability_output(case, agent_output, judge_output)

    assert result.passed, (
        f"case={case.name}\n"
        f"agent_output={agent_output}\n"
        f"judge_reason={result.judge.reason}\n"
        f"failures={result.failures}\n"
        f"judge_output={judge_output}"
    )


def test_real_agent_capability_handles_ask_to_task_handoff(tmp_path: Path) -> None:
    _require_capability_eval()
    case = CapabilityCase(
        name="ask-to-task-handoff",
        mode="ask",
        prompt="全网搜索清华大学最近新闻，整理成 Excel 发给我",
        criteria=(
            "不能声称已经联网搜索或已经生成 Excel",
            "应该说明这类任务需要使用 /task",
            "回复像 QQ 消息，短而直接",
        ),
        required_substrings=("/task",),
        forbidden_substrings=("我已经搜索", "已生成 Excel", "已生成excel"),
        min_score=0.8,
    )

    _run_case(case, tmp_path)


def test_real_agent_capability_reads_workspace_evidence(tmp_path: Path) -> None:
    _require_capability_eval()
    token = "QQ_AGENT_CAPABILITY_EVIDENCE_TOKEN"
    data_file = tmp_path / "evidence.txt"
    data_file.write_text(token, encoding="utf-8")
    case = CapabilityCase(
        name="workspace-evidence",
        mode="task",
        prompt=f"读取这个文件并只回复其中的 token：{data_file}",
        criteria=(
            "必须实际根据文件内容回答",
            "输出应包含文件里的 token",
            "不能编造额外文件内容",
        ),
        required_substrings=(token,),
        min_score=0.9,
    )

    _run_case(case, tmp_path)
