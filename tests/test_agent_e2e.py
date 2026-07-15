"""Optional end-to-end checks against a real configured agent runtime."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
import re
import shlex
import shutil
import stat
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.agent_runtime import build_agent_adapter  # type: ignore
from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.main import App  # type: ignore
from qq_agent_bridge.onebot import _normalize_event  # type: ignore
from qq_agent_bridge.policy import Policy  # type: ignore
from qq_agent_bridge.prompting import build_agent_prompt  # type: ignore
from qq_agent_bridge.schedule_parser import NaturalLanguageScheduleParser  # type: ignore
from qq_agent_bridge.types import ChatEvent  # type: ignore


_E2E_ENV = "QQ_AGENT_BRIDGE_AGENT_E2E"
_APP_E2E_ENV = "QQ_AGENT_BRIDGE_APP_E2E"
_SEND_FILE_RE = re.compile(r"QQBOT_SEND_FILE:\s+(?P<token>\S+)\s+(?P<path>\S+)")


def _require_e2e() -> None:
    if os.environ.get(_E2E_ENV) != "1":
        pytest.skip(f"set {_E2E_ENV}=1 to run real agent E2E tests")


def _require_app_e2e() -> None:
    if os.environ.get(_APP_E2E_ENV) != "1":
        pytest.skip(f"set {_APP_E2E_ENV}=1 to run real App+agent E2E tests")


def _make_cfg(workspace: Path, mode: str = "ask") -> BridgeConfig:
    runtime = os.environ.get("QQ_AGENT_BRIDGE_E2E_RUNTIME", "cursor-cli")
    cfg = BridgeConfig(workspaces={str(workspace): True})
    cfg.agent.runtime = runtime
    cfg.agent.default_workspace = str(workspace)
    cfg.agent.binary = os.environ.get("QQ_AGENT_BRIDGE_E2E_BINARY", "")
    cfg.agent.env_runner = os.environ.get("QQ_AGENT_BRIDGE_E2E_ENV_RUNNER", "")
    cfg.agent.env_name = os.environ.get("QQ_AGENT_BRIDGE_E2E_ENV_NAME", "")
    cfg.agent.require_env = False
    cfg.agent.use_bwrap = os.environ.get("QQ_AGENT_BRIDGE_E2E_BWRAP", "1") != "0"
    if runtime == "cursor-cli" and cfg.agent.use_bwrap and not shutil.which(cfg.agent.bwrap_binary):
        pytest.skip("cursor-cli E2E needs bwrap for temporary workspace trust; set QQ_AGENT_BRIDGE_E2E_BWRAP=0 to override")
    cfg.agent.force_task_tools = runtime == "cursor-cli" and cfg.agent.use_bwrap
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


class _CaptureAdapter:
    def __init__(self, get_record_response: dict[str, str] | None = None) -> None:
        self.events: list[tuple[str, Any]] = []
        self.sent: list[tuple[str, bool, str, str | None]] = []
        self.sent_images: list[tuple[str, bool, Path, str | None]] = []
        self.sent_files: list[tuple[str, bool, Path, str | None]] = []
        self.sent_voices: list[tuple[str, bool, Path, str | None]] = []
        self.get_record_response = get_record_response or {}
        self.get_record_calls: list[dict[str, str | None]] = []

    async def resolve_record_url(self, resource: Any) -> str | None:
        self.get_record_calls.append(
            {"file": resource.file_id or resource.url, "out_format": "wav"}
        )
        return self.get_record_response.get("url")

    async def send(
        self,
        chat_id: str,
        is_group: bool,
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        self.sent.append((chat_id, is_group, text, echo))
        self.events.append(("text", text))

    async def send_image(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        self.sent_images.append((chat_id, is_group, path, echo))
        self.events.append(("image", path))

    async def send_file(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        self.sent_files.append((chat_id, is_group, path, echo))
        self.events.append(("file", path))

    async def send_voice(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        self.sent_voices.append((chat_id, is_group, path, echo))
        self.events.append(("voice", path))


async def _wait_for(condition: Any, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.25)
    raise AssertionError("timed out waiting for E2E condition")


def _write_fake_whisper(
    tmp_path: Path, *, transcript: str = "", exit_code: int = 0
) -> tuple[Path, Path]:
    binary = tmp_path / "fake-whisper.py"
    binary.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import pathlib
            import sys

            args = sys.argv[1:]
            if {exit_code} == 0:
                pathlib.Path(args[args.index("-of") + 1]).with_suffix(".txt").write_text(
                    {transcript!r}, encoding="utf-8"
                )
            raise SystemExit({exit_code})
            """
        ),
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    model = tmp_path / "model.bin"
    model.write_bytes(b"fake whisper model")
    return binary, model


def test_cursor_e2e_uses_bwrap_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("QQ_AGENT_BRIDGE_E2E_RUNTIME", raising=False)
    monkeypatch.delenv("QQ_AGENT_BRIDGE_E2E_BWRAP", raising=False)

    cfg = _make_cfg(tmp_path, "ask")

    assert cfg.agent.runtime == "cursor-cli"
    assert cfg.agent.use_bwrap


