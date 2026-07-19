"""Typed, scope-mandatory persistence for long-term memory."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterable, Iterator, Sequence
import uuid

from .long_term_memory_models import (
    ALLOWED_CATEGORIES,
    ALLOWED_OPERATIONS,
    ALLOWED_STATUSES,
    INDEXED_STATUSES,
    MemoryItem,
    MemoryProposal,
    MemoryScope,
    MemorySource,
    MemoryStatusName,
    MemoryStoreStatus,
    ScopeKind,
)
from .long_term_memory_schema import SCHEMA_VERSION, migrate


class LongTermMemoryStore:
    """SQLite memory store whose public reads and writes require an exact scope."""

    def __init__(
        self,
        path: Path | str,
        *,
        default_scope_enabled: bool = False,
        raw_ttl_seconds: int = 604_800,
        decay_grace_seconds: int = 2_592_000,
        dormant_threshold: float = 0.40,
    ) -> None:
        self.path = Path(path)
        self.default_scope_enabled = bool(default_scope_enabled)
        self.raw_ttl_seconds = max(60, int(raw_ttl_seconds))
        self.decay_grace_seconds = max(0, int(decay_grace_seconds))
        self.dormant_threshold = min(1.0, max(0.0, float(dormant_threshold)))
        self._connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        self._connection = connection
        try:
            migrate(connection)
            os.chmod(self.path, 0o600)
        except BaseException:
            connection.close()
            self._connection = None
            raise

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def is_scope_enabled(self, scope: MemoryScope) -> bool:
        row = self._conn.execute(
            """
            SELECT enabled FROM memory_scopes
            WHERE scope_kind = ? AND scope_id = ?
            """,
            self._scope_params(scope),
        ).fetchone()
        return bool(row["enabled"]) if row is not None else self.default_scope_enabled

    def set_scope_enabled(self, scope: MemoryScope, enabled: bool) -> None:
        now = int(time.time())
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO memory_scopes(scope_kind, scope_id, enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_kind, scope_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (*self._scope_params(scope), int(bool(enabled)), now),
            )

    def collect(self, source: MemorySource) -> int | None:
        created_at = int(source.created_at or time.time())
        with self._transaction() as conn:
            if not self._is_scope_enabled_conn(conn, source.scope):
                return None
            cursor = conn.execute(
                """
                INSERT INTO review_buffer(
                    scope_kind, scope_id, message_id, sender_id, text,
                    message_timestamp, mentioned_ids_json, quoted_sender_id,
                    is_reply, direct_interaction, command_class, collection_reason,
                    explicit_source, review_state, attempt_count, next_attempt_at,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.scope.kind,
                    source.scope.id,
                    str(source.message_id),
                    str(source.sender_id),
                    str(source.text),
                    int(source.message_timestamp),
                    json.dumps([str(item) for item in source.mentioned_ids]),
                    str(source.quoted_sender_id) if source.quoted_sender_id is not None else None,
                    int(source.is_reply),
                    int(source.direct_interaction),
                    source.command_class,
                    source.collection_reason,
                    int(source.explicit),
                    source.review_state,
                    max(0, int(source.attempt_count)),
                    max(0, int(source.next_attempt_at)),
                    created_at,
                ),
            )
        return int(cursor.lastrowid)

    def pending_sources(
        self,
        scope: MemoryScope,
        limit: int,
        *,
        now: int | None = None,
    ) -> tuple[MemorySource, ...]:
        current = int(time.time()) if now is None else int(now)
        rows = self._conn.execute(
            """
            SELECT * FROM review_buffer
            WHERE scope_kind = ? AND scope_id = ?
              AND review_state = 'pending' AND next_attempt_at <= ?
            ORDER BY created_at, id
            LIMIT ?
            """,
            (*self._scope_params(scope), current, max(1, int(limit))),
        ).fetchall()
        return tuple(self._source(row) for row in rows)

    def commit_review(
        self,
        scope: MemoryScope,
        source_ids: Sequence[int],
        operations: Sequence[MemoryProposal],
    ) -> tuple[MemoryItem, ...]:
        unique_source_ids = tuple(dict.fromkeys(int(value) for value in source_ids))
        committed_ids: list[str] = []
        now = int(time.time())
        with self._transaction() as conn:
            if not self._is_scope_enabled_conn(conn, scope):
                raise RuntimeError("memory scope was disabled before review commit")
            self._require_scoped_sources(conn, scope, unique_source_ids)
            for operation in operations:
                committed_ids.extend(self._apply_operation(conn, scope, operation, now))
            if unique_source_ids:
                placeholders = ",".join("?" for _ in unique_source_ids)
                conn.execute(
                    f"""
                    DELETE FROM review_buffer
                    WHERE scope_kind = ? AND scope_id = ?
                      AND id IN ({placeholders})
                    """,
                    (*self._scope_params(scope), *unique_source_ids),
                )
            conn.execute(
                """
                INSERT INTO review_runs(
                    scope_hash, trigger_class, source_count,
                    proposed_count, accepted_count, candidate_count, rejected_count,
                    duration_ms, retry_count, error_class, started_at, finished_at
                ) VALUES (?, 'review', ?, ?, ?, ?, 0, 0, 0, NULL, ?, ?)
                """,
                (
                    self._scope_hash(scope),
                    len(unique_source_ids),
                    len(operations),
                    len(operations),
                    sum(
                        op.operation == "mark_candidate" or op.status == "candidate"
                        for op in operations
                    ),
                    now,
                    now,
                ),
            )
        return tuple(
            item
            for item_id in committed_ids
            if (item := self.get_item(scope, item_id)) is not None
        )

    def list_items(
        self,
        scope: MemoryScope,
        *,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        statuses: Iterable[str] | None = None,
        include_expired: bool = False,
        now: int | None = None,
        limit: int = 100,
    ) -> tuple[MemoryItem, ...]:
        clauses = ["scope_kind = ?", "scope_id = ?"]
        params: list[object] = [scope.kind, scope.id]
        if subject_kind is not None:
            clauses.append("subject_kind = ?")
            params.append(str(subject_kind))
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(str(subject_id))
        if statuses is not None:
            normalized = tuple(dict.fromkeys(str(status) for status in statuses))
            if not normalized:
                return ()
            clauses.append(f"status IN ({','.join('?' for _ in normalized)})")
            params.extend(normalized)
        if not include_expired:
            current = int(time.time()) if now is None else int(now)
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(current)
        params.append(max(1, int(limit)))
        rows = self._conn.execute(
            f"""
            SELECT * FROM memory_items
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, created_at DESC, id
            LIMIT ?
            """,
            params,
        ).fetchall()
        return tuple(self._item(row) for row in rows)

    def get_item(self, scope: MemoryScope, item_id: str) -> MemoryItem | None:
        row = self._conn.execute(
            """
            SELECT * FROM memory_items
            WHERE scope_kind = ? AND scope_id = ?
              AND (id = ? OR short_id = ?)
            """,
            (*self._scope_params(scope), str(item_id), str(item_id)),
        ).fetchone()
        return self._item(row) if row is not None else None

    def retrieve_candidates(
        self,
        scope: MemoryScope,
        *,
        subject_ids: Iterable[str] | None = None,
        query: str = "",
        minimum_score: float = 0.0,
        now: int | None = None,
        limit: int = 12,
    ) -> tuple[MemoryItem, ...]:
        current = int(time.time()) if now is None else int(now)
        clauses = [
            "m.scope_kind = ?",
            "m.scope_id = ?",
            "m.status = 'active'",
            "m.effective_score >= ?",
            "(m.expires_at IS NULL OR m.expires_at > ?)",
        ]
        params: list[object] = [
            scope.kind,
            scope.id,
            min(1.0, max(0.0, float(minimum_score))),
            current,
        ]
        if subject_ids is not None:
            normalized = tuple(dict.fromkeys(str(item) for item in subject_ids))
            if not normalized:
                return ()
            clauses.append(f"m.subject_id IN ({','.join('?' for _ in normalized)})")
            params.extend(normalized)

        match = self._fts_query(query)
        rank_sql = "0.0"
        join_sql = ""
        if match:
            join_sql = "JOIN memory_fts f ON f.item_id = m.id"
            clauses.append("memory_fts MATCH ?")
            params.append(match)
            rank_sql = "bm25(memory_fts)"
        params.append(max(1, int(limit)))
        rows = self._conn.execute(
            f"""
            SELECT m.*, {rank_sql} AS text_rank
            FROM memory_items m
            {join_sql}
            WHERE {' AND '.join(clauses)}
            ORDER BY text_rank, m.effective_score DESC,
                     m.source_count DESC, m.last_supported_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return tuple(self._item(row) for row in rows)

    def hard_delete(self, scope: MemoryScope, item_id: str) -> bool:
        with self._transaction() as conn:
            item = self._get_item_row(conn, scope, item_id)
            if item is None:
                return False
            self._hard_delete_row(conn, item, actor_class="user")
        return True

    def clear_subject(
        self,
        scope: MemoryScope,
        subject_kind: str,
        subject_id: str,
        *,
        actor_class: str = "user",
    ) -> int:
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE scope_kind = ? AND scope_id = ?
                  AND subject_kind = ? AND subject_id = ?
                """,
                (*self._scope_params(scope), str(subject_kind), str(subject_id)),
            ).fetchall()
            for row in rows:
                self._hard_delete_row(conn, row, actor_class=actor_class)
        return len(rows)

    def status(self, scope: MemoryScope) -> MemoryStoreStatus:
        counts = self._conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_count,
                SUM(CASE WHEN status = 'candidate' THEN 1 ELSE 0 END) AS candidate_count
            FROM memory_items
            WHERE scope_kind = ? AND scope_id = ?
            """,
            self._scope_params(scope),
        ).fetchone()
        pending = self._conn.execute(
            """
            SELECT COUNT(*) AS count FROM review_buffer
            WHERE scope_kind = ? AND scope_id = ? AND review_state = 'pending'
            """,
            self._scope_params(scope),
        ).fetchone()
        review = self._conn.execute(
            """
            SELECT MAX(finished_at) AS last_review_at FROM review_runs
            WHERE scope_hash = ? AND error_class IS NULL
            """,
            (self._scope_hash(scope),),
        ).fetchone()
        return MemoryStoreStatus(
            enabled=self.is_scope_enabled(scope),
            pending_count=int(pending["count"] if pending else 0),
            active_count=int(counts["active_count"] or 0),
            candidate_count=int(counts["candidate_count"] or 0),
            last_review_at=(
                int(review["last_review_at"])
                if review and review["last_review_at"] is not None
                else None
            ),
        )

    def expire_raw(self, now: int) -> int:
        cutoff = int(now) - self.raw_ttl_seconds
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM review_buffer WHERE created_at <= ?",
                (cutoff,),
            )
        return max(0, int(cursor.rowcount))

    def apply_decay(self, now: int) -> int:
        changed = 0
        current = int(now)
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE status = 'active' AND decay_exempt = 0
                """
            ).fetchall()
            for row in rows:
                if row["expires_at"] is not None and int(row["expires_at"]) <= current:
                    score = float(row["effective_score"])
                else:
                    age_after_grace = (
                        current
                        - int(row["last_supported_at"])
                        - self.decay_grace_seconds
                    )
                    if age_after_grace <= 0:
                        continue
                    score = max(
                        0.0,
                        float(row["base_confidence"])
                        - self._daily_decay(str(row["category"]))
                        * (age_after_grace / 86_400),
                    )
                    if math.isclose(score, float(row["effective_score"]), abs_tol=1e-9):
                        continue
                status = "dormant" if score < self.dormant_threshold or (
                    row["expires_at"] is not None and int(row["expires_at"]) <= current
                ) else "active"
                dormant_at = current if status == "dormant" else None
                conn.execute(
                    """
                    UPDATE memory_items
                    SET effective_score = ?, status = ?, dormant_at = ?,
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (score, status, dormant_at, current, row["id"]),
                )
                self._sync_fts(conn, str(row["id"]))
                if status == "dormant":
                    self._record_revision(
                        conn,
                        item_id=str(row["id"]),
                        operation="dormancy",
                        actor_class="maintenance",
                        before_summary=str(row["content"]),
                        after_summary=str(row["content"]),
                        now=current,
                    )
                changed += 1
        return changed

    def mark_review_failure(
        self,
        scope: MemoryScope,
        source_ids: Sequence[int],
        *,
        error_class: str,
        next_attempt_at: int,
        trigger_class: str = "review",
        now: int | None = None,
    ) -> int:
        current = int(time.time()) if now is None else int(now)
        unique_ids = tuple(dict.fromkeys(int(value) for value in source_ids))
        if not unique_ids:
            return 0
        with self._transaction() as conn:
            self._require_scoped_sources(conn, scope, unique_ids)
            placeholders = ",".join("?" for _ in unique_ids)
            cursor = conn.execute(
                f"""
                UPDATE review_buffer
                SET attempt_count = attempt_count + 1, next_attempt_at = ?
                WHERE scope_kind = ? AND scope_id = ?
                  AND id IN ({placeholders})
                """,
                (int(next_attempt_at), *self._scope_params(scope), *unique_ids),
            )
            conn.execute(
                """
                INSERT INTO review_runs(
                    scope_hash, trigger_class, source_count,
                    proposed_count, accepted_count, candidate_count, rejected_count,
                    duration_ms, retry_count, error_class, started_at, finished_at
                ) VALUES (?, ?, ?, 0, 0, 0, 0, 0, 1, ?, ?, ?)
                """,
                (
                    self._scope_hash(scope),
                    trigger_class,
                    len(unique_ids),
                    str(error_class),
                    current,
                    current,
                ),
            )
        return max(0, int(cursor.rowcount))

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("long-term memory store is not initialized")
        return self._connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def _apply_operation(
        self,
        conn: sqlite3.Connection,
        scope: MemoryScope,
        proposal: MemoryProposal,
        now: int,
    ) -> list[str]:
        if proposal.operation not in ALLOWED_OPERATIONS:
            raise ValueError(f"unsupported memory operation: {proposal.operation}")
        if proposal.operation in {"add", "mark_candidate"}:
            return [self._insert_item(conn, scope, proposal, now)]
        if not proposal.item_id:
            raise ValueError(f"{proposal.operation} operation requires item_id")
        row = self._get_item_row(conn, scope, proposal.item_id)
        if row is None:
            raise ValueError("memory operation target does not exist in scope")

        item_id = str(row["id"])
        if proposal.operation == "forget":
            self._hard_delete_row(conn, row, actor_class=proposal.actor_class)
            return []
        if proposal.operation == "reinforce":
            confidence = self._confidence(
                proposal.confidence
                if proposal.confidence is not None
                else float(row["base_confidence"])
            )
            score = max(float(row["effective_score"]), confidence)
            status = "active" if row["status"] in {"candidate", "dormant"} else row["status"]
            conn.execute(
                """
                UPDATE memory_items
                SET base_confidence = MAX(base_confidence, ?), effective_score = ?,
                    status = ?, source_count = source_count + 1,
                    source_kind = ?, updated_at = ?, last_supported_at = ?,
                    dormant_at = NULL, version = version + 1
                WHERE id = ?
                """,
                (confidence, score, status, proposal.source_kind, now, now, item_id),
            )
            self._record_revision(
                conn,
                item_id=item_id,
                operation="reinforce",
                actor_class=proposal.actor_class,
                before_summary=str(row["content"]),
                after_summary=str(row["content"]),
                now=now,
            )
            self._sync_fts(conn, item_id)
            return [item_id]
        if proposal.operation == "revise":
            content = self._content(proposal.content)
            category = proposal.category or str(row["category"])
            self._require_category(category)
            confidence = self._confidence(
                proposal.confidence
                if proposal.confidence is not None
                else float(row["base_confidence"])
            )
            status = proposal.status or str(row["status"])
            self._require_status(status)
            conn.execute(
                """
                UPDATE memory_items
                SET category = ?, content = ?, base_confidence = ?,
                    effective_score = ?, status = ?, sensitivity = ?,
                    source_kind = ?, updated_at = ?, last_supported_at = ?,
                    expires_at = ?, dormant_at = CASE WHEN ? = 'dormant' THEN ? ELSE NULL END,
                    version = version + 1
                WHERE id = ?
                """,
                (
                    category,
                    content,
                    confidence,
                    confidence,
                    status,
                    proposal.sensitivity,
                    proposal.source_kind,
                    now,
                    now,
                    proposal.expires_at,
                    status,
                    now,
                    item_id,
                ),
            )
            self._record_revision(
                conn,
                item_id=item_id,
                operation="revise",
                actor_class=proposal.actor_class,
                before_summary=str(row["content"]),
                after_summary=content,
                now=now,
            )
            self._sync_fts(conn, item_id)
            return [item_id]
        if proposal.operation == "contradict":
            conn.execute(
                """
                UPDATE memory_items
                SET status = 'contradicted', updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (now, item_id),
            )
            self._record_revision(
                conn,
                item_id=item_id,
                operation="contradict",
                actor_class=proposal.actor_class,
                before_summary=str(row["content"]),
                after_summary=proposal.content,
                now=now,
            )
            self._sync_fts(conn, item_id)
            if proposal.content:
                replacement = MemoryProposal.add(
                    subject_kind=proposal.subject_kind or str(row["subject_kind"]),
                    subject_id=proposal.subject_id or str(row["subject_id"]),
                    category=proposal.category or str(row["category"]),
                    content=proposal.content,
                    confidence=(proposal.confidence if proposal.confidence is not None else 0.75),
                    sensitivity=proposal.sensitivity,
                    source_kind=proposal.source_kind,
                    explicit_memory=proposal.explicit_memory,
                    decay_exempt=proposal.decay_exempt,
                    expires_at=proposal.expires_at,
                    actor_class=proposal.actor_class,
                )
                return [item_id, self._insert_item(conn, scope, replacement, now)]
            return [item_id]
        if proposal.operation == "merge":
            for related_id in proposal.related_item_ids:
                related = self._get_item_row(conn, scope, related_id)
                if related is not None and related["id"] != item_id:
                    self._hard_delete_row(conn, related, actor_class=proposal.actor_class)
            conn.execute(
                """
                UPDATE memory_items
                SET source_count = source_count + ?, updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (len(proposal.related_item_ids), now, item_id),
            )
            self._record_revision(
                conn,
                item_id=item_id,
                operation="merge",
                actor_class=proposal.actor_class,
                before_summary=str(row["content"]),
                after_summary=str(row["content"]),
                now=now,
            )
            return [item_id]
        raise AssertionError("all allowed operations are handled")

    def _insert_item(
        self,
        conn: sqlite3.Connection,
        scope: MemoryScope,
        proposal: MemoryProposal,
        now: int,
    ) -> str:
        if proposal.subject_kind is None or not str(proposal.subject_kind).strip():
            raise ValueError("add operation requires subject_kind")
        if proposal.subject_id is None or not str(proposal.subject_id).strip():
            raise ValueError("add operation requires subject_id")
        category = proposal.category or "preference"
        self._require_category(category)
        content = self._content(proposal.content)
        status = (
            "candidate"
            if proposal.operation == "mark_candidate"
            else (proposal.status or "active")
        )
        self._require_status(status)
        confidence = self._confidence(
            proposal.confidence if proposal.confidence is not None else 0.75
        )
        created_at = int(proposal.created_at if proposal.created_at is not None else now)
        duplicate = conn.execute(
            """
            SELECT * FROM memory_items
            WHERE scope_kind = ? AND scope_id = ?
              AND subject_kind = ? AND subject_id = ? AND category = ?
              AND lower(trim(content)) = lower(trim(?))
              AND status IN ('active', 'candidate', 'dormant')
            ORDER BY updated_at DESC, id
            LIMIT 1
            """,
            (
                scope.kind,
                scope.id,
                str(proposal.subject_kind),
                str(proposal.subject_id),
                category,
                content,
            ),
        ).fetchone()
        if duplicate is not None:
            item_id = str(duplicate["id"])
            duplicate_status = str(duplicate["status"])
            reinforced_status = (
                status
                if status == "active" and duplicate_status in {"candidate", "dormant"}
                else duplicate_status
            )
            conn.execute(
                """
                UPDATE memory_items
                SET base_confidence = MAX(base_confidence, ?),
                    effective_score = MAX(effective_score, ?), status = ?,
                    source_count = source_count + 1, source_kind = ?,
                    updated_at = ?, last_supported_at = ?, dormant_at = NULL,
                    version = version + 1
                WHERE id = ?
                """,
                (
                    confidence,
                    confidence,
                    reinforced_status,
                    proposal.source_kind,
                    now,
                    now,
                    item_id,
                ),
            )
            self._record_revision(
                conn,
                item_id=item_id,
                operation="reinforce",
                actor_class=proposal.actor_class,
                before_summary=str(duplicate["content"]),
                after_summary=content,
                now=now,
            )
            self._sync_fts(conn, item_id)
            return item_id
        item_id = uuid.uuid4().hex
        short_id = item_id[:12]
        conn.execute(
            """
            INSERT INTO memory_items(
                id, short_id, scope_kind, scope_id, subject_kind, subject_id,
                category, content, base_confidence, effective_score, status,
                sensitivity, source_kind, source_count, explicit_memory,
                decay_exempt, created_at, updated_at, last_supported_at,
                expires_at, dormant_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                item_id,
                short_id,
                scope.kind,
                scope.id,
                str(proposal.subject_kind),
                str(proposal.subject_id),
                category,
                content,
                confidence,
                confidence,
                status,
                proposal.sensitivity,
                proposal.source_kind,
                int(proposal.explicit_memory),
                int(proposal.decay_exempt),
                created_at,
                now,
                created_at,
                proposal.expires_at,
                (now if status == "dormant" else None),
            ),
        )
        self._record_revision(
            conn,
            item_id=item_id,
            operation="add" if status != "candidate" else "candidate",
            actor_class=proposal.actor_class,
            before_summary=None,
            after_summary=content,
            now=now,
        )
        self._sync_fts(conn, item_id)
        return item_id

    def _hard_delete_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        actor_class: str,
    ) -> None:
        item_id = str(row["id"])
        deleted_hash = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
        conn.execute("DELETE FROM memory_fts WHERE item_id = ?", (item_id,))
        conn.execute(
            """
            UPDATE memory_revisions
            SET item_id = NULL, before_summary = NULL, after_summary = NULL,
                evidence_excerpt = NULL, deleted_item_hash = ?
            WHERE item_id = ?
            """,
            (deleted_hash, item_id),
        )
        conn.execute("DELETE FROM memory_items WHERE id = ?", (item_id,))
        conn.execute(
            """
            INSERT INTO memory_revisions(
                item_id, operation, actor_class, before_summary, after_summary,
                evidence_excerpt, deleted_item_hash, created_at
            ) VALUES (NULL, 'delete', ?, NULL, NULL, NULL, ?, ?)
            """,
            (actor_class, deleted_hash, int(time.time())),
        )

    def _sync_fts(self, conn: sqlite3.Connection, item_id: str) -> None:
        conn.execute("DELETE FROM memory_fts WHERE item_id = ?", (item_id,))
        row = conn.execute(
            "SELECT content, status FROM memory_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is not None and row["status"] in INDEXED_STATUSES:
            conn.execute(
                "INSERT INTO memory_fts(item_id, content) VALUES (?, ?)",
                (item_id, row["content"]),
            )

    @staticmethod
    def _record_revision(
        conn: sqlite3.Connection,
        *,
        item_id: str,
        operation: str,
        actor_class: str,
        before_summary: str | None,
        after_summary: str | None,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO memory_revisions(
                item_id, operation, actor_class, before_summary, after_summary,
                evidence_excerpt, deleted_item_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (item_id, operation, actor_class, before_summary, after_summary, now),
        )

    @staticmethod
    def _scope_params(scope: MemoryScope) -> tuple[str, str]:
        return scope.kind, scope.id

    @staticmethod
    def _scope_hash(scope: MemoryScope) -> str:
        value = f"{scope.kind}\0{scope.id}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _content(value: str | None) -> str:
        content = str(value or "").strip()
        if not content:
            raise ValueError("memory content must not be empty")
        return content

    @staticmethod
    def _confidence(value: float) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("memory confidence must be finite")
        return min(1.0, max(0.0, number))

    @staticmethod
    def _require_category(category: str) -> None:
        if category not in ALLOWED_CATEGORIES:
            raise ValueError(f"unsupported memory category: {category}")

    @staticmethod
    def _require_status(status: str) -> None:
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"unsupported memory status: {status}")

    @staticmethod
    def _daily_decay(category: str) -> float:
        return {
            "identity": 0.001,
            "preference": 0.002,
            "group_norm": 0.002,
            "relationship": 0.005,
            "project": 0.010,
            "recurring_topic": 0.015,
        }.get(category, 0.005)

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w\u3400-\u9fff]+", str(query), flags=re.UNICODE)
        return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:20])

    @staticmethod
    def _require_scoped_sources(
        conn: sqlite3.Connection,
        scope: MemoryScope,
        source_ids: Sequence[int],
    ) -> None:
        if not source_ids:
            return
        placeholders = ",".join("?" for _ in source_ids)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count FROM review_buffer
            WHERE scope_kind = ? AND scope_id = ?
              AND id IN ({placeholders})
            """,
            (scope.kind, scope.id, *source_ids),
        ).fetchone()
        if row is None or int(row["count"]) != len(source_ids):
            raise ValueError("review sources do not all belong to the exact scope")

    def _is_scope_enabled_conn(
        self,
        conn: sqlite3.Connection,
        scope: MemoryScope,
    ) -> bool:
        row = conn.execute(
            """
            SELECT enabled FROM memory_scopes
            WHERE scope_kind = ? AND scope_id = ?
            """,
            self._scope_params(scope),
        ).fetchone()
        return bool(row["enabled"]) if row is not None else self.default_scope_enabled

    @staticmethod
    def _get_item_row(
        conn: sqlite3.Connection,
        scope: MemoryScope,
        item_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM memory_items
            WHERE scope_kind = ? AND scope_id = ?
              AND (id = ? OR short_id = ?)
            """,
            (scope.kind, scope.id, str(item_id), str(item_id)),
        ).fetchone()

    @staticmethod
    def _source(row: sqlite3.Row) -> MemorySource:
        mentioned = json.loads(str(row["mentioned_ids_json"]))
        return MemorySource(
            id=int(row["id"]),
            scope=MemoryScope(
                str(row["scope_kind"]), str(row["scope_id"])
            ),  # type: ignore[arg-type]
            message_id=str(row["message_id"]),
            sender_id=str(row["sender_id"]),
            text=str(row["text"]),
            message_timestamp=int(row["message_timestamp"]),
            mentioned_ids=tuple(str(value) for value in mentioned),
            quoted_sender_id=(
                str(row["quoted_sender_id"])
                if row["quoted_sender_id"] is not None
                else None
            ),
            is_reply=bool(row["is_reply"]),
            direct_interaction=bool(row["direct_interaction"]),
            command_class=row["command_class"],
            collection_reason=str(row["collection_reason"]),
            explicit=bool(row["explicit_source"]),
            review_state=str(row["review_state"]),
            attempt_count=int(row["attempt_count"]),
            next_attempt_at=int(row["next_attempt_at"]),
            created_at=int(row["created_at"]),
        )

    @staticmethod
    def _item(row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=str(row["id"]),
            short_id=str(row["short_id"]),
            scope=MemoryScope(
                str(row["scope_kind"]), str(row["scope_id"])
            ),  # type: ignore[arg-type]
            subject_kind=str(row["subject_kind"]),
            subject_id=str(row["subject_id"]),
            category=str(row["category"]),
            content=str(row["content"]),
            base_confidence=float(row["base_confidence"]),
            effective_score=float(row["effective_score"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            sensitivity=str(row["sensitivity"]),
            source_kind=str(row["source_kind"]),
            source_count=int(row["source_count"]),
            explicit_memory=bool(row["explicit_memory"]),
            decay_exempt=bool(row["decay_exempt"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            last_supported_at=int(row["last_supported_at"]),
            expires_at=(int(row["expires_at"]) if row["expires_at"] is not None else None),
            dormant_at=(int(row["dormant_at"]) if row["dormant_at"] is not None else None),
            version=int(row["version"]),
        )
