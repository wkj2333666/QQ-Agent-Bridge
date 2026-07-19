from __future__ import annotations

from contextlib import contextmanager
import importlib
from pathlib import Path
import sqlite3
from typing import Iterator

import pytest

from qq_agent_bridge.long_term_memory import (
    LongTermMemoryStore,
    MemoryProposal,
    MemoryScope,
    MemorySource,
)


GROUP_A = MemoryScope("group", "group-a")
GROUP_B = MemoryScope("group", "group-b")
PRIVATE_A = MemoryScope("private", "user-a")


def _source(
    scope: MemoryScope,
    message_id: str,
    sender_id: str,
    text: str,
    *,
    created_at: int = 1_000,
) -> MemorySource:
    return MemorySource(
        scope=scope,
        message_id=message_id,
        sender_id=sender_id,
        text=text,
        message_timestamp=created_at,
        created_at=created_at,
    )


def test_public_module_reexports_domain_models() -> None:
    public_module = importlib.import_module("qq_agent_bridge.long_term_memory")
    models_module = importlib.import_module("qq_agent_bridge.long_term_memory_models")

    for name in (
        "MemoryScope",
        "MemorySource",
        "MemoryItem",
        "MemoryProposal",
        "MemoryStoreStatus",
    ):
        assert getattr(public_module, name) is getattr(models_module, name)


def test_schema_and_migration_helpers_are_in_focused_internal_module() -> None:
    schema_module = importlib.import_module("qq_agent_bridge.long_term_memory_schema")

    assert schema_module.SCHEMA_VERSION == 2
    assert callable(schema_module.migrate)