def test_cursor_e2e_forces_task_tools_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("QQ_AGENT_BRIDGE_E2E_RUNTIME", raising=False)
    monkeypatch.delenv("QQ_AGENT_BRIDGE_E2E_BWRAP", raising=False)

    cfg = _make_cfg(tmp_path, "task")

    assert cfg.agent.force_task_tools


def test_cursor_e2e_can_disable_bwrap_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("QQ_AGENT_BRIDGE_E2E_RUNTIME", "cursor-cli")
    monkeypatch.setenv("QQ_AGENT_BRIDGE_E2E_BWRAP", "0")

    cfg = _make_cfg(tmp_path, "ask")

    assert not cfg.agent.use_bwrap


@pytest.mark.parametrize(
    ("exit_code", "transcript"),
    [(0, "我是测试语音"), (2, "")],
    ids=("success", "whisper-failure"),
)
def test_voice_transcription_e2e_injects_only_verified_transcript(
    tmp_path: Path, exit_code: int, transcript: str
) -> None:
    async def go() -> None:
        binary, model = _write_fake_whisper(
            tmp_path, transcript=transcript, exit_code=exit_code
        )
        cfg = _make_cfg(tmp_path, "task")
        cfg.allowed_users = ["10001"]
        cfg.whisper.enabled = True
        cfg.whisper.binary = str(binary)
        cfg.whisper.model = str(model)
        cfg.whisper.cache_enabled = False
        cfg.whisper.cache_root = str(tmp_path / "whisper-cache")
        adapter = _CaptureAdapter({"url": "https://onebot.invalid/voice.wav"})
        prompts: list[str] = []
        modes: list[str] = []

        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.resources = app._build_resource_manager(cfg)

        async def fake_fetch(url: str, _limit: int) -> tuple[bytes, str]:
            assert url == "https://onebot.invalid/voice.wav"
            return b"RIFFfake-wav", "audio/wav"

        app.resources.fetch = fake_fetch

        async def fake_agent(
            prompt: str,
            _workspace: str,
            mode: str,
            model: str | None = None,
            progress: Any = None,
        ) -> str:
            prompts.append(prompt)
            modes.append(mode)
            return "语音任务已处理"

        app.agent.run = fake_agent  # type: ignore[method-assign]
        app.policy = Policy(cfg, app._agent_runner)
        event = _normalize_event(
            {
                "post_type": "message",
                "message_type": "private",
                "self_id": "1000000001",
                "user_id": 10001,
                "message_id": "voice-transcription-e2e",
                "time": 1,
                "message": [
                    {"type": "text", "data": {"text": "/task 请转写这条语音"}},
                    {"type": "record", "data": {"file": "voice.silk", "duration": 2}},
                ],
            },
            "1000000001",
        )
        assert event is not None
        assert not event.is_group
        assert event.mentioned_bot
        assert event.resources[0].kind == "voice"

        await app._handle(event)
        await _wait_for(lambda: len(prompts) == 1, timeout=5.0)

        assert adapter.get_record_calls == [{"file": "voice.silk", "out_format": "wav"}]
        assert len(prompts) == 1
        assert modes == ["task"]
        if exit_code == 0:
            assert (
                "  transcript (verified by local Whisper, language=zh): 我是测试语音"
                in prompts[0]
            )
            assert prompts[0].count("我是测试语音") == 1
        else:
            assert "  transcript: unavailable (Whisper failed)" in prompts[0]
            assert "我是测试语音" not in prompts[0]
            assert "猜测" not in prompts[0]

    asyncio.run(go())


async def _run_agent(prompt: str, cfg: BridgeConfig, mode: str, model: str | None) -> str:
    adapter = build_agent_adapter(cfg)
    return await adapter.run(prompt, cfg.agent.default_workspace, mode, model=model)


def test_real_agent_interprets_arbitrary_schedule_as_rrule(tmp_path: Path) -> None:
    _require_e2e()
    cfg = _make_cfg(tmp_path, "ask")
    cfg.scheduler.enabled = True
    cfg.scheduler.timezone = "Asia/Shanghai"
    cfg.scheduler.natural_language_model = os.environ.get(
        "QQ_AGENT_BRIDGE_E2E_CHAT_MODEL",
        "auto",
    )
    adapter = build_agent_adapter(cfg)

    class ReadOnlyE2EAdapter:
        async def run(
            self,
            prompt: str,
            workspace: str,
            mode: str,
            model: str | None = None,
            progress=None,
        ) -> str:
            runner_mode = (
                "task"
                if cfg.agent.runtime == "cursor-cli" and cfg.agent.use_bwrap
                else mode
            )
            return await adapter.run(prompt, workspace, runner_mode, model=model)

    parser = NaturalLanguageScheduleParser(cfg, ReadOnlyE2EAdapter())

    outcome = asyncio.run(
        parser.parse(
            "每月最后一个工作日下午六点整理本月工作",
            now=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        )
    )

    assert outcome.spec is not None, outcome.clarification
    assert outcome.spec.kind == "rrule"
    assert outcome.spec.action in {"ask", "task"}
    assert {
        "FREQ=MONTHLY",
        "BYDAY=MO,TU,WE,TH,FR",
        "BYSETPOS=-1",
    }.issubset(set((outcome.spec.rrule or "").split(";")))


