"""QQ progress directive parsing."""
from __future__ import annotations

PROGRESS_PREFIX = "QQBOT_PROGRESS:"


def split_progress_line(line: str) -> tuple[str | None, str | None]:
    if not line.startswith(PROGRESS_PREFIX):
        return line, None
    payload = line[len(PROGRESS_PREFIX) :].strip()
    if not payload:
        return None, None
    return None, payload


def strip_progress_directives(text: str) -> tuple[str, tuple[str, ...]]:
    clean_lines: list[str] = []
    progress: list[str] = []
    for line in text.splitlines():
        clean, payload = split_progress_line(line)
        if clean is not None:
            clean_lines.append(clean)
        if payload:
            progress.append(payload)
    return "\n".join(clean_lines).strip(), tuple(progress)


class ProgressLineBuffer:
    def __init__(self) -> None:
        self._pending = ""

    def feed(self, chunk: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        self._pending += chunk
        if "\n" not in self._pending:
            return (), ()
        parts = self._pending.split("\n")
        self._pending = parts.pop()
        return self._process_lines(parts)

    def finish(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not self._pending:
            return (), ()
        pending = self._pending
        self._pending = ""
        return self._process_lines([pending])

    def _process_lines(self, lines: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        clean_lines: list[str] = []
        progress: list[str] = []
        for line in lines:
            clean, payload = split_progress_line(line)
            if clean is not None and clean:
                clean_lines.append(clean)
            if payload:
                progress.append(payload)
        return tuple(clean_lines), tuple(progress)
