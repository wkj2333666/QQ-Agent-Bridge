"""Cursor adapter command construction tests."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.cursor_adapter import CursorAdapter  # type: ignore


def test_ask_command_is_read_only() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)
    cmd = adapter._build_cmd("hello", "/tmp", "ask", model=None)
    assert "--mode" in cmd
    assert "ask" in cmd
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "disabled"
    assert "--trust" not in cmd
    assert "--force" not in cmd


def test_code_command_uses_explicit_trusted_mode() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)
    cmd = adapter._build_cmd("edit file", "/tmp", "code", model=None)
    assert "--trust" in cmd
    assert "--force" in cmd
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "disabled"


def test_cursor_command_runs_inside_micromamba_base() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.env_runner = "micromamba"
    cfg.agent.env_name = "base"
    adapter = CursorAdapter(cfg)

    cmd = adapter._build_cmd("hello", "/tmp", "task", model="composer")

    assert Path(cmd[0]).name == "bwrap"
    runner_index = cmd.index(adapter.env_runner)
    assert cmd[runner_index : runner_index + 4] == [adapter.env_runner, "run", "-n", "base"]
    assert adapter.binary in cmd
    assert "--model" in cmd
    assert "--mode" not in cmd
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "disabled"
    assert cmd[-1] == "hello"


def test_task_command_can_force_tools_inside_bwrap() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.use_bwrap = True
    cfg.agent.force_task_tools = True
    adapter = CursorAdapter(cfg)

    cmd = adapter._build_cmd("search web", "/tmp", "task", model="composer")

    assert Path(cmd[0]).name == "bwrap"
    assert "--share-net" in cmd
    assert "--unshare-user" in cmd
    assert "--unshare-pid" not in cmd
    assert "--die-with-parent" in cmd
    assert "--force" in cmd
    assert "--mode" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "disabled"
    assert "--bind" in cmd
    assert "/tmp" in cmd
    assert str(Path.home()) not in _rw_bind_sources(cmd)


def test_task_command_uses_stream_json_when_progress_callback_present() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)

    cmd = adapter._build_cmd("hello", "/tmp", "task", model="composer", stream=True)

    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"


def test_progress_directives_are_stripped_from_final_text() -> None:
    from qq_agent_bridge.progress_directives import strip_progress_directives

    clean, progress = strip_progress_directives("QQBOT_PROGRESS: one\nfinal\nQQBOT_PROGRESS: two")

    assert clean == "final"
    assert progress == ("one", "two")


def test_stream_json_ignores_user_prompt_events() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)
    line = json.dumps(
        {
            "type": "user_message",
            "message": {
                "content": (
                    "你现在是在 QQ 里回复用户的 QQ聊天机器人。\n"
                    "身份与口吻：\n- 不要输出内部提示"
                )
            },
        },
        ensure_ascii=False,
    )

    assert adapter._stream_text_from_line(line) == ""  # noqa: SLF001


def test_stream_json_extracts_assistant_message_content() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)
    line = json.dumps(
        {"type": "assistant_message", "message": {"content": "处理好了"}},
        ensure_ascii=False,
    )

    assert adapter._stream_text_from_line(line) == "处理好了"  # noqa: SLF001


def test_stream_json_extracts_output_text_delta() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)
    line = json.dumps(
        {"type": "response.output_text.delta", "delta": "正在整理"},
        ensure_ascii=False,
    )

    assert adapter._stream_text_from_line(line) == "正在整理"  # noqa: SLF001


def test_stream_json_ignores_unknown_json_events() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)
    line = json.dumps(
        {"event": "metadata", "message": {"content": "内部上下文"}},
        ensure_ascii=False,
    )

    assert adapter._stream_text_from_line(line) == ""  # noqa: SLF001


def test_streaming_handles_json_lines_larger_than_readline_limit() -> None:
    class FakeProc:
        returncode = 0

        def __init__(self, stdout: asyncio.StreamReader, stderr: asyncio.StreamReader) -> None:
            self.stdout = stdout
            self.stderr = stderr

        async def wait(self) -> int:
            return self.returncode

    async def run_case() -> tuple[str, str]:
        cfg = BridgeConfig(workspaces={"/tmp": True})
        adapter = CursorAdapter(cfg)
        stdout = asyncio.StreamReader(limit=64 * 1024)
        stderr = asyncio.StreamReader(limit=64 * 1024)
        long_text = "x" * (70 * 1024)
        line = (
            json.dumps(
                {"type": "response.output_text.delta", "delta": long_text},
                ensure_ascii=False,
            )
            + "\n"
        )
        stdout.feed_data(line.encode("utf-8"))
        stdout.feed_eof()
        stderr.feed_eof()

        progress: list[str] = []

        async def record_progress(text: str) -> None:
            progress.append(text)

        out, err = await adapter._communicate_streaming(  # noqa: SLF001
            FakeProc(stdout, stderr),  # type: ignore[arg-type]
            record_progress,
        )
        assert progress == []
        return out, err

    out, err = asyncio.run(run_case())

    assert out == "x" * (70 * 1024)
    assert err == ""


def test_streaming_sends_intermediate_assistant_messages_as_progress() -> None:
    class FakeProc:
        returncode = 0

        def __init__(self, stdout: asyncio.StreamReader, stderr: asyncio.StreamReader) -> None:
            self.stdout = stdout
            self.stderr = stderr

        async def wait(self) -> int:
            return self.returncode

    async def run_case() -> tuple[str, list[str]]:
        cfg = BridgeConfig(workspaces={"/tmp": True})
        adapter = CursorAdapter(cfg)
        stdout = asyncio.StreamReader(limit=64 * 1024)
        stderr = asyncio.StreamReader(limit=64 * 1024)
        lines = [
            {"type": "assistant_message", "message": {"content": "我先查一下资料。"}},
            {"type": "assistant_message", "message": {"content": "最终结果：查完了。"}},
        ]
        stdout.feed_data(("\n".join(json.dumps(item, ensure_ascii=False) for item in lines) + "\n").encode("utf-8"))
        stdout.feed_eof()
        stderr.feed_eof()
        progress: list[str] = []

        async def record_progress(text: str) -> None:
            progress.append(text)

        out, _err = await adapter._communicate_streaming(  # noqa: SLF001
            FakeProc(stdout, stderr),  # type: ignore[arg-type]
            record_progress,
        )
        return out, progress

    out, progress = asyncio.run(run_case())

    assert progress == ["我先查一下资料。"]
    assert out == "最终结果：查完了。"


def test_streaming_sends_intermediate_assistant_message_before_process_exit() -> None:
    class FakeProc:
        returncode = 0

        def __init__(self, stdout: asyncio.StreamReader, stderr: asyncio.StreamReader) -> None:
            self.stdout = stdout
            self.stderr = stderr

        async def wait(self) -> int:
            return self.returncode

    async def run_case() -> str:
        cfg = BridgeConfig(workspaces={"/tmp": True})
        adapter = CursorAdapter(cfg)
        stdout = asyncio.StreamReader(limit=64 * 1024)
        stderr = asyncio.StreamReader(limit=64 * 1024)
        progress: list[str] = []
        progress_seen = asyncio.Event()

        async def record_progress(text: str) -> None:
            progress.append(text)
            progress_seen.set()

        task = asyncio.create_task(
            adapter._communicate_streaming(  # noqa: SLF001
                FakeProc(stdout, stderr),  # type: ignore[arg-type]
                record_progress,
            )
        )
        first = {"type": "assistant_message", "message": {"content": "我先查一下资料。"}}
        second = {"type": "assistant_message", "message": {"content": "最终结果：查完了。"}}
        stdout.feed_data(
            (json.dumps(first, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await asyncio.sleep(0)
        stdout.feed_data(
            (json.dumps(second, ensure_ascii=False) + "\n").encode("utf-8")
        )

        try:
            await asyncio.wait_for(progress_seen.wait(), timeout=0.2)
        finally:
            stdout.feed_eof()
            stderr.feed_eof()
        out, _err = await task

        assert progress == ["我先查一下资料。"]
        return out

    out = asyncio.run(run_case())

    assert out == "最终结果：查完了。"


def test_streaming_sends_tool_call_progress_before_process_exit() -> None:
    class FakeProc:
        returncode = 0

        def __init__(self, stdout: asyncio.StreamReader, stderr: asyncio.StreamReader) -> None:
            self.stdout = stdout
            self.stderr = stderr

        async def wait(self) -> int:
            return self.returncode

    async def run_case() -> str:
        cfg = BridgeConfig(workspaces={"/tmp": True})
        adapter = CursorAdapter(cfg)
        stdout = asyncio.StreamReader(limit=64 * 1024)
        stderr = asyncio.StreamReader(limit=64 * 1024)
        progress: list[str] = []
        progress_seen = asyncio.Event()

        async def record_progress(text: str) -> None:
            progress.append(text)
            progress_seen.set()

        task = asyncio.create_task(
            adapter._communicate_streaming(  # noqa: SLF001
                FakeProc(stdout, stderr),  # type: ignore[arg-type]
                record_progress,
            )
        )
        started = {
            "type": "tool_call",
            "subtype": "started",
            "tool_call": {"description": "查看图片生成与 QQ 发送规范"},
        }
        stdout.feed_data(
            (json.dumps(started, ensure_ascii=False) + "\n").encode("utf-8")
        )

        try:
            await asyncio.wait_for(progress_seen.wait(), timeout=0.2)
        finally:
            stdout.feed_data(
                (
                    json.dumps(
                        {"type": "assistant", "message": {"content": "最终结果"}},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            stdout.feed_eof()
            stderr.feed_eof()
        out, _err = await task

        assert progress == ["正在执行：查看图片生成与 QQ 发送规范"]
        return out

    out = asyncio.run(run_case())

    assert out == "最终结果"


def test_ask_command_does_not_force_tools_inside_bwrap() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.use_bwrap = True
    cfg.agent.force_task_tools = True
    adapter = CursorAdapter(cfg)

    cmd = adapter._build_cmd("hello", "/tmp", "ask", model="auto")

    assert Path(cmd[0]).name == "bwrap"
    assert "--force" not in cmd
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "ask"
    assert cmd[cmd.index("--sandbox") + 1] == "disabled"


def test_non_bwrap_task_keeps_cursor_sandbox_enabled_without_force() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.use_bwrap = False
    cfg.agent.force_task_tools = False
    adapter = CursorAdapter(cfg)

    cmd = adapter._build_cmd("hello", "/tmp", "task", model=None)

    assert Path(cmd[0]).name == "micromamba"
    assert "--mode" not in cmd
    assert "--force" not in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "enabled"


def test_bwrap_mounts_cursor_runtime_read_only_and_workspace_writable() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.use_bwrap = True
    adapter = CursorAdapter(cfg)
    home = Path.home()

    cmd = adapter._build_cmd("hello", "/tmp", "code", model=None)

    assert _has_bind(cmd, "--bind", "/tmp", "/tmp")
    assert _has_bind(cmd, "--bind", "/tmp/qq-agent-bridge/agent-home", str(home))
    assert _has_bind(cmd, "--ro-bind", str(home / ".local/bin"), str(home / ".local/bin"))
    assert _has_bind(
        cmd,
        "--ro-bind",
        str(home / ".local/share/cursor-agent"),
        str(home / ".local/share/cursor-agent"),
    )
    assert _has_bind(
        cmd,
        "--ro-bind",
        str(home / ".local/share/mamba"),
        str(home / ".local/share/mamba"),
    )
    assert not _has_bind(
        cmd,
        "--ro-bind",
        str(home / ".cursor/cli-config.json"),
        str(home / ".cursor/cli-config.json"),
    )
    assert not _has_bind(
        cmd,
        "--ro-bind",
        str(home / ".config/cursor/auth.json"),
        str(home / ".config/cursor/auth.json"),
    )


def test_task_mounts_workspace_read_only_with_outgoing_dir_writable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.use_bwrap = True
    adapter = CursorAdapter(cfg)

    cmd = adapter._build_cmd("hello", str(workspace), "task", model=None)
    outgoing = workspace / "downloads/qq-agent-bridge/outgoing"

    assert _has_bind(cmd, "--ro-bind", str(workspace), str(workspace))
    assert not _has_bind(cmd, "--bind", str(workspace), str(workspace))
    assert _has_bind(cmd, "--bind", str(outgoing), str(outgoing))


def test_ask_mounts_workspace_read_only_without_outgoing_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.use_bwrap = True
    adapter = CursorAdapter(cfg)

    cmd = adapter._build_cmd("hello", str(workspace), "ask", model=None)
    outgoing = workspace / "downloads/qq-agent-bridge/outgoing"

    assert _has_bind(cmd, "--ro-bind", str(workspace), str(workspace))
    assert not _has_bind(cmd, "--bind", str(workspace), str(workspace))
    assert not _has_bind(cmd, "--bind", str(outgoing), str(outgoing))


def test_bwrap_mounts_home_before_cursor_runtime() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.use_bwrap = True
    adapter = CursorAdapter(cfg)
    home = Path.home()

    cmd = adapter._build_cmd("hello", "/tmp", "task", model=None)

    home_bind = _bind_index(cmd, "--bind", "/tmp/qq-agent-bridge/agent-home", str(home))
    local_bin_bind = _bind_index(
        cmd,
        "--ro-bind",
        str(home / ".local/bin"),
        str(home / ".local/bin"),
    )
    assert home_bind < local_bin_bind


def test_bwrap_prepare_copies_cursor_state_to_sandbox_home(tmp_path: Path, monkeypatch: object) -> None:
    fake_home = tmp_path / "real-home"
    fake_home.mkdir()
    (fake_home / ".cursor").mkdir()
    (fake_home / ".config" / "cursor").mkdir(parents=True)
    (fake_home / ".cursor" / "cli-config.json").write_text('{"authInfo": {}}', encoding="utf-8")
    (fake_home / ".cursor" / "agent-cli-state.json").write_text("{}", encoding="utf-8")
    (fake_home / ".config" / "cursor" / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))  # type: ignore[attr-defined]

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox_home = tmp_path / "sandbox-home"
    sandbox_home.mkdir(mode=0o755)
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.sandbox_home = str(sandbox_home)
    adapter = CursorAdapter(cfg)

    error = adapter._prepare_bwrap(str(workspace), "task")  # noqa: SLF001

    assert error is None
    assert (sandbox_home / ".cursor" / "cli-config.json").read_text(encoding="utf-8") == '{"authInfo": {}}'
    assert (sandbox_home / ".cursor" / "agent-cli-state.json").read_text(encoding="utf-8") == "{}"
    assert (sandbox_home / ".config" / "cursor" / "auth.json").read_text(encoding="utf-8") == '{"token":"secret"}'
    assert oct(sandbox_home.stat().st_mode & 0o777) == "0o700"
    assert oct((sandbox_home / ".config" / "cursor" / "auth.json").stat().st_mode & 0o777) == "0o600"


def test_bwrap_rejects_sandbox_home_inside_workspace(tmp_path: Path, monkeypatch: object) -> None:
    fake_home = tmp_path / "real-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))  # type: ignore[attr-defined]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.sandbox_home = str(workspace / ".qq-agent-sandbox/home")
    adapter = CursorAdapter(cfg)

    error = adapter._prepare_bwrap(str(workspace), "task")  # noqa: SLF001

    assert error == "[error] 助手沙箱未配置"


def test_bwrap_rejects_symlinked_sandbox_home(tmp_path: Path, monkeypatch: object) -> None:
    fake_home = tmp_path / "real-home"
    fake_home.mkdir()
    (fake_home / ".cursor").mkdir()
    (fake_home / ".cursor" / "cli-config.json").write_text('{"authInfo": {}}', encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))  # type: ignore[attr-defined]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    leak = tmp_path / "leak"
    leak.mkdir()
    sandbox_home = tmp_path / "cursor-home-link"
    sandbox_home.symlink_to(leak, target_is_directory=True)
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.sandbox_home = str(sandbox_home)
    adapter = CursorAdapter(cfg)

    error = adapter._prepare_bwrap(str(workspace), "task")  # noqa: SLF001

    assert error == "[error] 助手沙箱未配置"
    assert not (leak / ".cursor" / "cli-config.json").exists()


def test_bwrap_rejects_relative_binary_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.bwrap_binary = "./bwrap"
    adapter = CursorAdapter(cfg)

    error = adapter._prepare_bwrap(str(workspace), "ask")  # noqa: SLF001

    assert error == "[error] 助手沙箱未配置"


def test_bwrap_rejects_path_resolved_from_tmp(
    tmp_path: Path, monkeypatch: object
) -> None:
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_bwrap.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path))
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.bwrap_binary = "bwrap"
    adapter = CursorAdapter(cfg)

    error = adapter._prepare_bwrap(str(workspace), "ask")  # noqa: SLF001

    assert error == "[error] 助手沙箱未配置"


def test_bwrap_prepare_trusts_only_current_sandbox_workspace(
    tmp_path: Path, monkeypatch: object
) -> None:
    fake_home = tmp_path / "real-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))  # type: ignore[attr-defined]

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox_home = tmp_path / "sandbox-home"
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.sandbox_home = str(sandbox_home)
    adapter = CursorAdapter(cfg)

    error = adapter._prepare_bwrap(str(workspace), "task")  # noqa: SLF001

    assert error is None
    project_dir = adapter._cursor_project_dir_name(workspace.resolve(strict=False))  # noqa: SLF001
    trust_file = sandbox_home / ".cursor/projects" / project_dir / ".workspace-trusted"
    trust = json.loads(trust_file.read_text(encoding="utf-8"))
    assert trust["workspacePath"] == str(workspace.resolve(strict=False))
    assert trust["trustedAt"].endswith("Z")
    assert oct(trust_file.stat().st_mode & 0o777) == "0o600"


def test_force_task_tools_requires_bwrap() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.use_bwrap = False
    cfg.agent.force_task_tools = True
    adapter = CursorAdapter(cfg)

    result = asyncio.run(adapter.run("hello", "/tmp", "task"))

    assert result == "[error] 助手沙箱未配置"


def test_bwrap_must_be_available_when_enabled() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.use_bwrap = True
    cfg.agent.bwrap_binary = "/not/a/real/bwrap"
    adapter = CursorAdapter(cfg)

    result = asyncio.run(adapter.run("hello", "/tmp", "ask"))

    assert result == "[error] 助手沙箱未配置"


def test_cursor_subprocess_starts_in_new_session(
    tmp_path: Path, monkeypatch: object
) -> None:
    fake_cli = tmp_path / "fake-cursor"
    fake_cli.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
    fake_cli.chmod(0o755)

    captured: dict[str, object] = {}
    original_create = asyncio.create_subprocess_exec

    async def capture_create(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        captured["start_new_session"] = kwargs.get("start_new_session")
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_create)

    cfg = BridgeConfig(workspaces={str(tmp_path): True})
    cfg.agent.binary = str(fake_cli)
    cfg.agent.env_runner = ""
    cfg.agent.require_env = False
    cfg.agent.use_bwrap = False
    adapter = CursorAdapter(cfg)

    result = asyncio.run(adapter.run("hello", str(tmp_path), "ask"))

    assert result == "ok"
    assert captured["start_new_session"] is True


def test_timeout_kills_and_reaps_subprocess(tmp_path: Path, monkeypatch: object) -> None:
    class FakeProc:
        pid = 4242
        returncode = None
        waited = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(60)
            return b"", b""

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            self.waited = True
            self.returncode = -9
            return self.returncode

    fake_proc = FakeProc()

    async def fake_create(*args: object, **kwargs: object) -> FakeProc:
        return fake_proc

    killed: dict[str, int] = {}

    def fake_killpg(pid: int, sig: int) -> None:
        killed["pid"] = pid
        killed["sig"] = sig

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr("os.killpg", fake_killpg)

    cfg = BridgeConfig(workspaces={str(tmp_path): True})
    cfg.agent.env_runner = ""
    cfg.agent.require_env = False
    cfg.agent.use_bwrap = False
    cfg.agent.max_runtime_seconds = 0.01
    adapter = CursorAdapter(cfg)

    result = asyncio.run(adapter.run("hello", str(tmp_path), "ask"))

    assert result == "[error] 助手响应超时"
    assert killed["pid"] == 4242
    assert fake_proc.waited


def test_cursor_run_fails_closed_when_required_env_is_disabled() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.env_runner = ""
    adapter = CursorAdapter(cfg)

    result = asyncio.run(adapter.run("hello", "/tmp", "ask"))

    assert result == "[error] 助手环境未配置"


def test_cursor_run_fails_closed_when_required_env_is_not_micromamba() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.env_runner = "/tmp/fake-runner"
    cfg.agent.env_name = "base"
    adapter = CursorAdapter(cfg)

    result = asyncio.run(adapter.run("hello", "/tmp", "ask"))

    assert result == "[error] 助手环境未配置"


def test_cursor_run_fails_closed_when_required_env_name_is_not_base() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.env_runner = "micromamba"
    cfg.agent.env_name = "dev"
    adapter = CursorAdapter(cfg)

    result = asyncio.run(adapter.run("hello", "/tmp", "ask"))

    assert result == "[error] 助手环境未配置"


def test_model_is_added_when_configured() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)

    cmd = adapter._build_cmd("hello", "/tmp", "ask", model="auto")

    assert "--model" in cmd
    model_index = cmd.index("--model")
    assert cmd[model_index + 1] == "auto"
    assert cmd[-1] == "hello"


def test_plan_command_is_read_only() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)
    cmd = adapter._build_cmd("make a plan", "/tmp", "plan", model=None)
    assert "--mode" in cmd
    assert "plan" in cmd
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "disabled"
    assert "--trust" not in cmd
    assert "--force" not in cmd


def test_unknown_mode_fails_closed() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    adapter = CursorAdapter(cfg)
    try:
        adapter._build_cmd("hello", "/tmp", "typo", model=None)
    except ValueError as exc:
        assert "unsupported cursor mode" in str(exc)
    else:
        raise AssertionError("unknown mode should fail closed")


def test_user_visible_errors_hide_cursor_name() -> None:
    cfg = BridgeConfig(workspaces={"/tmp": True})
    cfg.agent.binary = "/definitely/not/a/real/cursor-agent"
    cfg.agent.env_runner = ""
    cfg.agent.require_env = False
    adapter = CursorAdapter(cfg)
    result = asyncio.run(adapter.run("hello", "/tmp", "ask"))
    assert "cursor" not in result.lower()
    assert "agent" not in result.lower()


def test_nonzero_subprocess_output_is_generic(tmp_path: Path) -> None:
    fake_cli = tmp_path / "fake-cursor"
    fake_cli.write_text(
        "#!/bin/sh\n"
        "printf 'Cursor Agent failed inside CLI runtime' >&2\n"
        "exit 42\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)

    cfg = BridgeConfig(workspaces={str(tmp_path): True})
    cfg.agent.binary = str(fake_cli)
    cfg.agent.env_runner = ""
    cfg.agent.require_env = False
    adapter = CursorAdapter(cfg)

    result = asyncio.run(adapter.run("hello", str(tmp_path), "ask"))

    assert result == "[error] 助手执行失败"
    lowered = result.lower()
    assert "cursor" not in lowered
    assert "agent" not in lowered
    assert "cli" not in lowered


def _has_bind(cmd: list[str], flag: str, source: str, target: str) -> bool:
    for idx, item in enumerate(cmd[:-2]):
        if item == flag and cmd[idx + 1] == source and cmd[idx + 2] == target:
            return True
    return False


def _rw_bind_sources(cmd: list[str]) -> list[str]:
    return [cmd[idx + 1] for idx, item in enumerate(cmd[:-2]) if item == "--bind"]


def _bind_index(cmd: list[str], flag: str, source: str, target: str) -> int:
    for idx, item in enumerate(cmd[:-2]):
        if item == flag and cmd[idx + 1] == source and cmd[idx + 2] == target:
            return idx
    raise AssertionError(f"{flag} {source} {target} not found in command")
