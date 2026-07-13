"""SQLite persistence for schedules and their individual runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

from .scheduler import Schedule, ScheduleRun


class ScheduleStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    is_group INTEGER NOT NULL,
                    creator_id TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    mentions_json TEXT NOT NULL DEFAULT '[]',
                    timezone TEXT NOT NULL,
                    start_at INTEGER NOT NULL,
                    next_run_at INTEGER,
                    rrule TEXT,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    run_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    missed_count INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_run_at INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(chat_id, is_group, source_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_schedules_due
                    ON schedules(status, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_schedules_chat
                    ON schedules(chat_id, is_group, created_at);
                CREATE TABLE IF NOT EXISTS schedule_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
                    due_at INTEGER NOT NULL,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    state TEXT NOT NULL,
                    job_id TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    manual INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(schedule_id, due_at, manual)
                );
                CREATE INDEX IF NOT EXISTS idx_schedule_runs_active
                    ON schedule_runs(schedule_id, state);
                """
            )
            self._migrate_legacy_columns(conn)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _migrate_legacy_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(schedules)").fetchall()
        }
        if "rrule" not in columns:
            conn.execute("ALTER TABLE schedules ADD COLUMN rrule TEXT")
        if "description" not in columns:
            conn.execute("ALTER TABLE schedules ADD COLUMN description TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def create(self, schedule: Schedule) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO schedules (
                    id, chat_id, is_group, creator_id, source_message_id,
                    kind, action, payload, mentions_json, timezone,
                    start_at, next_run_at, rrule, description, status,
                    run_count, success_count, failure_count, missed_count,
                    consecutive_failures, created_at, updated_at, last_run_at, last_error
                ) VALUES (
                    :id, :chat_id, :is_group, :creator_id, :source_message_id,
                    :kind, :action, :payload, :mentions_json, :timezone,
                    :start_at, :next_run_at, :rrule, :description, :status,
                    :run_count, :success_count, :failure_count, :missed_count,
                    :consecutive_failures, :created_at, :updated_at, :last_run_at, :last_error
                )
                """,
                self._params(schedule),
            )

    def get(self, schedule_id: str) -> Schedule | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        return self._schedule(row)

    def get_by_source_message(
        self,
        chat_id: str,
        is_group: bool,
        message_id: str,
    ) -> Schedule | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM schedules
                WHERE chat_id = ? AND is_group = ? AND source_message_id = ?
                """,
                (chat_id, int(is_group), message_id),
            ).fetchone()
        return self._schedule(row)

    def list_for_chat(
        self,
        chat_id: str,
        is_group: bool,
        *,
        active_only: bool = True,
    ) -> list[Schedule]:
        query = "SELECT * FROM schedules WHERE chat_id = ? AND is_group = ?"
        params: list[object] = [chat_id, int(is_group)]
        if active_only:
            query += " AND status IN ('active', 'paused', 'finishing')"
        query += " ORDER BY created_at, rowid"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [item for row in rows if (item := self._schedule(row)) is not None]

    def resolve_ref(
        self,
        chat_id: str,
        is_group: bool,
        ref: str | None,
        *,
        default_ref: str = "-1",
        active_only: bool = True,
    ) -> Schedule | None:
        raw = (ref or "").strip() or default_ref
        items = self.list_for_chat(chat_id, is_group, active_only=active_only)
        exact = next((item for item in items if item.id == raw), None)
        if exact:
            return exact
        prefixes = [item for item in items if item.id.startswith(raw)]
        if len(prefixes) == 1:
            return prefixes[0]
        try:
            index = int(raw)
        except ValueError:
            return None
        if index < 0:
            index += len(items)
        return items[index] if 0 <= index < len(items) else None

    def active_count(self, chat_id: str, is_group: bool) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM schedules
                WHERE chat_id = ? AND is_group = ?
                  AND status IN ('active', 'paused', 'finishing')
                """,
                (chat_id, int(is_group)),
            ).fetchone()
        return int(row["count"] if row else 0)

    def list_due(self, now: int, limit: int = 20) -> list[Schedule]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.* FROM schedules s
                WHERE s.status = 'active' AND s.next_run_at IS NOT NULL
                  AND s.next_run_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM schedule_runs r
                      WHERE r.schedule_id = s.id AND r.state = 'running'
                  )
                ORDER BY s.next_run_at, s.created_at
                LIMIT ?
                """,
                (now, max(1, limit)),
            ).fetchall()
        return [item for row in rows if (item := self._schedule(row)) is not None]

    def next_due_at(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MIN(s.next_run_at) AS next_due FROM schedules s
                WHERE s.status = 'active' AND s.next_run_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM schedule_runs r
                      WHERE r.schedule_id = s.id AND r.state = 'running'
                  )
                """
            ).fetchone()
        return int(row["next_due"]) if row and row["next_due"] is not None else None

    def claim(
        self,
        schedule_id: str,
        *,
        expected_due_at: int,
        claimed_at: int,
        next_run_at: int | None,
    ) -> ScheduleRun | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE schedules
                SET next_run_at = ?, run_count = run_count + 1,
                    status = CASE WHEN ? IS NULL THEN 'finishing' ELSE 'active' END,
                    last_run_at = ?, updated_at = ?
                WHERE id = ? AND status = 'active' AND next_run_at = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM schedule_runs
                      WHERE schedule_id = ? AND state = 'running'
                  )
                """,
                (
                    next_run_at,
                    next_run_at,
                    claimed_at,
                    claimed_at,
                    schedule_id,
                    expected_due_at,
                    schedule_id,
                ),
            )
            if updated.rowcount != 1:
                return None
            cursor = conn.execute(
                """
                INSERT INTO schedule_runs(schedule_id, due_at, started_at, state)
                VALUES (?, ?, ?, 'running')
                """,
                (schedule_id, expected_due_at, claimed_at),
            )
            run_id = int(cursor.lastrowid)
        return ScheduleRun(run_id, schedule_id, expected_due_at, claimed_at)

    def claim_manual(self, schedule_id: str, *, claimed_at: int) -> ScheduleRun | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE schedules
                SET run_count = run_count + 1, last_run_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('active', 'paused')
                  AND NOT EXISTS (
                      SELECT 1 FROM schedule_runs
                      WHERE schedule_id = ? AND state = 'running'
                  )
                """,
                (claimed_at, claimed_at, schedule_id, schedule_id),
            )
            if updated.rowcount != 1:
                return None
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO schedule_runs(schedule_id, due_at, started_at, state, manual)
                    VALUES (?, ?, ?, 'running', 1)
                    """,
                    (schedule_id, claimed_at, claimed_at),
                )
            except sqlite3.IntegrityError:
                raise RuntimeError("同一秒内不能重复手动执行同一个定时任务") from None
            run_id = int(cursor.lastrowid)
        return ScheduleRun(run_id, schedule_id, claimed_at, claimed_at, manual=True)

    def finish_run(
        self,
        run_id: int,
        state: str,
        *,
        finished_at: int,
        error: str = "",
        max_consecutive_failures: int = 0,
        max_run_history: int = 0,
        job_id: str | None = None,
    ) -> None:
        failed = state in {"failed", "interrupted"}
        succeeded = state == "succeeded"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT schedule_id FROM schedule_runs WHERE id = ? AND state = 'running'",
                (run_id,),
            ).fetchone()
            if not row:
                return
            schedule_id = str(row["schedule_id"])
            conn.execute(
                """
                UPDATE schedule_runs
                SET state = ?, finished_at = ?, error = ?, job_id = ?
                WHERE id = ?
                """,
                (state, finished_at, error, job_id, run_id),
            )
            conn.execute(
                """
                UPDATE schedules
                SET success_count = success_count + ?,
                    failure_count = failure_count + ?,
                    consecutive_failures = CASE
                        WHEN ? THEN 0
                        WHEN ? THEN consecutive_failures + 1
                        ELSE consecutive_failures
                    END,
                    last_error = ?, updated_at = ?,
                    status = CASE WHEN status = 'finishing' THEN 'completed' ELSE status END
                WHERE id = ?
                """,
                (int(succeeded), int(failed), succeeded, failed, error, finished_at, schedule_id),
            )
            if max_consecutive_failures > 0:
                conn.execute(
                    """
                    UPDATE schedules SET status = 'paused', updated_at = ?
                    WHERE id = ? AND status = 'active'
                      AND consecutive_failures >= ?
                    """,
                    (finished_at, schedule_id, max_consecutive_failures),
                )
            if max_run_history > 0:
                conn.execute(
                    """
                    DELETE FROM schedule_runs
                    WHERE schedule_id = ? AND id NOT IN (
                        SELECT id FROM schedule_runs
                        WHERE schedule_id = ? ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (schedule_id, schedule_id, max_run_history),
                )

    def advance_missed(
        self,
        schedule_id: str,
        *,
        expected_due_at: int,
        next_run_at: int | None,
        missed: int,
        now: int,
    ) -> bool:
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE schedules
                SET next_run_at = ?, missed_count = missed_count + ?, updated_at = ?,
                    status = CASE WHEN ? IS NULL THEN 'completed' ELSE status END
                WHERE id = ? AND status = 'active' AND next_run_at = ?
                """,
                (next_run_at, max(0, missed), now, next_run_at, schedule_id, expected_due_at),
            )
        return updated.rowcount == 1

    def recover_interrupted(self, now: int) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, schedule_id FROM schedule_runs WHERE state = 'running'"
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE schedule_runs SET state = 'interrupted', finished_at = ?,
                        error = 'bridge restarted'
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                conn.execute(
                    """
                    UPDATE schedules
                    SET failure_count = failure_count + 1,
                        consecutive_failures = consecutive_failures + 1,
                        last_error = 'bridge restarted', updated_at = ?,
                        status = CASE WHEN status = 'finishing' THEN 'completed' ELSE status END
                    WHERE id = ?
                    """,
                    (now, row["schedule_id"]),
                )
        return len(rows)

    def list_runs(self, schedule_id: str, limit: int = 20) -> list[ScheduleRun]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM schedule_runs WHERE schedule_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (schedule_id, max(1, limit)),
            ).fetchall()
        return [self._run(row) for row in rows]

    def set_status(
        self,
        schedule_id: str,
        status: str,
        *,
        now: int,
        from_statuses: tuple[str, ...] | None = None,
    ) -> bool:
        query = "UPDATE schedules SET status = ?, updated_at = ? WHERE id = ?"
        params: list[object] = [status, now, schedule_id]
        if from_statuses:
            placeholders = ",".join("?" for _item in from_statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(from_statuses)
        with self._connect() as conn:
            updated = conn.execute(query, params)
        return updated.rowcount == 1

    def _params(self, schedule: Schedule) -> dict[str, object]:
        data = dict(schedule.__dict__)
        data["is_group"] = int(schedule.is_group)
        data["mentions_json"] = json.dumps(schedule.mentions, ensure_ascii=False)
        data.pop("mentions")
        return data

    def _schedule(self, row: sqlite3.Row | None) -> Schedule | None:
        if row is None:
            return None
        columns = set(row.keys())
        return Schedule(
            id=str(row["id"]),
            chat_id=str(row["chat_id"]),
            is_group=bool(row["is_group"]),
            creator_id=str(row["creator_id"]),
            source_message_id=str(row["source_message_id"]),
            kind=str(row["kind"]),
            action=str(row["action"]),
            payload=str(row["payload"]),
            mentions=tuple(str(item) for item in json.loads(row["mentions_json"])),
            timezone=str(row["timezone"]),
            start_at=int(row["start_at"]),
            next_run_at=int(row["next_run_at"]) if row["next_run_at"] is not None else None,
            rrule=str(row["rrule"]) if "rrule" in columns and row["rrule"] else None,
            description=(
                str(row["description"])
                if "description" in columns and row["description"] is not None
                else ""
            ),
            status=str(row["status"]),
            run_count=int(row["run_count"]),
            success_count=int(row["success_count"]),
            failure_count=int(row["failure_count"]),
            missed_count=int(row["missed_count"]),
            consecutive_failures=int(row["consecutive_failures"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            last_run_at=int(row["last_run_at"]) if row["last_run_at"] is not None else None,
            last_error=str(row["last_error"]),
        )

    def _run(self, row: sqlite3.Row) -> ScheduleRun:
        return ScheduleRun(
            id=int(row["id"]),
            schedule_id=str(row["schedule_id"]),
            due_at=int(row["due_at"]),
            started_at=int(row["started_at"]),
            finished_at=int(row["finished_at"]) if row["finished_at"] is not None else None,
            state=str(row["state"]),
            job_id=str(row["job_id"]) if row["job_id"] is not None else None,
            error=str(row["error"]),
            manual=bool(row["manual"]),
        )
