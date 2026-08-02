from __future__ import annotations

from pathlib import Path

import pytest

from qq_agent_bridge.config import MemoryRetrievalConfig
from qq_agent_bridge.config import BridgeConfig
from qq_agent_bridge.agent_memory import AgentMemoryManager
from qq_agent_bridge.long_term_memory import (
    AgentMemoryCommitError,
    AgentMemoryProposal,
    LongTermMemoryRetriever,
    LongTermMemoryStore,
    MemoryProposal,
    MemoryScope,
    MemorySource,
    authorized_memory_subjects,
)


def _store(tmp_path: Path) -> LongTermMemoryStore:
    store = LongTermMemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    return store


def _add_memory(
    store: LongTermMemoryStore,
    scope: MemoryScope,
    *,
    message_id: str,
    subject_kind: str,
    subject_id: str,
    content: str,
    status: str = "active",
    source_kind: str = "self_statement",
    expires_at: int | None = None,
) -> str:
    store.set_scope_enabled(scope, True)
    source_id = store.collect(
        MemorySource(
            scope=scope,
            message_id=message_id,
            sender_id=subject_id,
            text=content,
            message_timestamp=1,
            created_at=1,
        )
    )
    assert source_id is not None
    proposal = MemoryProposal.add(
        subject_kind=subject_kind,
        subject_id=subject_id,
        category="project",
        content=content,
        confidence=0.8,
        status=status,
        source_kind=source_kind,
        expires_at=expires_at,
        source_ids=(source_id,),
    )
    return store.commit_review(scope, (source_id,), (proposal,))[0].id


def test_authorized_memory_subjects_are_exact_and_deduplicated() -> None:
    assert authorized_memory_subjects(
        MemoryScope("group", "group-a"),
        "user-a",
        ("user-b", "user-b"),
        "user-c",
    ) == (
        ("group", "group-a"),
        ("user", "user-a"),
        ("user", "user-b"),
        ("user", "user-c"),
    )

    assert authorized_memory_subjects(
        MemoryScope("private", "user-a"),
        "user-a",
        ("user-b",),
        "user-c",
    ) == (("user", "user-a"),)


def test_private_memory_subjects_reject_a_different_sender() -> None:
    with pytest.raises(ValueError, match="private memory scope"):
        authorized_memory_subjects(
            MemoryScope("private", "user-a"),
            "user-b",
            (),
            None,
        )


