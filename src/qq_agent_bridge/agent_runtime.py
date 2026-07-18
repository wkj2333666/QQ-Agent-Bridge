"""Agent runtime adapter factory."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import inspect
from typing import Any

from .config import BridgeConfig
from .cursor_adapter import CursorAdapter, CustomCommandAdapter

ProgressCallback = Callable[[str], Awaitable[None]]


class DisabledAgentAdapter:
    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg

    async def run(
        self,
        prompt: str,
        workspace: str | None = None,
        mode: str = "ask",
        model: str | None = None,
        progress: ProgressCallback | None = None,
        trace_id: str | None = None,
        redact_extra: tuple[str, ...] | None = None,
    ) -> str:
        return "[error] agent runtime 未配置，请在 config.yaml 里设置 agent.runtime"


def _supports_keyword(method: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


async def run_agent(
    agent: Any,
    prompt: str,
    workspace: str,
    mode: str,
    *,
    model: str | None = None,
    progress: ProgressCallback | None = None,
    trace_id: str | None = None,
    redact_extra: tuple[str, ...] | None = None,
) -> str:
    """Call an Agent while keeping older test/custom adapters compatible."""
    kwargs: dict[str, Any] = {"model": model}
    if progress is not None and _supports_keyword(agent.run, "progress"):
        kwargs["progress"] = progress
    if trace_id is not None and _supports_keyword(agent.run, "trace_id"):
        kwargs["trace_id"] = trace_id
    if redact_extra is not None and _supports_keyword(agent.run, "redact_extra"):
        kwargs["redact_extra"] = redact_extra
    return await agent.run(prompt, workspace, mode, **kwargs)


def build_agent_adapter(cfg: BridgeConfig) -> CursorAdapter | CustomCommandAdapter | DisabledAgentAdapter:
    runtime = (cfg.agent.runtime or "").strip().lower()
    if not runtime:
        return DisabledAgentAdapter(cfg)
    if runtime == "cursor-cli":
        return CursorAdapter(cfg)
    if runtime == "custom-cli":
        return CustomCommandAdapter(cfg)
    raise ValueError(f"unsupported agent runtime: {cfg.agent.runtime}")
