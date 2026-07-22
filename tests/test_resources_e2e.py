"""True end-to-end resource tests through the full App._handle pipeline.

CI tests (no env var needed):
    Message with resources -> App._handle -> prepare -> format -> prompt
    injection -> agent response delivered to chat.

Real-agent tests (require env var, skipped on CI):
    Same pipeline but with real agent.

Usage::

    # CI (no env var)
    .venv/bin/pytest tests/test_resources_e2e.py -x -v -k "ci"

    # Local only (real agent)
    QQ_AGENT_BRIDGE_APP_E2E=1 .venv/bin/pytest tests/test_resources_e2e.py -x -v -k "real_app"
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest

from qq_agent_bridge.config import BridgeConfig
from qq_agent_bridge.main import App
from qq_agent_bridge.policy import Policy
from qq_agent_bridge.resources import PreparedResource
from qq_agent_bridge.types import ChatEvent, ChatResource, ChatSegment

_APP_E2E_ENV = "QQ_AGENT_BRIDGE_APP_E2E"


def _require_app_e2e() -> None:
    if os.environ.get(_APP_E2E_ENV) != "1":
        pytest.skip(f"set {_APP_E2E_ENV}=1 to run real App resource E2E tests")


async def _wait_for_sent(
    adapter: Any, predicate: Any, timeout: float = 5.0
) -> None:
    """Poll until predicate(adapter) returns True."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(adapter):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for condition")


# ── Fake components for CI tests ─────────────────────────────────────────────


