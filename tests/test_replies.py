"""User-visible reply formatting tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.main import App  # type: ignore


def test_single_reply_has_no_job_prefix() -> None:
    app = App(BridgeConfig())
    replies = app._reply_chunks("j1782616646632-61bcb2", "我是QQ小助手")
    assert replies == ["我是QQ小助手"]


def test_multi_reply_has_part_prefix_but_no_job_id() -> None:
    app = App(BridgeConfig())
    replies = app._reply_chunks("j1782616646632-61bcb2", "abcdef", size=3)
    assert replies == ["（1/2）abc", "（2/2）def"]
    assert all("j178" not in reply for reply in replies)
