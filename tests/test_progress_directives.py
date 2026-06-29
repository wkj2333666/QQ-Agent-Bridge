"""Progress directive parsing tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.progress_directives import ProgressLineBuffer, strip_progress_directives  # type: ignore


def test_strip_progress_directives_removes_lines_and_returns_payloads() -> None:
    clean, progress = strip_progress_directives(
        "hello\nQQBOT_PROGRESS: 解析链接中\nworld\nQQBOT_PROGRESS: 抽帧完成\n"
    )

    assert clean == "hello\nworld"
    assert progress == ("解析链接中", "抽帧完成")


def test_strip_progress_directives_ignores_empty_payloads() -> None:
    clean, progress = strip_progress_directives("a\nQQBOT_PROGRESS:   \nb")

    assert clean == "a\nb"
    assert progress == ()


def test_progress_line_buffer_handles_split_chunks() -> None:
    buffer = ProgressLineBuffer()

    assert buffer.feed("hello\nQQBOT_PRO") == (("hello",), ())
    lines, progress = buffer.feed("GRESS: 处理中\nfinal")
    assert lines == ()
    assert progress == ("处理中",)
    assert buffer.finish() == (("final",), ())