def test_agent_memory_export_enforces_scope_subject_status_and_expiry(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    group_a = MemoryScope("group", "group-a")
    group_b = MemoryScope("group", "group-b")
    private_a = MemoryScope("private", "user-a")
    try:
        expected = {
            _add_memory(
                store,
                group_a,
                message_id="group",
                subject_kind="group",
                subject_id="group-a",
                content="GROUP VISIBLE",
            ),
            _add_memory(
                store,
                group_a,
                message_id="sender",
                subject_kind="user",
                subject_id="user-a",
                content="SENDER VISIBLE",
            ),
            _add_memory(
                store,
                group_a,
                message_id="mentioned",
                subject_kind="user",
                subject_id="user-b",
                content="MENTIONED VISIBLE",
            ),
            _add_memory(
                store,
                group_a,
                message_id="work",
                subject_kind="group",
                subject_id="group-a",
                content="AGENT WORK VISIBLE",
                source_kind="agent_work",
            ),
        }
        _add_memory(
            store,
            group_a,
            message_id="hidden",
            subject_kind="user",
            subject_id="user-c",
            content="HIDDEN USER",
        )
        _add_memory(
            store,
            group_a,
            message_id="candidate",
            subject_kind="group",
            subject_id="group-a",
            content="HIDDEN CANDIDATE",
            status="candidate",
        )
        _add_memory(
            store,
            group_a,
            message_id="expired",
            subject_kind="group",
            subject_id="group-a",
            content="HIDDEN EXPIRED",
            expires_at=1,
        )
        _add_memory(
            store,
            group_b,
            message_id="other-group",
            subject_kind="group",
            subject_id="group-b",
            content="HIDDEN OTHER GROUP",
        )
        _add_memory(
            store,
            private_a,
            message_id="private",
            subject_kind="user",
            subject_id="user-a",
            content="HIDDEN PRIVATE",
        )

        items = store.export_agent_memories(
            group_a,
            authorized_subjects=authorized_memory_subjects(
                group_a, "user-a", ("user-b",), None
            ),
            schedule_id=None,
            limit=200,
            max_chars=50_000,
        )

        assert {item.id for item in items} == expected
    finally:
        store.close()


def test_ordinary_retrieval_excludes_agent_work_notes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = MemoryScope("group", "group-a")
    try:
        _add_memory(
            store,
            scope,
            message_id="work",
            subject_kind="group",
            subject_id="group-a",
            content="AGENTWORKNONCE",
            source_kind="agent_work",
        )
        retriever = LongTermMemoryRetriever(
            store,
            MemoryRetrievalConfig(max_items=12, max_chars=1_500, minimum_score=0.0),
        )

        assert retriever.retrieve(scope, "user-a", (), None, "AGENTWORKNONCE") == ""
    finally:
        store.close()


def test_agent_memory_commit_add_is_atomic_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = MemoryScope("group", "group-a")
    store.set_scope_enabled(scope, True)
    proposal = AgentMemoryProposal("add", "已完成第一阶段")
    try:
        result = store.commit_agent_memories(
            scope,
            job_id="job-1",
            schedule_id="schedule-1",
            subject=("group", "group-a"),
            proposals=(proposal,),
        )

        assert result.replayed is False
        assert len(result.items) == 1
        item = result.items[0]
        assert item.category == "project"
        assert item.source_kind == "agent_work"
        assert item.subject_kind == "group"
        assert item.subject_id == "group-a"
        assert item.content == "已完成第一阶段"
        assert item.status == "active"

        replayed = store.commit_agent_memories(
            scope,
            job_id="job-1",
            schedule_id="schedule-1",
            subject=("group", "group-a"),
            proposals=(proposal,),
        )

        assert replayed.replayed is True
        assert [value.id for value in replayed.items] == [item.id]
        assert store._connection is not None
        assert store._connection.execute(
            "SELECT COUNT(*) FROM agent_memory_commits WHERE job_id = 'job-1'"
        ).fetchone()[0] == 1
        assert store._connection.execute(
            "SELECT COUNT(*) FROM agent_memory_commit_items WHERE job_id = 'job-1'"
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_agent_memory_commit_revises_only_exact_work_note_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = MemoryScope("group", "group-a")
    store.set_scope_enabled(scope, True)
    try:
        added = store.commit_agent_memories(
            scope,
            job_id="job-add",
            schedule_id=None,
            subject=("group", "group-a"),
            proposals=(AgentMemoryProposal("add", "旧进度"),),
        ).items[0]

        revised = store.commit_agent_memories(
            scope,
            job_id="job-revise",
            schedule_id=None,
            subject=("group", "group-a"),
            proposals=(
                AgentMemoryProposal(
                    "revise",
                    "新进度",
                    item_id=added.id,
                    expected_version=added.version,
                ),
            ),
        ).items[0]

        assert revised.id == added.id
        assert revised.content == "新进度"
        assert revised.version == added.version + 1

        with pytest.raises(AgentMemoryCommitError, match="stale"):
            store.commit_agent_memories(
                scope,
                job_id="job-stale",
                schedule_id=None,
                subject=("group", "group-a"),
                proposals=(
                    AgentMemoryProposal(
                        "revise",
                        "不能覆盖",
                        item_id=added.id,
                        expected_version=added.version,
                    ),
                ),
            )

        unchanged = store.get_item(scope, added.id)
        assert unchanged is not None
        assert unchanged.content == "新进度"
        assert store._connection is not None
        assert store._connection.execute(
            "SELECT COUNT(*) FROM agent_memory_commits WHERE job_id = 'job-stale'"
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_agent_memory_commit_rejects_normal_memory_and_changed_replay(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    scope = MemoryScope("group", "group-a")
    normal_id = _add_memory(
        store,
        scope,
        message_id="normal",
        subject_kind="group",
        subject_id="group-a",
        content="普通群记忆",
    )
    try:
        with pytest.raises(AgentMemoryCommitError, match="agent work"):
            store.commit_agent_memories(
                scope,
                job_id="job-normal-target",
                schedule_id=None,
                subject=("group", "group-a"),
                proposals=(
                    AgentMemoryProposal(
                        "revise",
                        "恶意修改",
                        item_id=normal_id,
                        expected_version=1,
                    ),
                ),
            )

        proposal = AgentMemoryProposal("add", "固定内容")
        store.commit_agent_memories(
            scope,
            job_id="job-replay",
            schedule_id=None,
            subject=("group", "group-a"),
            proposals=(proposal,),
        )
        with pytest.raises(AgentMemoryCommitError, match="replay mismatch"):
            store.commit_agent_memories(
                scope,
                job_id="job-replay",
                schedule_id=None,
                subject=("group", "group-a"),
                proposals=(AgentMemoryProposal("add", "被替换的内容"),),
            )

        assert store.get_item(scope, normal_id).content == "普通群记忆"  # type: ignore[union-attr]
    finally:
        store.close()


def test_agent_memory_commit_has_no_forget_operation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = MemoryScope("group", "group-a")
    store.set_scope_enabled(scope, True)
    item = store.commit_agent_memories(
        scope,
        job_id="job-add-before-forget",
        schedule_id=None,
        subject=("group", "group-a"),
        proposals=(AgentMemoryProposal("add", "必须由用户管理的工作记录"),),
    ).items[0]
    try:
        with pytest.raises(AgentMemoryCommitError, match="unsupported"):
            store.commit_agent_memories(
                scope,
                job_id="job-forget",
                schedule_id=None,
                subject=("group", "group-a"),
                proposals=(
                    AgentMemoryProposal(  # type: ignore[arg-type]
                        "forget", "", item_id=item.id, expected_version=item.version
                    ),
                ),
            )

        assert store.get_item(scope, item.id) is not None
    finally:
        store.close()


def _manager(
    tmp_path: Path,
    store: LongTermMemoryStore,
) -> tuple[AgentMemoryManager, BridgeConfig, Path]:
    cfg = BridgeConfig()
    cfg.long_term_memory.enabled = True
    cfg.long_term_memory.agent_access.enabled = True
    cfg.long_term_memory.agent_access.commands = ("task",)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return (
        AgentMemoryManager(store, cfg, workspace, "downloads/qq-agent-bridge"),
        cfg,
        workspace,
    )


def test_agent_memory_session_is_scoped_immutable_and_path_safe(tmp_path: Path) -> None:
    store = LongTermMemoryStore(tmp_path / "state" / "memory.sqlite3")
    store.initialize()
    scope = MemoryScope("group", "group-a")
    _add_memory(
        store,
        scope,
        message_id="visible",
        subject_kind="group",
        subject_id="group-a",
        content="可见的历史进度",
    )
    manager, cfg, workspace = _manager(tmp_path, store)
    try:
        session = manager.prepare(
            job_id="job-1",
            command="task",
            scope=scope,
            current_sender="user-a",
            real_mentions=(),
            quoted_sender=None,
            schedule_id=None,
        )

        assert session is not None
        assert session.subject == ("group", "group-a")
        assert session.snapshot_path.stat().st_mode & 0o777 == 0o400
        assert session.manifest_path.stat().st_mode & 0o777 == 0o400
        assert session.proposal_dir.stat().st_mode & 0o777 == 0o700
        manifest = session.manifest_path.read_text(encoding="utf-8")
        assert str(store.path) not in manifest
        assert str(workspace) not in manifest

        cfg.long_term_memory.agent_access.enabled = False
        assert manager.prepare(
            job_id="disabled",
            command="task",
            scope=scope,
            current_sender="user-a",
            real_mentions=(),
            quoted_sender=None,
            schedule_id=None,
        ) is None
        cfg.long_term_memory.agent_access.enabled = True
        assert manager.prepare(
            job_id="ask-job",
            command="ask",
            scope=scope,
            current_sender="user-a",
            real_mentions=(),
            quoted_sender=None,
            schedule_id=None,
        ) is None
        cfg.long_term_memory.agent_access.scheduled_task_enabled = False
        assert manager.prepare(
            job_id="scheduled-disabled",
            command="task",
            scope=scope,
            current_sender="user-a",
            real_mentions=(),
            quoted_sender=None,
            schedule_id="schedule-a",
        ) is None
        with pytest.raises(ValueError, match="unsafe job id"):
            manager.prepare(
                job_id="../escape",
                command="task",
                scope=scope,
                current_sender="user-a",
                real_mentions=(),
                quoted_sender=None,
                schedule_id=None,
            )
        manager.cleanup(session)
        manager.cleanup(session)
        assert not session.root.exists()
    finally:
        store.close()


def test_agent_memory_session_refuses_production_database_inside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = LongTermMemoryStore(workspace / "data" / "memory.sqlite3")
    store.initialize()
    store.set_scope_enabled(MemoryScope("private", "user-a"), True)
    cfg = BridgeConfig()
    manager = AgentMemoryManager(store, cfg, workspace, "downloads/qq-agent-bridge")
    try:
        with pytest.raises(ValueError, match="production memory database"):
            manager.prepare(
                job_id="job-unsafe-db",
                command="task",
                scope=MemoryScope("private", "user-a"),
                current_sender="user-a",
                real_mentions=(),
                quoted_sender=None,
                schedule_id=None,
            )
    finally:
        store.close()


def test_agent_memory_inspection_accepts_strict_proposals_and_rejects_links(
    tmp_path: Path,
) -> None:
    store = LongTermMemoryStore(tmp_path / "state" / "memory.sqlite3")
    store.initialize()
    scope = MemoryScope("group", "group-a")
    store.set_scope_enabled(scope, True)
    manager, _cfg, _workspace = _manager(tmp_path, store)
    session = manager.prepare(
        job_id="job-inspect",
        command="task",
        scope=scope,
        current_sender="user-a",
        real_mentions=(),
        quoted_sender=None,
        schedule_id=None,
    )
    assert session is not None
    try:
        proposal = session.proposal_dir / "0001.json"
        proposal.write_text(
            __import__("json").dumps(
                {
                    "token": session.token,
                    "job_id": session.job_id,
                    "operation": "add",
                    "content": "下次继续核对来源",
                    "item_id": None,
                    "expected_version": None,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        proposal.chmod(0o600)
        outside = tmp_path / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        (session.proposal_dir / "0002.json").symlink_to(outside)

        inspected = manager.inspect(session)

        assert inspected.proposals == (
            AgentMemoryProposal("add", "下次继续核对来源"),
        )
        assert "symlink" in inspected.rejection_reasons
    finally:
        manager.cleanup(session)
        store.close()


@pytest.mark.parametrize("replaced", ["snapshot", "manifest"])
def test_agent_memory_inspection_fails_closed_when_read_only_input_is_replaced(
    tmp_path: Path,
    replaced: str,
) -> None:
    store = LongTermMemoryStore(tmp_path / "state" / "memory.sqlite3")
    store.initialize()
    scope = MemoryScope("group", "group-a")
    store.set_scope_enabled(scope, True)
    manager, _cfg, _workspace = _manager(tmp_path, store)
    session = manager.prepare(
        job_id=f"job-replaced-{replaced}",
        command="task",
        scope=scope,
        current_sender="user-a",
        real_mentions=(),
        quoted_sender=None,
        schedule_id=None,
    )
    assert session is not None
    try:
        proposal = session.proposal_dir / "valid.json"
        proposal.write_text(
            __import__("json").dumps(
                {
                    "token": session.token,
                    "job_id": session.job_id,
                    "operation": "add",
                    "content": "不应被提交",
                    "item_id": None,
                    "expected_version": None,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        proposal.chmod(0o600)
        target = (
            session.snapshot_path if replaced == "snapshot" else session.manifest_path
        )
        target.unlink()
        target.write_text("replaced", encoding="utf-8")
        target.chmod(0o400)

        inspected = manager.inspect(session)

        assert inspected.proposals == ()
        assert f"{replaced}-replaced" in inspected.rejection_reasons
    finally:
        manager.cleanup(session)
        store.close()


def test_agent_memory_export_revives_dormant_work_only_for_same_schedule(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    scope = MemoryScope("group", "group-a")
    store.set_scope_enabled(scope, True)
    item = store.commit_agent_memories(
        scope,
        job_id="scheduled-job",
        schedule_id="schedule-a",
        subject=("group", "group-a"),
        proposals=(AgentMemoryProposal("add", "同一定时任务的旧进度"),),
    ).items[0]
    assert store._connection is not None
    store._connection.execute(
        "UPDATE memory_items SET status = 'dormant' WHERE id = ?", (item.id,)
    )
    subjects = (("group", "group-a"),)
    try:
        same = store.export_agent_memories(
            scope,
            authorized_subjects=subjects,
            schedule_id="schedule-a",
            limit=20,
            max_chars=10_000,
        )
        other = store.export_agent_memories(
            scope,
            authorized_subjects=subjects,
            schedule_id="schedule-b",
            limit=20,
            max_chars=10_000,
        )
        unscheduled = store.export_agent_memories(
            scope,
            authorized_subjects=subjects,
            schedule_id=None,
            limit=20,
            max_chars=10_000,
        )

        assert [value.id for value in same] == [item.id]
        assert other == ()
        assert unscheduled == ()
    finally:
        store.close()
