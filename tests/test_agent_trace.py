"""Agent trace logging tests."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.cursor_adapter import CustomCommandAdapter, CursorAdapter  # type: ignore
from qq_agent_bridge.agent_trace import AgentTrace  # type: ignore


def _config(workspace: Path, trace_root: Path) -> BridgeConfig:
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.trace_enabled = True
    cfg.agent.trace_root = str(trace_root)
    cfg.agent.trace_max_bytes = 32 * 1024
    cfg.agent.trace_max_line_chars = 2000
    cfg.agent.use_bwrap = False
    cfg.agent.env_runner = ""
    cfg.agent.require_env = False
    cfg.agent.max_runtime_seconds = 5
    return cfg


def _trace_root_for(workspace: Path) -> Path:
    return workspace.parent / f"{workspace.name}-agent-trace"


def _write_script(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _read_trace(root: Path) -> tuple[Path, list[dict[str, object]]]:
    files = list(root.glob("*.jsonl"))
    assert len(files) == 1
    path = files[0]
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return path, records


def test_disabled_trace_creates_no_files(tmp_path: Path) -> None:
    cfg = BridgeConfig(workspaces={str(tmp_path): True})
    cfg.agent.trace_root = str(tmp_path / "trace")

    trace = AgentTrace(cfg, "job-1", "ask", "auto", str(tmp_path))
    trace.record("stdout", "text", summary="not written")
    trace.close()

    assert not (tmp_path / "trace").exists()


def test_cursor_trace_records_lifecycle_redaction_and_safe_job_name(tmp_path: Path) -> None:
    script = tmp_path / "agent.sh"
    _write_script(
        script,
        """
printf '%s\\n' '{"type":"tool_call","subtype":"started","tool_call":{"description":"search token=abcdefgh123456"}}'
printf '%s\\n' '{"type":"assistant_message","message":{"content":"已完成"}}'
printf '%s\\n' 'stderr secret=abcdefgh123456' >&2
""",
    )
    trace_root = _trace_root_for(tmp_path)
    cfg = _config(tmp_path, trace_root)
    cfg.agent.binary = str(script)
    adapter = CursorAdapter(cfg)

    result = asyncio.run(
        adapter.run(
            "prompt must not be written",
            str(tmp_path),
            "ask",
            model="auto",
            trace_id="../unsafe/job-1",
        )
    )

    path, records = _read_trace(trace_root)
    assert result != "[error] 助手执行失败"
    assert path.parent == trace_root
    assert "/" not in path.stem
    assert records[0]["event"] == "start"
    assert any(record["event"] == "exit" for record in records)
    assert any(record["event"] == "tool_call" for record in records)
    assert any(record["stream"] == "stderr" for record in records)
    serialized = path.read_text(encoding="utf-8")
    assert "prompt must not be written" not in serialized
    assert "abcdefgh123456" not in serialized
    assert "[REDACTED]" in serialized


def test_trace_root_and_file_are_private_when_supported(tmp_path: Path) -> None:
    trace_root = _trace_root_for(tmp_path)
    cfg = _config(tmp_path, trace_root)
    trace = AgentTrace(cfg, "job-1", "task", "composer", str(tmp_path))
    trace.close()

    path, _records = _read_trace(trace_root)
    if os.name != "nt":
        assert stat.S_IMODE(trace_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_trace_line_and_byte_limits_emit_bounded_output(tmp_path: Path) -> None:
    trace_root = _trace_root_for(tmp_path)
    cfg = _config(tmp_path, trace_root)
    cfg.agent.trace_max_bytes = 700
    cfg.agent.trace_max_line_chars = 120
    trace = AgentTrace(cfg, "job-limit", "task", "composer", str(tmp_path))
    for _ in range(20):
        trace.record("stdout", "text", summary="x" * 1000)
    trace.close()

    path, records = _read_trace(trace_root)
    assert path.stat().st_size <= cfg.agent.trace_max_bytes
    assert all(len(line) <= cfg.agent.trace_max_line_chars for line in path.read_text(encoding="utf-8").splitlines())
    assert any(record["event"] == "truncated" for record in records)


def test_custom_non_json_trace_closes_without_changing_result(tmp_path: Path) -> None:
    script = tmp_path / "custom.sh"
    _write_script(script, "printf '%s\\n' 'custom result'")
    trace_root = _trace_root_for(tmp_path)
    cfg = _config(tmp_path, trace_root)
    cfg.agent.runtime = "custom-cli"
    cfg.agent.command = {"task": [str(script)]}
    cfg.agent.force_task_tools = False
    adapter = CustomCommandAdapter(cfg)

    result = asyncio.run(adapter.run("hello", str(tmp_path), "task", trace_id="custom-1"))

    _path, records = _read_trace(trace_root)
    assert result == "custom result"
    assert any(record["event"] == "text" and record["stream"] == "stdout" for record in records)
    assert any(record["event"] == "exit" for record in records)


def test_timeout_trace_records_timeout_and_exit(tmp_path: Path) -> None:
    script = tmp_path / "slow.sh"
    _write_script(script, "sleep 60")
    trace_root = _trace_root_for(tmp_path)
    cfg = _config(tmp_path, trace_root)
    cfg.agent.binary = str(script)
    cfg.agent.max_runtime_seconds = 0.05

    result = asyncio.run(CursorAdapter(cfg).run("hello", str(tmp_path), "ask", trace_id="timeout-1"))

    _path, records = _read_trace(trace_root)
    assert result == "[error] 助手响应超时"
    assert any(record["event"] == "timeout" for record in records)
    assert any(record["event"] == "exit" for record in records)