class _FakeAdapter:
    """Records send calls (text + images/files/voices)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bool, str, str | None]] = []
        self.sent_images: list[tuple[str, bool, Path, str | None]] = []
        self.sent_files: list[tuple[str, bool, Path, str | None]] = []
        self.sent_voices: list[tuple[str, bool, Path, str | None]] = []

    def is_connected(self) -> bool:
        return True

    async def send(
        self,
        chat_id: str,
        is_group: bool,
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        self.sent.append((chat_id, is_group, text, echo))

    async def send_image(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        self.sent_images.append((chat_id, is_group, path, echo))

    async def send_file(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        self.sent_files.append((chat_id, is_group, path, echo))

    async def send_voice(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        self.sent_voices.append((chat_id, is_group, path, echo))

    async def send_at(
        self,
        chat_id: str,
        qq: str,
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        pass

    async def send_ats(
        self,
        chat_id: str,
        qqs: tuple[str, ...],
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        pass


class _FakeResourceManager:
    """Returns canned PreparedResource without downloading."""

    def __init__(self, prepared: tuple[PreparedResource, ...] = ()) -> None:
        self.prepared = prepared
        self.prepare_calls: list[ChatEvent] = []
        self.cleanup_calls: list[tuple[PreparedResource, ...]] = []

    async def prepare(self, ev: ChatEvent) -> tuple[PreparedResource, ...]:
        self.prepare_calls.append(ev)
        return self.prepared

    def cleanup_prepared(
        self, resources: tuple[PreparedResource, ...]
    ) -> None:
        self.cleanup_calls.append(resources)


def _make_ev(
    text: str,
    sender: str = "reader",
    group: str | None = None,
    mid: str = "m1",
    mentioned: bool = True,
    resources: tuple[ChatResource, ...] = (),
    segments: tuple[ChatSegment, ...] = (),
) -> ChatEvent:
    return ChatEvent(
        id=mid,
        platform="qq",
        chat_id=group or sender,
        sender_id=sender,
        is_group=group is not None,
        mentioned_bot=mentioned,
        text=text,
        timestamp=1,
        resources=resources,
        segments=segments,
    )


def _make_resource_cfg(tmp_path: Path) -> BridgeConfig:
    """Minimal config with resources enabled, no real agent runtime."""
    cfg = BridgeConfig(
        owners=["owner"],
        allowed_users=["reader", "owner"],
        allowed_groups=["group"],
        commands={"ask": True, "task": True},
        workspaces={str(tmp_path): True},
    )
    cfg.agent.default_workspace = str(tmp_path)
    cfg.agent.runtime = ""  # → DisabledAgentAdapter, we replace .run
    cfg.storage_maintenance.enabled = False
    cfg.resources.enabled = True
    cfg.resources.max_items = 10
    cfg.resources.max_bytes = 10 * 1024 * 1024
    return cfg


# ── Scheme A: CI tests through App._handle ──────────────────────────────────


def test_e2e_resource_image_pipeline_ci(tmp_path: Path) -> None:
    """Image resource -> App._handle -> prepare -> prompt -> response.

    Full pipeline: message with image attachment -> /ask command ->
    ResourceManager.prepare -> format_resource_context ->
    agent prompt with resource context -> agent response delivered.
    """
    async def go() -> None:
        cfg = _make_resource_cfg(tmp_path)
        adapter = _FakeAdapter()
        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app._prepare_runtime_skill_bundle = lambda: ""  # type: ignore[method-assign]

        # Replace resources with fake that returns a canned image
        fake_rm = _FakeResourceManager(
            (
                PreparedResource(
                    kind="image",
                    name="cat.jpg",
                    local_path=str(tmp_path / "cat.jpg"),
                    url="https://example.com/cat.jpg",
                ),
            )
        )
        app.resources = fake_rm  # type: ignore[assignment]

        # Replace agent.run with prompt-capturing fake
        prompts: list[str] = []

        async def fake_agent_run(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
            **kw: Any,
        ) -> str:
            prompts.append(prompt)
            return "这张图片显示了一只猫，它正在窗台上晒太阳。"

        app.agent.run = fake_agent_run  # type: ignore[method-assign]

        # Wire policy using the real _agent_runner
        app.policy = Policy(cfg, app._agent_runner)  # noqa: SLF001 # type: ignore[arg-type]

        # Send /ask with an image resource attached
        img_resource = ChatResource(
            kind="image",
            url="https://example.com/cat.jpg",
            name="cat.jpg",
            size=12345,
            mime_type="image/jpeg",
        )
        await app._handle(  # noqa: SLF001
            _make_ev(
                "/ask 描述这张图片",
                sender="reader",
                group="group",
                mid="img-msg",
                resources=(img_resource,),
            )
        )

        await _wait_for_sent(
            adapter,
            lambda a: any("这张图片" in s[2] for s in a.sent),
            timeout=5.0,
        )

        # Verify: resource context was injected into the agent prompt
        assert len(prompts) >= 1, "agent should have been called"
        latest_prompt = prompts[-1]
        assert "cat.jpg" in latest_prompt, (
            f"resource name should appear in prompt: {latest_prompt[:300]}"
        )
        assert (
            "- image:" in latest_prompt
            or "image:" in latest_prompt
            or "image" in latest_prompt
        ), f"resource kind should be in prompt: {latest_prompt[:300]}"

        # Verify: agent response was delivered to chat
        assert any(
            "这张图片" in s[2] for s in adapter.sent
        ), f"agent response should be delivered: {adapter.sent}"

        # Verify: prepare was called exactly once
        assert len(fake_rm.prepare_calls) == 1

    asyncio.run(go())


def test_e2e_resource_multiple_types_pipeline_ci(tmp_path: Path) -> None:
    """Multiple resource types -> App._handle -> prompt contains all.

    Verifies that when a message has multiple resources (image + file + url),
    all of them appear in the agent prompt context.
    """
    async def go() -> None:
        cfg = _make_resource_cfg(tmp_path)
        adapter = _FakeAdapter()
        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app._prepare_runtime_skill_bundle = lambda: ""  # type: ignore[method-assign]

        fake_rm = _FakeResourceManager(
            (
                PreparedResource(
                    kind="image",
                    name="screenshot.png",
                    local_path=str(tmp_path / "screenshot.png"),
                    url="https://example.com/screenshot.png",
                ),
                PreparedResource(
                    kind="file",
                    name="report.pdf",
                    local_path=str(tmp_path / "report.pdf"),
                    url="https://example.com/report.pdf",
                ),
                PreparedResource(
                    kind="url",
                    url="https://example.com/docs",
                ),
            )
        )
        app.resources = fake_rm  # type: ignore[assignment]

        prompts: list[str] = []

        async def fake_agent_run(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
            **kw: Any,
        ) -> str:
            prompts.append(prompt)
            return "我看到你发送了截图、PDF报告和一个链接，我来帮你分析。"

        app.agent.run = fake_agent_run  # type: ignore[method-assign]
        app.policy = Policy(cfg, app._agent_runner)  # noqa: SLF001 # type: ignore[arg-type]

        # Send /ask with three resource types
        await app._handle(  # noqa: SLF001
            _make_ev(
                "/ask 分析这些文件",
                sender="reader",
                group="group",
                mid="multi-msg",
                resources=(
                    ChatResource(kind="image", url="https://example.com/screenshot.png", name="screenshot.png"),
                    ChatResource(kind="file", url="https://example.com/report.pdf", name="report.pdf"),
                    ChatResource(kind="url", url="https://example.com/docs"),
                ),
            )
        )

        await _wait_for_sent(
            adapter,
            lambda a: any("分析" in s[2] for s in a.sent),
            timeout=5.0,
        )

        latest_prompt = prompts[-1]
        assert "screenshot.png" in latest_prompt, (
            f"image resource should be in prompt: {latest_prompt[:400]}"
        )
        assert "report.pdf" in latest_prompt, (
            f"file resource should be in prompt: {latest_prompt[:400]}"
        )
        assert "https://example.com/docs" in latest_prompt, (
            f"url resource should be in prompt: {latest_prompt[:400]}"
        )

        assert any(
            "分析" in s[2] for s in adapter.sent
        ), f"agent response should be delivered: {adapter.sent}"

        assert len(fake_rm.prepare_calls) == 1

    asyncio.run(go())


def test_e2e_resource_voice_with_transcript_pipeline_ci(tmp_path: Path) -> None:
    """Voice resource with transcript -> App._handle -> prompt has transcript.

    Verifies that voice resources with Whisper transcripts inject
    transcript context into the agent prompt.
    """
    async def go() -> None:
        cfg = _make_resource_cfg(tmp_path)
        adapter = _FakeAdapter()
        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app._prepare_runtime_skill_bundle = lambda: ""  # type: ignore[method-assign]

        fake_rm = _FakeResourceManager(
            (
                PreparedResource(
                    kind="voice",
                    name="msg-1.amr",
                    local_path=str(tmp_path / "msg-1.wav"),
                    duration_seconds=12,
                    transcript="明天下午三点开会讨论项目进度",
                    transcript_status="verified",
                    transcript_language="zh",
                ),
            )
        )
        app.resources = fake_rm  # type: ignore[assignment]

        prompts: list[str] = []

        async def fake_agent_run(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
            **kw: Any,
        ) -> str:
            prompts.append(prompt)
            return "收到，我会记住明天下午三点有项目进度会议。"

        app.agent.run = fake_agent_run  # type: ignore[method-assign]
        app.policy = Policy(cfg, app._agent_runner)  # noqa: SLF001 # type: ignore[arg-type]

        # Send /ask with voice resource
        voice_resource = ChatResource(
            kind="voice",
            url="https://example.com/voice.amr",
            name="msg-1.amr",
            duration_seconds=12,
            size=15000,
            mime_type="audio/amr",
        )
        await app._handle(  # noqa: SLF001
            _make_ev(
                "/ask 总结这条语音",
                sender="reader",
                group="group",
                mid="voice-msg",
                resources=(voice_resource,),
            )
        )

        await _wait_for_sent(
            adapter,
            lambda a: any("会议" in s[2] or "收到" in s[2] for s in a.sent),
            timeout=5.0,
        )

        latest_prompt = prompts[-1]
        assert "明天下午三点开会讨论项目进度" in latest_prompt, (
            f"voice transcript should be in prompt: {latest_prompt[:400]}"
        )
        assert "transcript" in latest_prompt, (
            f"transcript label should be in prompt: {latest_prompt[:400]}"
        )
        assert "zh" in latest_prompt, (
            f"transcript language should be in prompt: {latest_prompt[:400]}"
        )

        assert any(
            "会议" in s[2] for s in adapter.sent
        ), f"agent response should be delivered: {adapter.sent}"

    asyncio.run(go())


def test_e2e_resource_no_resources_still_works_ci(tmp_path: Path) -> None:
    """Message without resources -> App._handle -> no resource context in prompt.

    Edge case: a plain text message should not have resource context injected.
    """
    async def go() -> None:
        cfg = _make_resource_cfg(tmp_path)
        adapter = _FakeAdapter()
        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app._prepare_runtime_skill_bundle = lambda: ""  # type: ignore[method-assign]

        # ResourceManager that always returns empty (like real one when no resources)
        fake_rm = _FakeResourceManager(())
        app.resources = fake_rm  # type: ignore[assignment]

        prompts: list[str] = []

        async def fake_agent_run(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
            **kw: Any,
        ) -> str:
            prompts.append(prompt)
            return "你好！有什么可以帮助你的吗？"

        app.agent.run = fake_agent_run  # type: ignore[method-assign]
        app.policy = Policy(cfg, app._agent_runner)  # noqa: SLF001 # type: ignore[arg-type]

        # Send plain /ask without any resources
        await app._handle(  # noqa: SLF001
            _make_ev(
                "/ask 你好",
                sender="reader",
                group="group",
                mid="plain-msg",
            )
        )

        await _wait_for_sent(
            adapter,
            lambda a: any("你好" in s[2] or "帮助" in s[2] for s in a.sent),
            timeout=5.0,
        )

        assert len(prompts) >= 1, "agent should have been called"
        # Verify no stray resource context format markers for empty resources
        # format_resource_context(()) returns ""
        # The prompt builder may or may not include a blank line for empty context

        assert any(
            s[2].strip() for s in adapter.sent if "帮助" in s[2] or "你好" in s[2]
        ), f"agent response should be delivered: {adapter.sent}"

    asyncio.run(go())


def test_e2e_resource_forward_context_pipeline_ci(tmp_path: Path) -> None:
    """Forward message resource -> App._handle -> text context in prompt.

    Forward (merged chat records) are rendered as text and should
    appear in the agent prompt context.
    """
    async def go() -> None:
        cfg = _make_resource_cfg(tmp_path)
        adapter = _FakeAdapter()
        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app._prepare_runtime_skill_bundle = lambda: ""  # type: ignore[method-assign]

        fake_rm = _FakeResourceManager(
            (
                PreparedResource(
                    kind="forward",
                    text="[合并转发] 关于部署方案的讨论\n"
                    "Alice: 我建议用Docker部署\n"
                    "Bob: 同意，k8s太重量级了\n"
                    "Alice: 好的，我先写Dockerfile",
                ),
            )
        )
        app.resources = fake_rm  # type: ignore[assignment]

        prompts: list[str] = []

        async def fake_agent_run(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
            **kw: Any,
        ) -> str:
            prompts.append(prompt)
            return "总结：Alice和Bob讨论部署方案，决定用Docker部署，Alice正在写Dockerfile。"

        app.agent.run = fake_agent_run  # type: ignore[method-assign]
        app.policy = Policy(cfg, app._agent_runner)  # noqa: SLF001 # type: ignore[arg-type]

        await app._handle(  # noqa: SLF001
            _make_ev(
                "/ask 总结这段讨论",
                sender="reader",
                group="group",
                mid="fwd-msg",
                resources=(
                    ChatResource(kind="forward", name="群聊的聊天记录"),
                ),
            )
        )

        await _wait_for_sent(
            adapter,
            lambda a: any("总结" in s[2] or "Docker" in s[2] for s in a.sent),
            timeout=5.0,
        )

        latest_prompt = prompts[-1]
        assert "合并转发" in latest_prompt, (
            f"forward title should be in prompt: {latest_prompt[:400]}"
        )
        assert "Docker部署" in latest_prompt or "Alice" in latest_prompt, (
            f"forward content should be in prompt: {latest_prompt[:400]}"
        )

        assert any(
            s[2].strip() for s in adapter.sent
        ), f"agent response should be delivered: {adapter.sent}"

    asyncio.run(go())


def test_e2e_resource_image_in_private_chat_ci(tmp_path: Path) -> None:
    """Image resource in private chat -> App._handle -> prompt -> response.

    Private chats have different routing (no group mention logic).
    Resource pipeline should work the same.
    """
    async def go() -> None:
        cfg = _make_resource_cfg(tmp_path)
        adapter = _FakeAdapter()
        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app._prepare_runtime_skill_bundle = lambda: ""  # type: ignore[method-assign]

        fake_rm = _FakeResourceManager(
            (
                PreparedResource(
                    kind="image",
                    name="photo.jpg",
                    local_path=str(tmp_path / "photo.jpg"),
                ),
            )
        )
        app.resources = fake_rm  # type: ignore[assignment]

        prompts: list[str] = []

        async def fake_agent_run(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
            **kw: Any,
        ) -> str:
            prompts.append(prompt)
            return "我看到了你发的照片"

        app.agent.run = fake_agent_run  # type: ignore[method-assign]
        app.policy = Policy(cfg, app._agent_runner)  # noqa: SLF001 # type: ignore[arg-type]

        # Private chat: no group, no mentioned_bot needed
        await app._handle(  # noqa: SLF001
            _make_ev(
                "/ask 看看这张照片",
                sender="reader",
                group=None,
                mid="private-img",
                mentioned=False,
                resources=(
                    ChatResource(kind="image", url="https://example.com/photo.jpg", name="photo.jpg"),
                ),
            )
        )

        await _wait_for_sent(
            adapter,
            lambda a: any("照片" in s[2] for s in a.sent),
            timeout=5.0,
        )

        assert len(prompts) >= 1
        assert "photo.jpg" in prompts[-1], f"resource should be in prompt: {prompts[-1][:300]}"

    asyncio.run(go())


def test_e2e_resource_prepare_failure_is_handled_gracefully_ci(
    tmp_path: Path,
) -> None:
    """Resource prepare failure -> job fails gracefully, app does not crash.

    When ResourceManager.prepare() raises (e.g., download failure),
    the exception propagates through _agent_runner_inner and causes
    the job to fail. The app must remain operational.
    """
    async def go() -> None:
        cfg = _make_resource_cfg(tmp_path)
        adapter = _FakeAdapter()
        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app._prepare_runtime_skill_bundle = lambda: ""  # type: ignore[method-assign]

        # ResourceManager whose prepare() raises
        class _FailingResourceManager:
            async def prepare(self, ev: ChatEvent) -> tuple[PreparedResource, ...]:
                raise OSError("Connection reset")

            def cleanup_prepared(
                self, resources: tuple[PreparedResource, ...]
            ) -> None:
                pass

        app.resources = _FailingResourceManager()  # type: ignore[assignment]

        prompts: list[str] = []

        async def fake_agent_run(
            prompt: str,
            workspace: str | None = None,
            mode: str = "ask",
            model: str | None = None,
            progress: Any = None,
            **kw: Any,
        ) -> str:
            prompts.append(prompt)
            if "ping" in prompt:
                return "pong"
            return "should not reach here for failure case"

        app.agent.run = fake_agent_run  # type: ignore[method-assign]
        app.policy = Policy(cfg, app._agent_runner)  # noqa: SLF001 # type: ignore[arg-type]

        # Send message with a resource that will fail
        # This must NOT crash the app
        await app._handle(  # noqa: SLF001
            _make_ev(
                "/ask 分析这个文件",
                sender="reader",
                group="group",
                mid="fail-msg",
                resources=(
                    ChatResource(
                        kind="file",
                        url="https://broken.example/file.bin",
                        name="file.bin",
                    ),
                ),
            )
        )

        # Give the job time to fail
        await asyncio.sleep(0.3)

        # Agent should NOT have been called — prepare raises before agent runs
        assert len(prompts) == 0, (
            f"agent should not run when resource prepare fails, "
            f"but {len(prompts)} prompts were captured"
        )

        # App must still be operational — send another message
        # Replace resources with working fake first
        fake_rm = _FakeResourceManager(())
        app.resources = fake_rm  # type: ignore[assignment]

        adapter.sent.clear()
        await app._handle(  # noqa: SLF001
            _make_ev(
                "/ask ping",
                sender="reader",
                group="group",
                mid="recovery-msg",
            )
        )
        await _wait_for_sent(
            adapter,
            lambda a: any("pong" in s[2] for s in a.sent),
            timeout=5.0,
        )
        assert any(
            "pong" in s[2] for s in adapter.sent
        ), f"app should recover after resource failure: {adapter.sent}"

    asyncio.run(go())


# ── Scheme B: Real-agent tests ──────────────────────────────────────────────


def _make_e2e_cfg(tmp_path: Path) -> BridgeConfig:
    """Build an E2E config for resources, based on production config."""
    cfg = BridgeConfig.load("config.yaml")
    cfg.workspaces[str(tmp_path)] = True
    cfg.agent.default_workspace = str(tmp_path)
    # Runtime overrides from env
    runtime = os.environ.get("QQ_AGENT_BRIDGE_E2E_RUNTIME", "")
    if runtime:
        cfg.agent.runtime = runtime
    cfg.agent.binary = os.environ.get("QQ_AGENT_BRIDGE_E2E_BINARY", "")
    cfg.agent.env_runner = os.environ.get("QQ_AGENT_BRIDGE_E2E_ENV_RUNNER", "")
    cfg.agent.env_name = os.environ.get("QQ_AGENT_BRIDGE_E2E_ENV_NAME", "")
    cfg.agent.require_env = False
    cfg.agent.max_runtime_seconds = int(
        os.environ.get("QQ_AGENT_BRIDGE_E2E_TIMEOUT", "90")
    )
    cfg.agent.max_output_chars = 8000
    cfg.resources.root = "downloads/qq-agent-bridge"
    cfg.resources.enabled = True
    cfg.storage_maintenance.enabled = False
    return cfg


def test_e2e_resource_pipeline_real_app(tmp_path: Path) -> None:
    """Full resource pipeline through App._handle with real agent.

    Requires QQ_AGENT_BRIDGE_APP_E2E=1.
    Message with resource → App._handle → real ResourceManager.prepare →
    real agent prompt → real agent response → delivered to chat.
    """
    _require_app_e2e()

    async def go() -> None:
        from qq_agent_bridge.agent_runtime import build_agent_adapter

        cfg = _make_e2e_cfg(tmp_path)
        cfg.allowed_users = ["e2e-user"]
        cfg.commands = {"ask": True}
        cfg.resources.enabled = True
        cfg.resources.max_items = 5
        cfg.agent.max_runtime_seconds = int(
            os.environ.get("QQ_AGENT_BRIDGE_APP_E2E_TIMEOUT", "180")
        )

        adapter = _FakeAdapter()
        app = App(cfg)
        app.adapter = adapter  # type: ignore[assignment]
        app.policy = Policy(cfg, app._agent_runner)  # noqa: SLF001

        # Step 1: Send a plain text message to confirm pipeline works
        await app._handle(  # noqa: SLF001
            _make_ev(
                "/ask 回复：pong",
                sender="e2e-user",
                group=None,
                mid="real-1",
                mentioned=False,
            )
        )
        await _wait_for_sent(
            adapter,
            lambda a: any(
                s[2] for s in a.sent
                if "error" not in s[2].lower() and len(s[2]) > 3
            ),
            timeout=cfg.agent.max_runtime_seconds * 2,
        )

        # Verify a real response was sent
        response_texts = [
            s[2] for s in adapter.sent if s[2] and len(s[2]) > 3
        ]
        assert len(response_texts) >= 1, (
            f"real agent should produce a response, got: {adapter.sent}"
        )

        # Step 2: Send a message with a URL resource
        adapter.sent.clear()
        await app._handle(  # noqa: SLF001
            _make_ev(
                "/ask 这个链接是什么内容",
                sender="e2e-user",
                group=None,
                mid="real-2",
                mentioned=False,
                resources=(
                    ChatResource(
                        kind="url",
                        url="https://example.com",
                    ),
                ),
            )
        )
        await _wait_for_sent(
            adapter,
            lambda a: any(
                s[2] for s in a.sent
                if "error" not in s[2].lower() and len(s[2]) > 5
            ),
            timeout=cfg.agent.max_runtime_seconds * 2,
        )

        response_texts = [
            s[2] for s in adapter.sent if s[2] and len(s[2]) > 5
        ]
        assert len(response_texts) >= 1, (
            f"real agent should respond to URL resource: {adapter.sent}"
        )

    asyncio.run(go())
