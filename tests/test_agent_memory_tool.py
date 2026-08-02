from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from qq_agent_bridge.agent_memory import AgentMemoryManager
from qq_agent_bridge.config import BridgeConfig
from qq_agent_bridge.long_term_memory import LongTermMemoryStore
from qq_agent_bridge.long_term_memory_models import (
    AgentMemoryProposal,
    MemoryScope,
)


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "qq-agent-runtime"
    / "scripts"
    / "memory_tool.py"
)


def _session(tmp_path: Path):
    store = LongTermMemoryStore(tmp_path / "state" / "memory.sqlite3")
    store.initialize()
    scope = MemoryScope("group", "g1")
    store.set_scope_enabled(scope, True)
    original = store.commit_agent_memories(
        scope,
        job_id="seed",
        schedule_id=None,
        subject=("group", "g1"),
        proposals=(AgentMemoryProposal("add", "第一阶段已经完成"),),
    ).items[0]
    cfg = BridgeConfig()
    cfg.long_term_memory.agent_access.max_search_results = 1
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = AgentMemoryManager(
        store, cfg, workspace, "downloads/qq-agent-bridge"
    )
    session = manager.prepare(
        job_id="helper-job",
        command="task",
        scope=scope,
        current_sender="u1",
        real_mentions=(),
        quoted_sender=None,
        schedule_id=None,
    )
    assert session is not None
    return store, manager, session, original


def _run(manifest: Path, *args: str) -> tuple[int, dict[str, object], str]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest", str(manifest), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode, json.loads(completed.stdout), completed.stderr


def test_memory_tool_search_recent_and_read_are_bounded(tmp_path: Path) -> None:
    store, manager, session, original = _session(tmp_path)
    try:
        code, searched, err = _run(
            session.manifest_path, "search", "--query", "第一阶段"
        )
        assert code == 0 and err == ""
        assert searched["ok"] is True
        assert searched["count"] == 1
        assert searched["results"][0]["id"] == original.id  # type: ignore[index]

        code, recent, err = _run(session.manifest_path, "recent")
        assert code == 0 and err == ""
        assert recent["count"] == 1

        code, read, err = _run(session.manifest_path, "read", "--id", original.id)
        assert code == 0 and err == ""
        assert read["result"]["content"] == "第一阶段已经完成"  # type: ignore[index]

        code, missing, err = _run(session.manifest_path, "read", "--id", "missing")
        assert code != 0 and err == ""
        assert missing == {"ok": False, "error": "not-found"}

        activity = session.proposal_dir / ".activity.jsonl"
        records = [
            json.loads(line)
            for line in activity.read_text(encoding="utf-8").splitlines()
        ]
        assert records == [
            {
                "token": session.token,
                "job_id": session.job_id,
                "operation": "search",
                "result_count": 1,
            }
        ]
        assert "第一阶段" not in activity.read_text(encoding="utf-8")
        assert activity.stat().st_mode & 0o777 == 0o600
    finally:
        manager.cleanup(session)
        store.close()


def test_memory_tool_creates_exclusive_private_proposals(tmp_path: Path) -> None:
    store, manager, session, original = _session(tmp_path)
    try:
        code, added, err = _run(
            session.manifest_path,
            "propose-add",
            "--text",
            "下一步检查来源",
        )
        assert code == 0 and err == ""
        assert added == {"ok": True, "count": 1}

        code, revised, err = _run(
            session.manifest_path,
            "propose-revise",
            "--id",
            original.id,
            "--version",
            str(original.version),
            "--text",
            "第一阶段已完成并复核",
        )
        assert code == 0 and err == ""
        assert revised == {"ok": True, "count": 1}

        files = sorted(
            path
            for path in session.proposal_dir.iterdir()
            if path.name != ".activity.jsonl"
        )
        assert len(files) == 2
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in files]
        assert all(set(value) == {
            "token", "job_id", "operation", "content", "item_id", "expected_version"
        } for value in payloads)
        assert all("下一步检查来源" not in json.dumps(value) or value["operation"] == "add" for value in payloads)

        code, unknown, err = _run(
            session.manifest_path,
            "propose-revise",
            "--id",
            "unknown",
            "--version",
            "1",
            "--text",
            "不能写",
        )
        assert code != 0 and err == ""
        assert unknown == {"ok": False, "error": "not-found"}
        assert len(
            tuple(
                path
                for path in session.proposal_dir.iterdir()
                if path.name != ".activity.jsonl"
            )
        ) == 2
    finally:
        manager.cleanup(session)
        store.close()