def test_real_agent_schedule_separates_mention_connector_from_send_text(
    tmp_path: Path,
) -> None:
    _require_e2e()
    cfg = _make_cfg(tmp_path, "ask")
    cfg.scheduler.enabled = True
    cfg.scheduler.timezone = "Asia/Shanghai"
    cfg.scheduler.natural_language_model = os.environ.get(
        "QQ_AGENT_BRIDGE_E2E_CHAT_MODEL",
        "auto",
    )
    adapter = build_agent_adapter(cfg)

    class ReadOnlyE2EAdapter:
        async def run(
            self,
            prompt: str,
            workspace: str,
            mode: str,
            model: str | None = None,
            progress=None,
        ) -> str:
            runner_mode = (
                "task"
                if cfg.agent.runtime == "cursor-cli" and cfg.agent.use_bwrap
                else mode
            )
            return await adapter.run(prompt, workspace, runner_mode, model=model)

    parser = NaturalLanguageScheduleParser(cfg, ReadOnlyE2EAdapter())
    outcome = asyncio.run(
        parser.parse(
            "每过1分钟就 @1583165466 并说谢森同我爱你",
            now=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
            mentions=("1583165466",),
        )
    )

    assert outcome.spec is not None, outcome.clarification
    assert outcome.spec.action == "send"
    assert outcome.spec.mentions == ("1583165466",)
    assert outcome.spec.payload == "谢森同我爱你"


def test_real_agent_can_return_a_fixed_token(tmp_path: Path) -> None:
    _require_e2e()
    cfg = _make_cfg(tmp_path, "task")
    token = "QQ_AGENT_BRIDGE_E2E_OK_FIXED"
    prompt = build_agent_prompt(
        "ask",
        f"只输出这个 token，不要输出其他文字：{token}",
        _make_ev("固定 token"),
        profile_prompt="你是 QQ bot 测试对象，必须严格遵守当前用户要求。",
    )

    out = asyncio.run(_run_agent(prompt, cfg, "task", os.environ.get("QQ_AGENT_BRIDGE_E2E_CHAT_MODEL", "auto")))

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


def test_real_app_task_can_stream_progress_and_send_voice(tmp_path: Path) -> None:
    _require_app_e2e()
    cfg = _make_cfg(tmp_path, "task")
    cfg.allowed_users = ["e2e-user"]
    cfg.agent.max_runtime_seconds = int(os.environ.get("QQ_AGENT_BRIDGE_APP_E2E_TIMEOUT", "180"))
    cfg.max_runtime_seconds = cfg.agent.max_runtime_seconds
    cfg.progress.first_heartbeat_seconds = cfg.agent.max_runtime_seconds
    cfg.progress.heartbeat_seconds = cfg.agent.max_runtime_seconds
    cfg.progress.min_progress_interval_seconds = 0
    cfg.progress.max_progress_messages = 12
    cfg.resources.max_bytes = 2 * 1024 * 1024
    adapter = _CaptureAdapter()
    app = App(cfg)
    app.adapter = adapter  # type: ignore[assignment]
    app.policy = Policy(cfg, app._agent_runner)
    prompt = (
        "/task 端到端测试：请先输出一条 QQBOT_PROGRESS: 正在准备测试语音。"
        "然后在可发送资源目录创建一个 1 秒、可播放、非空的 wav 测试人声文件；"
        "文件准备好后用 QQBOT_SEND_VOICE 指令发送，duration=1。最终不要额外解释。"
    )

    async def go() -> None:
        await app._handle(_make_ev(prompt))
        await _wait_for(lambda: bool(adapter.sent_voices), timeout=cfg.agent.max_runtime_seconds)

    asyncio.run(go())

    texts = [text for _chat_id, _is_group, text, _echo in adapter.sent]
    assert texts[0] == "收到，我处理一下。"
    assert any("正在准备测试语音" in text for text in texts), texts
    forbidden = ("QQBOT_SEND", "Cursor", "NapCat", "OneBot", "edge-tts", "调用工具", "/home/")
    assert not any(item in text for text in texts for item in forbidden), texts
    first_progress_index = next(i for i, item in enumerate(adapter.events) if item[0] == "text" and "正在准备测试语音" in item[1])
    first_voice_index = next(i for i, item in enumerate(adapter.events) if item[0] == "voice")
    assert first_progress_index < first_voice_index
    assert len(adapter.sent_voices) == 1
    voice_path = adapter.sent_voices[0][2]
    assert voice_path.exists()
    assert voice_path.stat().st_size > 0
    assert "sending" in voice_path.parts
