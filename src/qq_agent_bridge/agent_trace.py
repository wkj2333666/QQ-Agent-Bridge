"""Bounded, redacted JSONL traces for Agent subprocesses."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .redactor import redact, strip_ansi

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_INPUT_MARKERS = ("user", "system", "input", "prompt", "request")
_OPTIONAL_TEXT_FIELDS = ("summary", "description", "tool", "subtype")


class AgentTrace:
    """Write a private, bounded trace without affecting the Agent invocation."""

    def __init__(
        self,
        cfg: BridgeConfig,
        trace_id: str | None,
        mode: str,
        model: str | None,
        workspace: str,
        redact_extra: tuple[str, ...] | None = None,
    ) -> None:
        self._fh: Any | None = None
        self._started = time.monotonic()
        self._bytes_written = 0
        self._dropped_events = 0
        self._truncation_written = False
        self._redact_extra = tuple(redact_extra or ())
        self.path: Path | None = None
        self._max_bytes = max(1, self._as_int(cfg.agent.trace_max_bytes, 5 * 1024 * 1024))
        self._max_line_chars = max(1, self._as_int(cfg.agent.trace_max_line_chars, 2000))
        self.job_id = self._safe_id(trace_id)
        if not cfg.agent.trace_enabled:
            return

        try:
            root = self._trace_root(cfg.agent.trace_root, workspace)
            root = self._prepare_root(root)
            self._fh = self._open_trace(root)
            self.record(
                "lifecycle",
                "start",
                mode=mode,
                model=model or "",
                workspace=Path(workspace).name,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must never break the job
            logger.warning("agent trace unavailable: %s", type(exc).__name__)
            self._close_handle()

    @property
    def enabled(self) -> bool:
        return self._fh is not None

    def elapsed_ms(self) -> int:
        return max(0, int((time.monotonic() - self._started) * 1000))

    def record(self, stream: str, event: str, *, subtype: str | None = None, **fields: Any) -> None:
        if not self.enabled:
            return
        record: dict[str, Any] = {
            "schema_version": 1,
            "time": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "job_id": self.job_id,
            "stream": self._safe_text(stream, 48),
            "event": self._safe_text(event, 80),
            "elapsed_ms": self.elapsed_ms(),
        }
        if subtype:
            record["subtype"] = self._safe_text(subtype, 120)
        for key, value in fields.items():
            record[key] = self._safe_value(value)
        self._append(record)

    def record_stdout_line(self, line: str) -> None:
        """Record one stdout line while omitting input/prompt payloads."""
        if not self.enabled:
            return
        stripped = line.strip()
        if not stripped:
            return
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            self.record("stdout", "text", summary=line)
            return
        if not isinstance(payload, dict):
            self.record("stdout", "json", summary=type(payload).__name__)
            return

        markers = " ".join(
            str(payload.get(key, "")).lower() for key in ("type", "event", "role", "name")
        )
        if any(marker in markers for marker in _INPUT_MARKERS):
            self.record("stdout", "input", summary="[omitted input]")
            return

        event_type = str(payload.get("type") or payload.get("event") or "json").lower()
        subtype = str(payload.get("subtype") or "")
        if "tool_call" in markers:
            tool, description = self._tool_summary(payload)
            self.record(
                "stdout",
                "tool_call",
                subtype=subtype or None,
                tool=tool,
                description=description,
            )
            return

        text = self._extract_text(payload)
        if any(marker in markers for marker in ("assistant", "output", "content_block_delta", "text_delta")):
            self.record("stdout", "assistant_message", subtype=subtype or event_type, summary=text)
            return

        self.record(
            "stdout",
            self._safe_text(event_type, 80),
            subtype=subtype or None,
            summary=self._event_summary(payload),
        )

    def record_stdout_text(self, text: str) -> None:
        for line in text.splitlines():
            self.record_stdout_line(line)

    def record_stderr_text(self, text: str) -> None:
        for line in text.splitlines():
            if line.strip():
                self.record("stderr", "text", summary=line)

    def close(self) -> None:
        if not self.enabled:
            return
        if self._dropped_events and not self._truncation_written:
            self._write_truncation_marker()
        self._close_handle()

    def _append(self, record: dict[str, Any]) -> None:
        if self._truncation_written:
            return
        line = self._serialize(record)
        if not line:
            self._drop_event()
            return
        data = (line + "\n").encode("utf-8")
        if record.get("event") != "truncated" and self._dropped_events == 0:
            reserve = self._truncation_reserve()
            if self._bytes_written + len(data) + reserve > self._max_bytes:
                self._drop_event()
                self._write_truncation_marker()
                return
        if self._bytes_written + len(data) > self._max_bytes:
            self._drop_event()
            self._write_truncation_marker()
            return
        try:
            self._fh.write(data)
            self._fh.flush()
            self._bytes_written += len(data)
        except Exception as exc:  # noqa: BLE001 - trace failure is non-fatal
            logger.warning("agent trace write failed: %s", type(exc).__name__)
            self._close_handle()

    def _drop_event(self) -> None:
        self._dropped_events += 1

    def _write_truncation_marker(self) -> None:
        if self._truncation_written or not self.enabled:
            return
        marker = {
            "schema_version": 1,
            "time": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "job_id": self.job_id,
            "stream": "trace",
            "event": "truncated",
            "elapsed_ms": self.elapsed_ms(),
            "dropped_events": self._dropped_events,
        }
        line = self._serialize(marker)
        if not line:
            return
        data = (line + "\n").encode("utf-8")
        if self._bytes_written + len(data) > self._max_bytes:
            return
        try:
            self._fh.write(data)
            self._fh.flush()
            self._bytes_written += len(data)
            self._truncation_written = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent trace truncation marker failed: %s", type(exc).__name__)
            self._close_handle()

    def _truncation_reserve(self) -> int:
        marker = {
            "schema_version": 1,
            "time": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "job_id": self.job_id,
            "stream": "trace",
            "event": "truncated",
            "elapsed_ms": self.elapsed_ms(),
            "dropped_events": 1,
        }
        line = self._serialize(marker)
        return len(line.encode("utf-8")) + 1 if line else 0

    def _serialize(self, record: dict[str, Any]) -> str:
        normalized = {key: self._safe_value(value) for key, value in record.items()}
        line = self._dump(normalized)
        if self._fits(line):
            return line

        for key in _OPTIONAL_TEXT_FIELDS:
            value = normalized.get(key)
            if isinstance(value, str) and len(value) > 8:
                normalized[key] = value[: max(1, len(value) // 2)]
                line = self._dump(normalized)
                if self._fits(line):
                    return line

        for key in ("summary", "description", "tool", "subtype", "elapsed_ms"):
            normalized.pop(key, None)
            line = self._dump(normalized)
            if self._fits(line):
                return line

        # Extremely small configured limits cannot preserve every optional field.
        compact = {
            key: normalized[key]
            for key in ("schema_version", "time", "job_id", "stream", "event")
            if key in normalized
        }
        line = self._dump(compact)
        return line if self._fits(line) else ""

    def _fits(self, line: str) -> bool:
        return len(line) <= self._max_line_chars and len(line.encode("utf-8")) + 1 <= self._max_bytes

    def _dump(self, value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def _tool_summary(self, payload: dict[str, Any]) -> tuple[str, str]:
        tool = payload.get("tool") or payload.get("name") or ""
        description = ""

        def visit(value: Any) -> None:
            nonlocal tool, description
            if isinstance(value, dict):
                if not tool and isinstance(value.get("name"), str):
                    tool = value["name"]
                if not description and isinstance(value.get("description"), str):
                    description = value["description"]
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload.get("tool_call", payload))
        return self._safe_text(tool, 120), self._safe_text(description, 400)

    def _extract_text(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(self._extract_text(item) for item in value)
        if not isinstance(value, dict):
            return ""
        return "".join(
            self._extract_text(value[key])
            for key in ("text", "delta", "content", "message", "output")
            if key in value
        )

    def _event_summary(self, payload: dict[str, Any]) -> str:
        keys = sorted(str(key) for key in payload if str(key) not in {"type", "event"})
        return "keys=" + ",".join(keys[:12])

    def _safe_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._safe_text(value, 1000)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, (list, tuple)):
            return [self._safe_value(item) for item in value[:12]]
        if isinstance(value, dict):
            return {str(key)[:80]: self._safe_value(item) for key, item in list(value.items())[:12]}
        return f"<{type(value).__name__}>"

    def _safe_text(self, value: Any, limit: int) -> str:
        return redact(strip_ansi(str(value)), extra=self._redact_extra)[:limit]

    def _trace_root(self, configured: str, workspace: str) -> Path:
        configured_path = Path(configured or "runtime/agent-traces").expanduser()
        root = configured_path if configured_path.is_absolute() else Path.cwd() / configured_path
        root = Path(os.path.abspath(root))
        resolved_root = root.resolve(strict=False)
        resolved_workspace = Path(workspace).expanduser().resolve(strict=False)
        if resolved_root == resolved_workspace or self._is_relative_to(resolved_root, resolved_workspace):
            raise ValueError("trace root must be outside workspace")
        return root

    def _prepare_root(self, root: Path) -> Path:
        if root.exists() and root.is_symlink():
            raise ValueError("trace root cannot be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not root.is_dir():
            raise ValueError("trace root is not a directory")
        root.chmod(0o700)
        return root

    def _open_trace(self, root: Path) -> Any:
        base = root / f"{self.job_id}.jsonl"
        for index in range(100):
            path = base if index == 0 else root / f"{self.job_id}-{index + 1}.jsonl"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            handle = os.fdopen(fd, "ab", buffering=0)
            path.chmod(0o600)
            self.path = path
            return handle
        raise OSError("could not allocate a trace filename")

    def _safe_id(self, value: str | None) -> str:
        raw = str(value or "").strip()
        safe = _SAFE_ID_RE.sub("_", raw).strip("._")[:120]
        return safe or f"run-{uuid.uuid4().hex[:12]}"

    def _as_int(self, value: Any, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    def _is_relative_to(self, path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
        except ValueError:
            return False
        return True

    def _close_handle(self) -> None:
        handle, self._fh = self._fh, None
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
