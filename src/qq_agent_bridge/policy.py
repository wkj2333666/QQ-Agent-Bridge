"""Policy engine, command parsing, authorization, job lifecycle."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .config import BridgeConfig
from .redactor import redact
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
    "profile",
    "reset",
    "reload",
}
READ_ONLY_COMMANDS: set[str] = {"ask", "plan", "search", "task", "status", "help", "profile"}
OWNER_ONLY_COMMANDS: set[str] = COMMANDS - READ_ONLY_COMMANDS


@dataclass
class Job:
    id: str
    cmd: str
    args: str
    event: ChatEvent
    started: float = field(default_factory=time.time)
    task: asyncio.Task[str] | None = None
    confirm_nonce: str | None = None
    state: str = "queued"
    result: str | None = None
    allow_outgoing_resources: bool = False
    outgoing_dir: str | None = None
    outgoing_token: str | None = None
    outgoing_dir_dev: int | None = None
    outgoing_dir_ino: int | None = None


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
        if cmd in OWNER_ONLY_COMMANDS and not self.cfg.is_owner(ev.sender_id):
            return False, "owner-only"
        if not self.cfg.is_command_allowed(cmd):
            return False, "cmd-disabled"
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
                    timeout=self.cfg.effective_max_runtime(),
                )
            job.state = "done"
            job.result = redact(result)[: self.cfg.effective_max_chars()]
            return job.result
        except asyncio.CancelledError:
            job.state = "cancelled"
            job.result = "[cancelled]"
            raise
        except asyncio.TimeoutError:
            job.state = "done"
            job.result = "[timeout]"
            return job.result
        except Exception as e:  # noqa: BLE001
            logger.exception("job %s failed", job.id)
            job.state = "done"
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
        job = self.jobs.get(jid)
        if not job:
            return False
        if not self.cfg.is_owner(uid):
            return False
        if job.task and not job.task.done():
            job.task.cancel()
            return True
        if job.state == "waiting_approval":
            job.state = "cancelled"
            return True
        return False

    def reload_config(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        active = any(job.state in {"queued", "running"} for job in self.jobs.values())
        if not active:
            self._semaphore = asyncio.Semaphore(max(1, cfg.agent.max_concurrent_jobs))

    def get_status(self, jid: str | None = None) -> str:
        if jid:
            j = self.jobs.get(jid)
            if not j:
                return "unknown job"
            return f"{jid}: {j.cmd} {j.state}"
        running = [f"{k}:{v.cmd}" for k, v in self.jobs.items() if v.state == "running"]
        queued = [f"{k}:{v.cmd}" for k, v in self.jobs.items() if v.state == "queued"]
        waiting = [f"{k}:{v.cmd}" for k, v in self.jobs.items() if v.state == "waiting_approval"]
        return (
            "running: "
            + (", ".join(running) or "none")
            + "; queued: "
            + (", ".join(queued) or "none")
            + "; waiting_approval: "
            + (", ".join(waiting) or "none")
        )

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