def test_v1_migration_adds_candidate_target_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY,
                short_id TEXT NOT NULL UNIQUE,
                scope_kind TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                subject_kind TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                base_confidence REAL NOT NULL,
                effective_score REAL NOT NULL,
                status TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_count INTEGER NOT NULL DEFAULT 1,
                explicit_memory INTEGER NOT NULL DEFAULT 0,
                decay_exempt INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_supported_at INTEGER NOT NULL,
                expires_at INTEGER,
                dormant_at INTEGER,
                version INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO memory_items VALUES (
                'legacy-item', 'legacy-short', 'group', 'group-a', 'user',
                'user-a', 'preference', 'Legacy fact', 0.8, 0.8, 'active',
                'normal', 'self_statement', 1, 0, 0, 1, 1, 1, NULL, NULL, 1
            );
            PRAGMA user_version = 1;
            """
        )

    first = LongTermMemoryStore(path)
    first.initialize()
    migrated = first.get_item(GROUP_A, "legacy-item")
    assert migrated is not None
    assert migrated.candidate_target_id is None
    assert first._connection is not None
    columns = {
        row[1] for row in first._connection.execute("PRAGMA table_info(memory_items)")
    }
    assert "candidate_target_id" in columns
    assert first._connection.execute("PRAGMA user_version").fetchone()[0] == 2
    first.close()

    reopened = LongTermMemoryStore(path)
    reopened.initialize()
    assert reopened.get_item(GROUP_A, "legacy-item") == migrated
    reopened.close()


@pytest.fixture
def store(tmp_path: Path) -> LongTermMemoryStore:
    result = LongTermMemoryStore(tmp_path / "private" / "memory.sqlite3")
    result.initialize()
    try:
        yield result
    finally:
        result.close()


def test_initialize_creates_private_wal_database_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "private" / "memory.sqlite3"
    store = LongTermMemoryStore(path)
    store.initialize()

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700

    assert store._connection is not None
    assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        review_run_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(review_runs)")
        }

    assert {
        "memory_scopes",
        "review_buffer",
        "memory_items",
        "memory_revisions",
        "review_runs",
        "memory_fts",
    } <= tables
    assert "scope_hash" in review_run_columns
    assert "scope_id" not in review_run_columns
    store.close()


def test_store_requires_exact_scope_for_enablement_and_sources(
    store: LongTermMemoryStore,
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    store.set_scope_enabled(PRIVATE_A, True)

    assert store.is_scope_enabled(GROUP_A)
    assert not store.is_scope_enabled(GROUP_B)
    assert store.is_scope_enabled(PRIVATE_A)

    group_source_id = store.collect(_source(GROUP_A, "g-a", "user-a", "group text"))
    private_source_id = store.collect(
        _source(PRIVATE_A, "p-a", "user-a", "private text")
    )
    assert store.collect(_source(GROUP_B, "g-b", "user-a", "disabled")) is None

    assert [source.id for source in store.pending_sources(GROUP_A, 10)] == [
        group_source_id
    ]
    assert [source.id for source in store.pending_sources(PRIVATE_A, 10)] == [
        private_source_id
    ]
    assert store.pending_sources(GROUP_B, 10) == ()

    store.close()
    store.initialize()
    assert store.is_scope_enabled(GROUP_A)
    assert store.is_scope_enabled(PRIVATE_A)


def test_items_cannot_cross_private_group_or_group_boundaries(
    store: LongTermMemoryStore,
) -> None:
    for scope in (GROUP_A, GROUP_B, PRIVATE_A):
        store.set_scope_enabled(scope, True)
        source_id = store.collect(
            _source(scope, f"message-{scope.kind}-{scope.id}", "user-a", scope.id)
        )
        assert source_id is not None
        store.commit_review(
            scope,
            [source_id],
            [
                MemoryProposal.add(
                    subject_kind="user",
                    subject_id="user-a",
                    category="project",
                    content=f"memory-{scope.kind}-{scope.id}",
                    confidence=0.9,
                )
            ],
        )

    group_a_item = store.list_items(GROUP_A)[0]
    assert [item.content for item in store.list_items(GROUP_A)] == [
        "memory-group-group-a"
    ]
    assert [item.content for item in store.list_items(GROUP_B)] == [
        "memory-group-group-b"
    ]
    assert [item.content for item in store.list_items(PRIVATE_A)] == [
        "memory-private-user-a"
    ]
    assert store.get_item(GROUP_B, group_a_item.id) is None
    assert store.get_item(PRIVATE_A, group_a_item.short_id) is None
    assert [item.id for item in store.retrieve_candidates(GROUP_A)] == [group_a_item.id]
    assert [item.id for item in store.retrieve_candidates(GROUP_A, query="memory")] == [
        group_a_item.id
    ]


def test_scope_disable_prevents_late_review_commit(store: LongTermMemoryStore) -> None:
    store.set_scope_enabled(GROUP_A, True)
    source_id = store.collect(_source(GROUP_A, "one", "user-a", "source"))
    assert source_id is not None
    store.set_scope_enabled(GROUP_A, False)

    with pytest.raises(RuntimeError, match="disabled"):
        store.commit_review(
            GROUP_A,
            [source_id],
            [
                MemoryProposal.add(
                    subject_kind="user",
                    subject_id="user-a",
                    content="Must not commit",
                )
            ],
        )

    assert [source.id for source in store.pending_sources(GROUP_A, 10)] == [source_id]
    assert store.list_items(GROUP_A) == ()


def test_collect_rechecks_enablement_inside_write_transaction(
    store: LongTermMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    disabling_store = LongTermMemoryStore(store.path)
    disabling_store.initialize()
    original_transaction = store._transaction
    statements: list[str] = []
    assert store._connection is not None
    store._connection.set_trace_callback(statements.append)

    @contextmanager
    def disable_before_collect_transaction() -> Iterator[sqlite3.Connection]:
        disabling_store.set_scope_enabled(GROUP_A, False)
        with original_transaction() as conn:
            yield conn

    monkeypatch.setattr(store, "_transaction", disable_before_collect_transaction)
    try:
        source_id = store.collect(
            _source(GROUP_A, "interleaved", "user-a", "must not be stored")
        )
    finally:
        store._connection.set_trace_callback(None)
        disabling_store.close()

    assert source_id is None
    assert not store.is_scope_enabled(GROUP_A)
    assert store.pending_sources(GROUP_A, 10) == ()
    begin_index = statements.index("BEGIN IMMEDIATE")
    enabled_index = next(
        index
        for index, statement in enumerate(statements)
        if "SELECT enabled FROM memory_scopes" in statement
    )
    assert begin_index < enabled_index
    assert not any("INSERT INTO review_buffer" in statement for statement in statements)


def test_commit_review_is_atomic_and_consumes_only_selected_scoped_sources(
    store: LongTermMemoryStore,
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    first = store.collect(_source(GROUP_A, "first", "user-a", "first"))
    second = store.collect(_source(GROUP_A, "second", "user-a", "second"))
    assert first is not None and second is not None

    with pytest.raises(ValueError, match="operation"):
        store.commit_review(
            GROUP_A,
            [first],
            [MemoryProposal(operation="not-supported")],
        )

    assert [source.id for source in store.pending_sources(GROUP_A, 10)] == [first, second]

    store.commit_review(
        GROUP_A,
        [first],
        [
            MemoryProposal.add(
                subject_kind="group",
                subject_id=GROUP_A.id,
                category="group_norm",
                content="Keep decisions concise",
            )
        ],
    )

    assert [source.id for source in store.pending_sources(GROUP_A, 10)] == [second]
    assert store.status(GROUP_A).active_count == 1
    assert store.status(GROUP_A).pending_count == 1


def test_per_source_failure_deadlines_are_atomic(
    store: LongTermMemoryStore,
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    source_id = store.collect(_source(GROUP_A, "first", "user-a", "first"))
    assert source_id is not None
    run_count = store._conn.execute(  # noqa: SLF001 - verify transactional audit write
        "SELECT COUNT(*) AS count FROM review_runs"
    ).fetchone()["count"]

    with pytest.raises(ValueError, match="exact scope"):
        store.mark_review_failures(
            GROUP_A,
            ((source_id, 1_060), (source_id + 10_000, 1_600)),
            error_class="malformed_output",
            trigger_class="explicit",
            now=1_000,
        )

    retained = store.pending_sources(GROUP_A, limit=10, now=2_000)
    assert len(retained) == 1
    assert retained[0].id == source_id
    assert retained[0].attempt_count == 0
    assert retained[0].next_attempt_at == 0
    assert store._conn.execute(  # noqa: SLF001 - verify audit rollback
        "SELECT COUNT(*) AS count FROM review_runs"
    ).fetchone()["count"] == run_count


def test_direct_revision_into_duplicate_reinforces_survivor_and_retires_target(
    store: LongTermMemoryStore,
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    target_source = store.collect(
        _source(GROUP_A, "target", "user-a", "Likes tea")
    )
    survivor_source = store.collect(
        _source(GROUP_A, "survivor", "user-a", "Likes coffee")
    )
    assert target_source is not None and survivor_source is not None
    target = store.commit_review(
        GROUP_A,
        (target_source,),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="user-a",
                content="Likes tea",
                confidence=0.85,
            ),
        ),
    )[0]
    survivor = store.commit_review(
        GROUP_A,
        (survivor_source,),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="user-a",
                content="Likes coffee",
                confidence=0.6,
                status="candidate",
            ),
        ),
    )[0]
    revision_source = store.collect(
        _source(GROUP_A, "revision", "user-a", "I now like coffee")
    )
    assert revision_source is not None

    committed = store.commit_review(
        GROUP_A,
        (revision_source,),
        (
            MemoryProposal(
                operation="revise",
                item_id=target.short_id,
                content=" likes   COFFEE ",
                confidence=0.95,
                source_kind="self_statement",
            ),
        ),
    )

    assert [item.id for item in committed] == [survivor.id]
    assert store.get_item(GROUP_A, target.id) is None
    remaining = store.list_items(GROUP_A, include_expired=True)
    assert [item.id for item in remaining] == [survivor.id]
    assert remaining[0].source_count == 2
    assert remaining[0].base_confidence == pytest.approx(0.95)
    assert remaining[0].effective_score == pytest.approx(0.95)
    assert remaining[0].status == "active"
    assert remaining[0].source_kind == "self_statement"

    with sqlite3.connect(store.path) as conn:
        fts_rows = conn.execute(
            "SELECT item_id, content FROM memory_fts ORDER BY item_id"
        ).fetchall()
        survivor_revisions = conn.execute(
            "SELECT operation FROM memory_revisions WHERE item_id = ? ORDER BY id",
            (survivor.id,),
        ).fetchall()
        retired_audits = conn.execute(
            "SELECT operation, deleted_item_hash FROM memory_revisions "
            "WHERE deleted_item_hash IS NOT NULL"
        ).fetchall()

    assert fts_rows == [(survivor.id, "Likes coffee")]
    assert [row[0] for row in survivor_revisions] == ["candidate", "merge"]
    assert [row[0] for row in retired_audits] == ["add", "delete"]
    assert retired_audits[0][1]
    assert retired_audits[0][1] == retired_audits[1][1]


def test_direct_self_duplicate_revision_is_a_reinforcement(
    store: LongTermMemoryStore,
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    initial_source = store.collect(
        _source(GROUP_A, "initial", "user-a", "Likes tea")
    )
    assert initial_source is not None
    target = store.commit_review(
        GROUP_A,
        (initial_source,),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="user-a",
                content="Likes tea",
                confidence=0.8,
            ),
        ),
    )[0]
    revision_source = store.collect(
        _source(GROUP_A, "revision", "user-a", "Still likes tea")
    )
    assert revision_source is not None

    committed = store.commit_review(
        GROUP_A,
        (revision_source,),
        (
            MemoryProposal(
                operation="revise",
                item_id=target.short_id,
                content="  LIKES   TEA ",
                confidence=0.9,
                source_kind="self_statement",
            ),
        ),
    )

    assert [item.id for item in committed] == [target.id]
    assert len(store.list_items(GROUP_A, include_expired=True)) == 1
    reinforced = store.get_item(GROUP_A, target.id)
    assert reinforced is not None
    assert reinforced.content == "Likes tea"
    assert reinforced.source_count == 2
    assert reinforced.base_confidence == pytest.approx(0.9)
    with sqlite3.connect(store.path) as conn:
        operations = [
            row[0]
            for row in conn.execute(
                "SELECT operation FROM memory_revisions WHERE item_id = ? ORDER BY id",
                (target.id,),
            ).fetchall()
        ]
    assert operations == ["add", "reinforce"]


@pytest.mark.parametrize("operation", ["add", "revise", "contradict"])
def test_direct_low_confidence_content_operation_isolates_candidate_and_fts(
    store: LongTermMemoryStore, operation: str
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    target_source = store.collect(_source(GROUP_A, "target-low", "user-a", "Tea"))
    duplicate_source = store.collect(
        _source(GROUP_A, "duplicate-low", "user-a", "Coffee")
    )
    assert target_source is not None and duplicate_source is not None
    target = store.commit_review(
        GROUP_A,
        (target_source,),
        (
            MemoryProposal.add(
                subject_kind="user", subject_id="user-a", content="Tea", confidence=0.9
            ),
        ),
    )[0]
    duplicate = store.commit_review(
        GROUP_A,
        (duplicate_source,),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="user-a",
                content="Coffee",
                confidence=0.8,
                status="dormant",
            ),
        ),
    )[0]
    before = {item.id: item for item in store.list_items(GROUP_A, include_expired=True)}
    assert store._connection is not None
    fts_before = store._connection.execute(
        "SELECT item_id, content FROM memory_fts ORDER BY item_id"
    ).fetchall()
    source_id = store.collect(
        _source(GROUP_A, f"uncertain-{operation}", "user-a", "Maybe coffee")
    )
    assert source_id is not None
    proposal = (
        MemoryProposal.add(
            subject_kind="user", subject_id="user-a", content="Coffee", confidence=0.5
        )
        if operation == "add"
        else MemoryProposal(
            operation=operation,
            item_id=target.short_id,
            content="Coffee",
            confidence=0.5,
        )
    )

    committed = store.commit_review(GROUP_A, (source_id,), (proposal,))

    candidate = committed[0]
    expected_target = duplicate.id if operation == "add" else target.id
    assert candidate.status == "candidate"
    assert candidate.candidate_target_id == expected_target
    assert candidate.id not in before
    after = {item.id: item for item in store.list_items(GROUP_A, include_expired=True)}
    assert {item_id: after[item_id] for item_id in before} == before
    assert store._connection.execute(
        "SELECT item_id, content FROM memory_fts ORDER BY item_id"
    ).fetchall() == fts_before
    assert store._connection.execute(
        "SELECT candidate_count FROM review_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()[0] == 1


def test_repeated_candidate_proposal_reinforces_only_candidate_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "candidate-restart.sqlite3"
    store = LongTermMemoryStore(path)
    store.initialize()
    store.set_scope_enabled(GROUP_A, True)
    target_source = store.collect(_source(GROUP_A, "restart-target", "user-a", "Tea"))
    assert target_source is not None
    target = store.commit_review(
        GROUP_A,
        (target_source,),
        (
            MemoryProposal.add(
                subject_kind="user", subject_id="user-a", content="Tea", confidence=0.9
            ),
        ),
    )[0]
    target_before = store.get_item(GROUP_A, target.id)
    first_source = store.collect(
        _source(GROUP_A, "restart-first", "user-a", "Maybe still tea")
    )
    assert first_source is not None
    first = store.commit_review(
        GROUP_A,
        (first_source,),
        (
            MemoryProposal(
                operation="revise",
                item_id=target.short_id,
                content=" tea ",
                confidence=0.45,
            ),
        ),
    )[0]
    store.close()

    reopened = LongTermMemoryStore(path)
    reopened.initialize()
    second_source = reopened.collect(
        _source(GROUP_A, "restart-second", "user-a", "Maybe still tea again")
    )
    assert second_source is not None
    second = reopened.commit_review(
        GROUP_A,
        (second_source,),
        (
            MemoryProposal(
                operation="revise",
                item_id=target.id,
                content="TEA",
                confidence=0.55,
            ),
        ),
    )[0]

    assert second.id == first.id
    assert second.candidate_target_id == target.id
    assert second.status == "candidate"
    assert second.source_count == 2
    assert second.base_confidence == pytest.approx(0.55)
    assert reopened.get_item(GROUP_A, target.id) == target_before
    reopened.close()


@pytest.mark.parametrize("candidate_kind", ["targetless", "fact-backed"])
@pytest.mark.parametrize("first_operation", ["add", "mark_candidate"])
@pytest.mark.parametrize("second_operation", ["add", "mark_candidate"])
def test_direct_repeated_low_confidence_candidate_permutations_do_not_chain(
    tmp_path: Path,
    candidate_kind: str,
    first_operation: str,
    second_operation: str,
) -> None:
    path = tmp_path / "direct-candidate-restart.sqlite3"
    store = LongTermMemoryStore(path)
    store.initialize()
    store.set_scope_enabled(GROUP_A, True)
    fact = None
    if candidate_kind == "fact-backed":
        fact_source = store.collect(
            _source(GROUP_A, "established-fact", "user-a", "Likes tea")
        )
        assert fact_source is not None
        fact = store.commit_review(
            GROUP_A,
            (fact_source,),
            (
                MemoryProposal.add(
                    subject_kind="user",
                    subject_id="user-a",
                    content="Likes tea",
                    confidence=0.9,
                ),
            ),
        )[0]
    assert store._connection is not None
    fts_before = store._connection.execute(
        "SELECT item_id, content FROM memory_fts ORDER BY item_id"
    ).fetchall()

    def proposal(operation: str, confidence: float) -> MemoryProposal:
        return MemoryProposal(
            operation=operation,
            subject_kind="user",
            subject_id="user-a",
            content=" likes   TEA ",
            confidence=confidence,
            status="candidate" if operation == "mark_candidate" else "active",
        )

    first_source = store.collect(
        _source(GROUP_A, "candidate-first", "user-a", "Maybe I like tea")
    )
    assert first_source is not None
    first = store.commit_review(
        GROUP_A, (first_source,), (proposal(first_operation, 0.45),)
    )[0]
    store.close()

    reopened = LongTermMemoryStore(path)
    reopened.initialize()
    second_source = reopened.collect(
        _source(GROUP_A, "candidate-second", "user-a", "Maybe I still like tea")
    )
    assert second_source is not None
    second = reopened.commit_review(
        GROUP_A, (second_source,), (proposal(second_operation, 0.55),)
    )[0]

    expected_target = fact.id if fact is not None else None
    candidates = tuple(
        item
        for item in reopened.list_items(GROUP_A, include_expired=True)
        if item.status == "candidate"
    )
    assert candidates == (second,)
    assert second.id == first.id
    assert second.candidate_target_id == expected_target
    assert second.source_count == 2
    assert second.base_confidence == pytest.approx(0.55)
    assert reopened._connection is not None
    assert reopened._connection.execute(
        "SELECT item_id, content FROM memory_fts ORDER BY item_id"
    ).fetchall() == fts_before
    assert reopened.pending_sources(GROUP_A, 10) == ()
    review_rows = reopened._connection.execute(
        "SELECT source_count, proposed_count, accepted_count, candidate_count, "
        "rejected_count FROM review_runs ORDER BY id DESC LIMIT 2"
    ).fetchall()
    assert [tuple(row) for row in review_rows] == [
        (1, 1, 1, 1, 0),
        (1, 1, 1, 1, 0),
    ]
    assert [
        row[0]
        for row in reopened._connection.execute(
            "SELECT operation FROM memory_revisions WHERE item_id = ? ORDER BY id",
            (second.id,),
        ).fetchall()
    ] == ["candidate", "reinforce"]
    reopened.close()


def test_hard_delete_nulls_candidate_target_without_cross_scope_effects(
    store: LongTermMemoryStore,
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    store.set_scope_enabled(GROUP_B, True)
    target_source = store.collect(_source(GROUP_A, "delete-target", "user-a", "Tea"))
    assert target_source is not None
    target = store.commit_review(
        GROUP_A,
        (target_source,),
        (MemoryProposal.add(subject_kind="user", subject_id="user-a", content="Tea"),),
    )[0]
    candidate_source = store.collect(
        _source(GROUP_A, "delete-candidate", "user-a", "Maybe coffee")
    )
    assert candidate_source is not None
    candidate = store.commit_review(
        GROUP_A,
        (candidate_source,),
        (
            MemoryProposal(
                operation="revise",
                item_id=target.id,
                content="Coffee",
                confidence=0.5,
            ),
        ),
    )[0]

    assert store.hard_delete(GROUP_B, target.id) is False
    assert store.get_item(GROUP_A, candidate.id) == candidate
    assert store.hard_delete(GROUP_A, target.id) is True
    detached = store.get_item(GROUP_A, candidate.id)
    assert detached is not None
    assert detached.content == "Coffee"
    assert detached.status == "candidate"
    assert detached.candidate_target_id is None


def test_candidate_target_must_resolve_in_exact_scope(
    store: LongTermMemoryStore,
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    store.set_scope_enabled(GROUP_B, True)
    foreign_source = store.collect(_source(GROUP_B, "foreign-target", "user-a", "Tea"))
    assert foreign_source is not None
    foreign = store.commit_review(
        GROUP_B,
        (foreign_source,),
        (MemoryProposal.add(subject_kind="user", subject_id="user-a", content="Tea"),),
    )[0]
    local_source = store.collect(
        _source(GROUP_A, "cross-scope-candidate", "user-a", "Maybe coffee")
    )
    assert local_source is not None

    with pytest.raises(ValueError, match="candidate target does not exist in scope"):
        store.commit_review(
            GROUP_A,
            (local_source,),
            (
                MemoryProposal(
                    operation="mark_candidate",
                    candidate_target_id=foreign.short_id,
                    subject_kind="user",
                    subject_id="user-a",
                    content="Coffee",
                    confidence=0.5,
                    status="candidate",
                ),
            ),
        )

    assert [source.id for source in store.pending_sources(GROUP_A, 10)] == [local_source]
    assert store.list_items(GROUP_A, include_expired=True) == ()


def test_duplicate_revision_rolls_back_rows_fts_revisions_and_source_consumption(
    store: LongTermMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    target_source = store.collect(_source(GROUP_A, "target", "user-a", "Tea"))
    survivor_source = store.collect(
        _source(GROUP_A, "survivor", "user-a", "Coffee")
    )
    assert target_source is not None and survivor_source is not None
    target = store.commit_review(
        GROUP_A,
        (target_source,),
        (MemoryProposal.add(subject_kind="user", subject_id="user-a", content="Tea"),),
    )[0]
    survivor = store.commit_review(
        GROUP_A,
        (survivor_source,),
        (
            MemoryProposal.add(
                subject_kind="user", subject_id="user-a", content="Coffee"
            ),
        ),
    )[0]
    revision_source = store.collect(
        _source(GROUP_A, "revision", "user-a", "Actually coffee")
    )
    assert revision_source is not None
    original_record_revision = store._record_revision

    def fail_merge_revision(*args: object, **kwargs: object) -> None:
        if kwargs.get("operation") == "merge":
            raise RuntimeError("injected merge audit failure")
        original_record_revision(*args, **kwargs)

    monkeypatch.setattr(store, "_record_revision", fail_merge_revision)

    with pytest.raises(RuntimeError, match="injected merge audit failure"):
        store.commit_review(
            GROUP_A,
            (revision_source,),
            (
                MemoryProposal(
                    operation="revise",
                    item_id=target.id,
                    content="coffee",
                    confidence=0.9,
                ),
            ),
        )

    assert {item.id for item in store.list_items(GROUP_A, include_expired=True)} == {
        target.id,
        survivor.id,
    }
    assert [source.id for source in store.pending_sources(GROUP_A, 10)] == [
        revision_source
    ]
    with sqlite3.connect(store.path) as conn:
        fts_ids = {
            row[0] for row in conn.execute("SELECT item_id FROM memory_fts").fetchall()
        }
        deleted_audits = conn.execute(
            "SELECT COUNT(*) FROM memory_revisions WHERE deleted_item_hash IS NOT NULL"
        ).fetchone()[0]
    assert fts_ids == {target.id, survivor.id}
    assert deleted_audits == 0


def test_hard_delete_scrubs_revisions_and_fts(store: LongTermMemoryStore) -> None:
    store.set_scope_enabled(GROUP_A, True)
    source_id = store.collect(_source(GROUP_A, "one", "user-a", "source text"))
    assert source_id is not None
    item = store.commit_review(
        GROUP_A,
        [source_id],
        [
            MemoryProposal.add(
                subject_kind="user",
                subject_id="user-a",
                category="preference",
                content="Prefers concise answers",
            )
        ],
    )[0]

    assert store.hard_delete(GROUP_B, item.id) is False
    assert store.hard_delete(GROUP_A, item.id) is True
    assert store.get_item(GROUP_A, item.id) is None

    with sqlite3.connect(store.path) as conn:
        fts_count = conn.execute(
            "SELECT COUNT(*) FROM memory_fts WHERE item_id = ?", (item.id,)
        ).fetchone()[0]
        revision = conn.execute(
            """
            SELECT item_id, before_summary, after_summary, evidence_excerpt,
                   deleted_item_hash
            FROM memory_revisions
            WHERE deleted_item_hash IS NOT NULL
            """
        ).fetchone()

    assert fts_count == 0
    assert revision[:4] == (None, None, None, None)
    assert revision[4]


def test_direct_revision_cannot_downgrade_sensitive_item(
    store: LongTermMemoryStore,
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    source_id = store.collect(_source(GROUP_A, "sensitive", "user-a", "sensitive"))
    assert source_id is not None
    item = store.commit_review(
        GROUP_A,
        (source_id,),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="user-a",
                content="private health fact",
                sensitivity="sensitive",
            ),
        ),
    )[0]

    with pytest.raises(ValueError, match="cannot be downgraded"):
        store.commit_review(
            GROUP_A,
            (),
            (
                MemoryProposal(
                    operation="revise",
                    item_id=item.id,
                    content="benign replacement",
                    sensitivity="normal",
                ),
            ),
        )

    unchanged = store.get_item(GROUP_A, item.id)
    assert unchanged is not None
    assert unchanged.content == "private health fact"
    assert unchanged.sensitivity == "sensitive"


def test_expiry_and_decay_are_bounded_and_scope_independent(
    store: LongTermMemoryStore,
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    stale_id = store.collect(
        _source(GROUP_A, "stale", "user-a", "stale", created_at=1)
    )
    fresh_id = store.collect(
        _source(GROUP_A, "fresh", "user-a", "fresh", created_at=700_000)
    )
    assert stale_id is not None and fresh_id is not None

    assert store.expire_raw(604_802) == 1
    assert [source.id for source in store.pending_sources(GROUP_A, 10)] == [fresh_id]

    item = store.commit_review(
        GROUP_A,
        [fresh_id],
        [
            MemoryProposal.add(
                subject_kind="user",
                subject_id="user-a",
                category="recurring_topic",
                content="An aging topic",
                confidence=0.70,
                created_at=1,
            )
        ],
    )[0]
    assert item.status == "active"

    assert store.apply_decay(5_000_000) == 1
    decayed = store.get_item(GROUP_A, item.id)
    assert decayed is not None
    assert decayed.status == "dormant"
    assert store.retrieve_candidates(GROUP_A) == ()


def test_expire_raw_deletes_source_at_exact_ttl_boundary(
    store: LongTermMemoryStore,
) -> None:
    store.set_scope_enabled(GROUP_A, True)
    source_id = store.collect(
        _source(GROUP_A, "ttl-boundary", "user-a", "expires now", created_at=1)
    )
    assert source_id is not None

    assert store.expire_raw(604_801) == 1
    assert store.pending_sources(GROUP_A, 10) == ()


def test_memory_scope_rejects_incomplete_or_unknown_scopes() -> None:
    with pytest.raises(ValueError):
        MemoryScope("group", "")
    with pytest.raises(ValueError):
        MemoryScope("user", "123")  # type: ignore[arg-type]
