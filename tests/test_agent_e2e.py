"""Optional end-to-end checks against a real configured agent runtime."""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.agent_runtime import build_agent_adapter  # type: ignore
from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.prompting import build_agent_prompt  # type: ignore
from qq_agent_bridge.types import ChatEvent  # type: ignore


_E2E_ENV = "QQ_AGENT_BRIDGE_AGENT_E2E"
_SEND_FILE_RE = re.compile(r"QQBOT_SEND_FILE:\s+(?P<token>\S+)\s+(?P<path>\S+)")


def _require_e2e() -> None:
    if os.environ.get(_E2E_ENV) != "1":
        pytest.skip(f"set {_E2E_ENV}=1 to run real agent E2E tests")


def _make_cfg(workspace: Path, mode: str = "ask") -> BridgeConfig:
    runtime = os.environ.get("QQ_AGENT_BRIDGE_E2E_RUNTIME", "cursor-cli")
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.runtime = runtime
    cfg.agent.default_workspace = str(workspace)
    cfg.agent.binary = os.environ.get("QQ_AGENT_BRIDGE_E2E_BINARY", "")
    cfg.agent.env_runner = os.environ.get("QQ_AGENT_BRIDGE_E2E_ENV_RUNNER", "")
    cfg.agent.env_name = os.environ.get("QQ_AGENT_BRIDGE_E2E_ENV_NAME", "")
    cfg.agent.require_env = False
    cfg.agent.use_bwrap = os.environ.get("QQ_AGENT_BRIDGE_E2E_BWRAP", "") == "1"
    cfg.agent.force_task_tools = False
    cfg.agent.max_runtime_seconds = int(os.environ.get("QQ_AGENT_BRIDGE_E2E_TIMEOUT", "90"))
    cfg.agent.max_output_chars = 8000
    cfg.resources.root = "downloads/qq-agent-bridge"
    cfg.commands = {mode: True}
    if runtime == "custom-cli":
        for item in ("ask", "task", "plan", "code"):
            value = os.environ.get(f"QQ_AGENT_BRIDGE_E2E_{item.upper()}_CMD", "").strip()
            if value:
                cfg.agent.command[item] = shlex.split(value)
        if mode not in cfg.agent.command:
            pytest.skip(f"set QQ_AGENT_BRIDGE_E2E_{mode.upper()}_CMD for custom-cli E2E")
    return cfg


def _make_ev(text: str, chat_id: str = "e2e-user") -> ChatEvent:
    return ChatEvent(
        id="agent-e2e",
        platform="qq",
        chat_id=chat_id,
        sender_id="e2e-user",
        is_group=False,
        mentioned_bot=True,
        text=text,
        timestamp=1,
    )


async def _run_agent(prompt: str, cfg: BridgeConfig, mode: str, model: str | None) -> str:
    adapter = build_agent_adapter(cfg)
    return await adapter.run(prompt, cfg.agent.default_workspace, mode, model=model)


def test_real_agent_can_return_a_fixed_token(tmp_path: Path) -> None:
    _require_e2e()
    cfg = _make_cfg(tmp_path, "ask")
    token = "QQ_AGENT_BRIDGE_E2E_OK_FIXED"
    prompt = build_agent_prompt(
        "ask",
        f"只输出这个 token，不要输出其他文字：{token}",
        _make_ev("固定 token"),
        profile_prompt="你是 QQ bot 测试对象，必须严格遵守当前用户要求。",
    )

    out = asyncio.run(_run_agent(prompt, cfg, "ask", os.environ.get("QQ_AGENT_BRIDGE_E2E_CHAT_MODEL", "auto")))

    assert token in out
    assert "Cursor" not in out
    assert "NapCat" not in out
    assert "OneBot" not in out


def test_real_agent_task_can_read_workspace_file(tmp_path: Path) -> None:
    _require_e2e()
    cfg = _make_cfg(tmp_path, "task")
    token = "QQ_AGENT_BRIDGE_E2E_OK_FILE"
    data_file = tmp_path / "agent-e2e-token.txt"
    data_file.write_text(f"{token}\n", encoding="utf-8")
    prompt = build_agent_prompt(
        "task",
        f"读取这个本地文件并只回复里面的 token：{data_file}",
        _make_ev("读文件"),
        profile_prompt="你是 QQ bot 测试对象。只有实际读取文件后才能回答。",
    )

    out = asyncio.run(_run_agent(prompt, cfg, "task", os.environ.get("QQ_AGENT_BRIDGE_E2E_TASK_MODEL")))

    assert token in out


def test_real_agent_task_can_emit_send_file_directive(tmp_path: Path) -> None:
    _require_e2e()
    cfg = _make_cfg(tmp_path, "task")
    token = "QQ_AGENT_BRIDGE_E2E_OK_SEND_FILE"
    outbox = tmp_path / cfg.resources.root / "outgoing" / "agent-e2e"
    outbox.mkdir(parents=True, exist_ok=True)
    directive_token = "e2e-send-token"
    outgoing_context = (
        f"可发送资源目录：{outbox}\n"
        f"资源发送令牌：{directive_token}\n"
        "发送文件格式：QQBOT_SEND_FILE: <token> <relative-or-absolute-path>\n"
    )
    prompt = build_agent_prompt(
        "task",
        f"在可发送资源目录创建 agent-e2e.txt，内容必须包含 {token}，然后输出发送文件指令。",
        _make_ev("发文件"),
        outgoing_resource_context=outgoing_context,
        profile_prompt="你是 QQ bot 测试对象。不要声称发送成功，只输出真实指令。",
    )

    out = asyncio.run(_run_agent(prompt, cfg, "task", os.environ.get("QQ_AGENT_BRIDGE_E2E_TASK_MODEL")))

    match = _SEND_FILE_RE.search(out)
    assert match is not None, out
    assert match.group("token") == directive_token
    raw_path = Path(match.group("path"))
    candidates = [raw_path]
    if not raw_path.is_absolute():
        candidates.extend([tmp_path / raw_path, outbox / raw_path])
    sent_file = next((path for path in candidates if path.exists()), None)
    assert sent_file is not None, f"send directive did not point to an existing file: {out}"
    assert token in sent_file.read_text(encoding="utf-8")
