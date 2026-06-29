"""Agent runtime adapter factory."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

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
    ) -> str:
        return "[error] agent runtime 未配置，请在 config.yaml 里设置 agent.runtime"


def build_agent_adapter(cfg: BridgeConfig) -> CursorAdapter | CustomCommandAdapter | DisabledAgentAdapter:
    runtime = (cfg.agent.runtime or "").strip().lower()
    if not runtime:
        return DisabledAgentAdapter(cfg)
    if runtime == "cursor-cli":
        return CursorAdapter(cfg)
    if runtime == "custom-cli":
        return CustomCommandAdapter(cfg)
    raise ValueError(f"unsupported agent runtime: {cfg.agent.runtime}")
