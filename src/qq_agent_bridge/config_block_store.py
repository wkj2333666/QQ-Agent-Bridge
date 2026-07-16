"""Update one top-level YAML block while preserving unrelated config text."""
from __future__ import annotations

import os
import fcntl
from pathlib import Path
import stat
import tempfile


def write_top_level_block(path: Path, key: str, block: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # Read after locking so concurrent block updates do not use stale snapshots.
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            normalized = block.rstrip()
            if not text.strip():
                _write_text_atomic(path, normalized + "\n")
                return
            updated, found = _replace_top_level_block(text, key, normalized)
            if not found:
                updated = _append_top_level_block(text, normalized)
            _write_text_atomic(path, updated)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _replace_top_level_block(text: str, key: str, block: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    start: int | None = None
    end = len(lines)
    prefix = f"{key}:"
    for idx, line in enumerate(lines):
        if line.startswith(prefix):
            start = idx
            break
    if start is None:
        return text, False
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        if (
            line.strip()
            and not line.startswith((" ", "\t"))
            and not line.lstrip().startswith("#")
        ):
            end = idx
            break
    replacement = [item + "\n" for item in block.splitlines()]
    preserved_comments = [line for line in lines[start + 1 : end] if line.startswith("#")]
    return "".join(lines[:start] + replacement + preserved_comments + lines[end:]), True


def _append_top_level_block(text: str, block: str) -> str:
    marker = "\n# OneBot"
    if marker in text:
        before, after = text.split(marker, 1)
        return before.rstrip() + "\n\n" + block + "\n" + marker + after
    return text.rstrip() + "\n\n" + block + "\n"


def _write_text_atomic(path: Path, text: str) -> None:
    parent = path.parent
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(temp_path, existing_mode)
        os.replace(temp_path, path)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)
