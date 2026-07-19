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

    assert schema_module.SCHEMA_VERSION == 1
    assert callable(schema_module.migrate)


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
                confidence=0.41,
                created_at=1,
            )
        ],
    )[0]
    assert item.status == "active"

    assert store.apply_decay(3_000_000) == 1
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
