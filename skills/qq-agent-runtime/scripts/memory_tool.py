#!/usr/bin/env python3
"""Search a scoped memory snapshot and append constrained work-note proposals."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from typing import Any


MANIFEST_KEYS = {
    "version", "token", "job_id", "schedule_id", "snapshot", "proposal_dir",
    "max_results", "max_proposals", "max_note_chars",
}


class ToolError(ValueError):
    pass


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ToolError("invalid-manifest")
        result[key] = value
    return result


def load_manifest(path: Path) -> tuple[dict[str, Any], Path, Path]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ToolError) as exc:
        raise ToolError("invalid-manifest") from exc
    if not isinstance(payload, dict) or set(payload) != MANIFEST_KEYS or payload.get("version") != 1:
        raise ToolError("invalid-manifest")
    if not isinstance(payload.get("token"), str) or not isinstance(payload.get("job_id"), str):
        raise ToolError("invalid-manifest")
    snapshot_value = Path(str(payload.get("snapshot", "")))
    proposal_value = Path(str(payload.get("proposal_dir", "")))
    if (
        snapshot_value.is_absolute() or proposal_value.is_absolute()
        or ".." in snapshot_value.parts or ".." in proposal_value.parts
    ):
        raise ToolError("invalid-manifest")
    return payload, path.parent / snapshot_value, path.parent / proposal_value


def connect_snapshot(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise ToolError("snapshot-unavailable") from exc


def item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "subject_kind": row["subject_kind"],
        "subject_id": row["subject_id"], "category": row["category"],
        "content": row["content"], "updated_at": row["updated_at"],
        "source_kind": row["source_kind"], "version": row["version"],
    }


def search(conn: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    normalized = " ".join(query.split())
    if not normalized:
        return []
    rows: list[sqlite3.Row] = []
    escaped = '"' + normalized.replace('"', '""') + '"'
    try:
        rows = conn.execute(
            "SELECT m.* FROM memories m JOIN memory_fts f ON f.id=m.id "
            "WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts), m.updated_at DESC LIMIT ?",
            (escaped, limit),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    if not rows:
        rows = conn.execute(
            "SELECT * FROM memories WHERE instr(lower(content), lower(?)) > 0 "
            "ORDER BY effective_score DESC, updated_at DESC, id LIMIT ?",
            (normalized, limit),
        ).fetchall()
    return [item(row) for row in rows]


def known_revision(conn: sqlite3.Connection, item_id: str, version: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM memories WHERE id=? AND version=? AND source_kind='agent_work'",
        (item_id, version),
    ).fetchone() is not None


def append_proposal(
    manifest: dict[str, Any], proposal_dir: Path, *, operation: str,
    content: str, item_id: str | None, expected_version: int | None,
) -> None:
    text = " ".join(content.split())
    if not text or len(text) > int(manifest["max_note_chars"]):
        raise ToolError("invalid-content")
    try:
        count = sum(1 for value in proposal_dir.iterdir() if value.is_file())
    except OSError as exc:
        raise ToolError("proposal-unavailable") from exc
    if count >= int(manifest["max_proposals"]):
        raise ToolError("proposal-limit")
    payload = {
        "token": manifest["token"], "job_id": manifest["job_id"],
        "operation": operation, "content": text, "item_id": item_id,
        "expected_version": expected_version,
    }
    for _ in range(8):
        path = proposal_dir / f"{secrets.token_hex(12)}.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        try:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    raise ToolError("proposal-unavailable")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--manifest", required=True)
    commands = result.add_subparsers(dest="command", required=True)
    search_parser = commands.add_parser("search")
    search_parser.add_argument("--query", required=True)
    commands.add_parser("recent")
    read_parser = commands.add_parser("read")
    read_parser.add_argument("--id", required=True)
    add_parser = commands.add_parser("propose-add")
    add_parser.add_argument("--text", required=True)
    revise_parser = commands.add_parser("propose-revise")
    revise_parser.add_argument("--id", required=True)
    revise_parser.add_argument("--version", required=True, type=int)
    revise_parser.add_argument("--text", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        manifest, snapshot, proposal_dir = load_manifest(Path(args.manifest))
        conn = connect_snapshot(snapshot)
        try:
            limit = max(1, int(manifest["max_results"]))
            if args.command == "search":
                results = search(conn, args.query, limit)
                output = {"ok": True, "results": results, "count": len(results)}
            elif args.command == "recent":
                rows = conn.execute(
                    "SELECT * FROM memories ORDER BY updated_at DESC, id LIMIT ?", (limit,)
                ).fetchall()
                results = [item(row) for row in rows]
                output = {"ok": True, "results": results, "count": len(results)}
            elif args.command == "read":
                row = conn.execute("SELECT * FROM memories WHERE id=?", (args.id,)).fetchone()
                if row is None:
                    raise ToolError("not-found")
                output = {"ok": True, "result": item(row), "count": 1}
            elif args.command == "propose-add":
                append_proposal(
                    manifest, proposal_dir, operation="add", content=args.text,
                    item_id=None, expected_version=None,
                )
                output = {"ok": True, "count": 1}
            else:
                if not known_revision(conn, args.id, args.version):
                    raise ToolError("not-found")
                append_proposal(
                    manifest, proposal_dir, operation="revise", content=args.text,
                    item_id=args.id, expected_version=args.version,
                )
                output = {"ok": True, "count": 1}
        finally:
            conn.close()
    except (ToolError, OSError, sqlite3.Error, ValueError) as exc:
        error = str(exc) if isinstance(exc, ToolError) else "invalid-request"
        print(json.dumps({"ok": False, "error": error}, separators=(",", ":")))
        return 1
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
