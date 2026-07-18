"""Policy engine, command parsing, authorization, job lifecycle."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .config import BridgeConfig, LEGACY_OWNER_COMMANDS
from .redactor import redact, strip_ansi
from .types import ChatEvent, CommandName, ParsedCommand

logger = logging.getLogger(__name__)

COMMANDS: set[str] = {
    "ask",
    "plan",
    "search",
    "task",
    "code",
    "status",
    "stop",
    "approve",
    "shell",
    "help",
    "permission",
    "profile",
    "mode",
    "reset",
    "reload",
    "schedule",
}
READ_ONLY_COMMANDS: set[str] = {
    "ask",
    "plan",
    "search",
    "task",
    "status",
    "help",
    "profile",
    "mode",
    "schedule",
}
# Public compatibility alias for callers that imported the old static policy set.
OWNER_ONLY_COMMANDS: set[str] = set(LEGACY_OWNER_COMMANDS)


@dataclass
class Job:
    id: str
    cmd: str
    args: str
    event: ChatEvent
    started: float = field(default_factory=time.time)
    task: asyncio.Task[str] | None = field(default=None, repr=False)
    confirm_nonce: str | None = None
    state: str = "queued"
    result: str | None = field(default=None, repr=False)
    artifact_result: str | None = field(default=None, repr=False)
    artifact_delivery_outcome: str | None = field(default=None, repr=False)
    allow_outgoing_resources: bool = False
    outgoing_dir: str | None = field(default=None, repr=False)
    outgoing_dir_relative: str | None = field(default=None, repr=False)
    outgoing_token: str | None = field(default=None, repr=False)
    outgoing_dir_dev: int | None = field(default=None, repr=False)
    outgoing_dir_ino: int | None = field(default=None, repr=False)
    timeout_seconds: float | None = None
    source: str = "chat"
    schedule_id: str | None = None
    schedule_run_id: int | None = None
    scheduled_for: int | None = None
    reply_ats: tuple[str, ...] = ()


JobRunner = Callable[[Job], Awaitable[str]]


class Policy:
    def __init__(self, cfg: BridgeConfig, runner: JobRunner) -> None:
        self.cfg = cfg
        self.runner = runner
        self.jobs: dict[str, Job] = {}
        self.seen: dict[str, float] = {}  # dedupe message ids
        self._semaphore = asyncio.Semaphore(max(1, cfg.agent.max_concurrent_jobs))

    def parse(
        self,
        text: str,
        prefix: str = "/ask",
        default_command: CommandName | None = None,
    ) -> ParsedCommand | None:
        t = text.strip()
        if not t:
            if default_command is None or default_command not in READ_ONLY_COMMANDS:
                return None
            return ParsedCommand(name=default_command, args="", raw="")
        t = self._strip_leading_mentions(t)
        if not t:
            return None
        if not t.startswith("/"):
            if default_command is None:
                return None
            if default_command not in READ_ONLY_COMMANDS:
                return None
            return ParsedCommand(name=default_command, args=t, raw=t)
        parts = t[1:].split(maxsplit=1)
        if not parts:
            return None
        name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        if name not in COMMANDS:
            return None
        return ParsedCommand(name=name, args=args, raw=t)  # type: ignore[arg-type]

    def _strip_leading_mentions(self, text: str) -> str:
        t = text.strip()
        while t.startswith("@"):
            parts = t.split(maxsplit=1)
            if len(parts) < 2:
                return ""
            t = parts[1].strip()
        return t

    def allow(self, ev: ChatEvent, cmd: str) -> tuple[bool, str]:
        if ev.id in self.seen:
            return False, "duplicate"
        if ev.is_group:
            if not self.cfg.is_group_allowed(ev.chat_id):
                return False, "group-denied"
            if not ev.mentioned_bot:
                return False, "no-mention"
        elif not self.cfg.is_user_allowed(ev.sender_id):
            return False, "user-denied"
        group_id = ev.chat_id if ev.is_group else None
        access = (
            self.cfg.command_access(cmd, group_id)
            if group_id is not None
            else self.cfg.command_access(cmd)
        )
        if access == "disabled":
            return False, "cmd-disabled"
        if access == "owner" and not self.cfg.is_owner(ev.sender_id):
            return False, "owner-only"
        if cmd in ("code", "shell") and not self.cfg.is_workspace_allowed(self.cfg.agent.default_workspace):
            return False, "ws-denied"
        self.seen[ev.id] = time.time()
        return True, "ok"

    def start_job(self, ev: ChatEvent, cmd: ParsedCommand) -> tuple[str, str | None]:
        jid = f"j{int(time.time()*1000)}-{secrets.token_hex(3)}"
        job = Job(id=jid, cmd=cmd.name, args=cmd.args, event=ev)
        if self.cfg.dangerous_requires_confirm and cmd.name in ("code", "shell"):
            job.confirm_nonce = secrets.token_hex(4)
            job.state = "waiting_approval"
            self.jobs[jid] = job
            return jid, job.confirm_nonce
        self.jobs[jid] = job
        return jid, None

    def start_job_task(self, job: Job) -> None:
        if job.task is not None:
            return
        self._start_job_task(job)

    def _start_job_task(self, job: Job) -> None:
        job.state = "queued"
        job.task = asyncio.create_task(self._run(job))

    async def _run(self, job: Job) -> str:
        try:
            async with self._semaphore:
                job.state = "running"
                result = await asyncio.wait_for(
                    self.runner(job),
                    timeout=(
                        job.timeout_seconds
                        if job.timeout_seconds is not None
                        else self.cfg.effective_max_runtime()
                    ),
                )
            job.state = "done"
            job.artifact_result = result if job.allow_outgoing_resources else None
            job.result = redact(
                strip_ansi(result),
                extra=(
                    job.outgoing_token or "",
                    job.outgoing_dir or "",
                    job.outgoing_dir_relative or "",
                ),
            )[: self.cfg.effective_max_chars()]
            return job.result
        except asyncio.CancelledError:
            job.state = "cancelled"
            job.artifact_result = None
            job.result = "[cancelled]"
            raise
        except asyncio.TimeoutError:
            job.state = "done"
            job.artifact_result = None
            job.result = "[timeout]"
            return job.result
        except Exception as e:  # noqa: BLE001
            logger.error("job failed job=%s error=%s", job.id, type(e).__name__)
            job.state = "done"
            job.artifact_result = None
            job.result = f"[error] {type(e).__name__}"
            return job.result

    async def approve(self, jid: str, nonce: str, uid: str) -> str | None:
        job = self.jobs.get(jid)
        if not job or not job.confirm_nonce:
            return None
        if not self.cfg.is_owner(uid):
            return None
        if nonce != job.confirm_nonce:
            return None
        job.confirm_nonce = None
        job.state = "queued"
        return jid

    def cancel(self, jid: str, uid: str) -> bool:
        ok, _jid, _job, _reason = self.cancel_by_ref(jid, uid, default_ref="")
        return ok

    def cancel_by_ref(
        self,
        ref: str | None,
        uid: str,
        *,
        default_ref: str = "-1",
    ) -> tuple[bool, str | None, Job | None, str]:
        if not self.cfg.is_owner(uid):
            return False, None, None, "owner-only"
        jid, job = self.resolve_job_ref(ref, default_ref=default_ref, active_only=True)
        if not job:
            return False, None, None, "unknown job"
        if job.task and not job.task.done():
            job.task.cancel()
            job.state = "cancelled"
            job.result = "[cancelled]"
            return True, jid, job, "ok"
        if job.state in {"queued", "running", "waiting_approval"}:
            job.state = "cancelled"
            job.result = "[cancelled]"
            return True, jid, job, "ok"
        return False, jid, job, "not-running"

    def reload_config(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        active = any(job.state in {"queued", "running"} for job in self.jobs.values())
        if not active:
            self._semaphore = asyncio.Semaphore(max(1, cfg.agent.max_concurrent_jobs))

    def resolve_job_ref(
        self,
        ref: str | None,
        *,
        default_ref: str | None = None,
        active_only: bool = False,
    ) -> tuple[str | None, Job | None]:
        raw = (ref or "").strip()
        if not raw and default_ref is not None:
            raw = default_ref
        if not raw:
            return None, None
        items = self._active_job_items() if active_only else list(self.jobs.items())
        job_map = dict(items)
        if raw in job_map:
            return raw, job_map[raw]

        prefix_matches = [(jid, job) for jid, job in items if jid.startswith(raw)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]

        try:
            idx = int(raw)
        except ValueError:
            return None, None
        if idx < 0:
            idx = len(items) + idx
        if idx < 0 or idx >= len(items):
            return None, None
        return items[idx]

    def get_status(self, job_ref: str | None = None) -> str:
        ref = (job_ref or "").strip()
        if ref:
            jid, job = self.resolve_job_ref(ref, active_only=True)
            if not job or not jid:
                return f"unknown job: {ref}"
            return self.format_job_line(jid, job)

        items = self._active_job_items()
        if not items:
            return "jobs: none"
        running = sum(1 for _jid, job in items if job.state == "running")
        queued = sum(1 for _jid, job in items if job.state == "queued")
        waiting = sum(1 for _jid, job in items if job.state == "waiting_approval")
        lines = [f"jobs: running:{running} queued:{queued} waiting_approval:{waiting}"]
        lines.extend(self.format_job_line(jid, job) for jid, job in items)
        return "\n".join(lines)

    def format_job_line(self, jid: str, job: Job) -> str:
        idx = self._job_index(jid, active_only=True)
        index = "?" if idx is None else str(idx)
        origin = f" schedule:{job.schedule_id}" if job.schedule_id else ""
        return (
            f"{index}. {jid} {job.cmd} {job.state} "
            f"by {job.event.sender_id}{origin}: {self._job_summary(job)}"
        )

    def _job_index(self, jid: str, *, active_only: bool = False) -> int | None:
        items = self._active_job_items() if active_only else list(self.jobs.items())
        for idx, (existing, _job) in enumerate(items):
            if existing == jid:
                return idx
        return None

    def _active_job_items(self) -> list[tuple[str, Job]]:
        return [
            (jid, job)
            for jid, job in self.jobs.items()
            if job.state in {"queued", "running", "waiting_approval"}
        ]

    def _job_summary(self, job: Job) -> str:
        summary = " ".join((job.args or job.event.text or "").split())
        if not summary:
            return "(empty)"
        if len(summary) <= 60:
            return summary
        return summary[:57].rstrip() + "..."

    async def cleanup(self) -> None:
        for j in list(self.jobs.values()):
            if j.task and j.task.done():
                try:
                    await j.task
                except BaseException:
                    pass
        prunable = [
            (jid, job)
            for jid, job in self.jobs.items()
            if (
                job.state == "waiting_approval"
                or (job.state in {"done", "cancelled"} and (job.task is None or job.task.done()))
            )
        ]
        overflow = len(prunable) - max(0, self.cfg.max_finished_jobs)
        for jid, _job in prunable[: max(0, overflow)]:
            self.jobs.pop(jid, None)
        seen_overflow = len(self.seen) - max(0, self.cfg.max_seen_messages)
        for msg_id in list(self.seen)[: max(0, seen_overflow)]:
            self.seen.pop(msg_id, None)
