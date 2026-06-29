"""Agent runtime adapter factory tests."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.agent_runtime import DisabledAgentAdapter, build_agent_adapter  # type: ignore
from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.cursor_adapter import CursorAdapter, CustomCommandAdapter  # type: ignore


def test_agent_runtime_factory_does_not_default_to_cursor() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})

    adapter = build_agent_adapter(cfg)

    assert isinstance(adapter, DisabledAgentAdapter)


def test_disabled_agent_runtime_explains_missing_config() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = build_agent_adapter(cfg)

    result = asyncio.run(adapter.run("hello", "/tmp", "ask", model=None))

    assert "agent runtime 未配置" in result


def test_agent_runtime_factory_uses_cursor_when_configured() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.runtime = "cursor-cli"

    adapter = build_agent_adapter(cfg)

    assert isinstance(adapter, CursorAdapter)


def test_custom_command_adapter_expands_command_template() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.runtime = "custom-cli"
    cfg.agent.env_runner = ""
    cfg.agent.use_bwrap = False
    cfg.agent.command = {
        "ask": [
            "agent-bin",
            "--mode",
            "{mode}",
            "--model",
            "{model}",
            "--workspace",
            "{workspace}",
            "{prompt}",
        ],
    }
    adapter = build_agent_adapter(cfg)

    cmd = adapter._build_cmd("hello world", "/tmp", "ask", model="fast")  # noqa: SLF001

    assert isinstance(adapter, CustomCommandAdapter)
    assert cmd == [
        "agent-bin",
        "--mode",
        "ask",
        "--model",
        "fast",
        "--workspace",
        "/tmp",
        "hello world",
    ]


def test_custom_command_adapter_rejects_missing_mode_template() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.runtime = "custom-cli"
    cfg.agent.env_runner = ""
    cfg.agent.use_bwrap = False
    cfg.agent.command = {"ask": ["agent-bin", "{prompt}"]}
    adapter = build_agent_adapter(cfg)

    try:
        adapter._build_cmd("hello", "/tmp", "task", model=None)  # noqa: SLF001
    except ValueError as exc:
        assert "missing custom command template for task" in str(exc)
    else:
        raise AssertionError("expected missing task template to raise")


def test_unsupported_agent_runtime_is_rejected() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.runtime = "unknown-runtime"

    try:
        build_agent_adapter(cfg)
    except ValueError as exc:
        assert "unsupported agent runtime" in str(exc)
    else:
        raise AssertionError("expected unsupported runtime to raise")
