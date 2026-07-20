"""Main entry: glue OneBot, policy, and agent runtime."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import logging
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .artifact_delivery import resolve_artifacts
from .attachment_cache import AttachmentCache
from .agent_runtime import build_agent_adapter, run_agent
from .config import COMMAND_ACCESS_LEVELS, MENTION_MODE_OPTIONS, MENTION_MODES, BridgeConfig
from .command_access_store import write_command_access_to_config
from .command_help import build_command_help
from .long_term_memory import LongTermMemoryRetriever, LongTermMemoryStore
from .long_term_memory_models import MemoryScope, exact_memory_scope
from .memory import ConversationMemory, GroupAmbientMemory
from .memory_commands import (
    MemoryCommandService,
    MemoryReviewRequest,
    build_memory_command_interpreter,
)
from .memory_curation import MemoryCollector
from .memory_review import (
    CuratorOutcome,
    MemoryReviewCoordinator,
    build_memory_review_coordinator,
)
from .mention_mode_store import write_mention_modes_to_config
from .onebot import OneBotAdapter
from .output_guard import guard_internal_output
from .outgoing_resources import OutgoingResource, inspect_outgoing_resources
from .policy import COMMANDS, Job, Policy
from .prompting import build_agent_prompt, select_profile_prompt
from .profile_store import write_profiles_to_config
from .proactive import MentionDecision, ProactiveSpeaker
from .progress import ProgressReporter
from .redactor import redact, strip_ansi
from .resources import ResourceManager, format_resource_context
from .runtime_skill import prepare_runtime_skill_bundle
from .schedule_parser import (
    NaturalLanguageScheduleParser,
    NaturalScheduleOutcome,
    ScheduleParseError,
    parse_explicit_schedule,
    rrule_is_unbounded,
    rrule_min_interval_seconds,
    rrule_occurrence_count,
)
from .schedule_store import ScheduleStore
from .scheduler import Schedule, ScheduleExecutionResult, ScheduleRun, Scheduler
from .self_knowledge import build_help_reply, build_prompt_self_knowledge, maybe_self_reply
from .storage_gate import GatedAgentAdapter, StorageActivityGate
from .storage_maintenance import StorageMaintainer
from .types import ChatEvent, ChatSegment, ParsedCommand, trusted_reply_sender_id
from .workspace_search import WorkspaceSearch
from .whisper_runner import WhisperRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("qq-bridge")
ARTIFACT_REPAIR_SHUTDOWN_GRACE_SECONDS = 1.0
MEMORY_REVIEW_SHUTDOWN_GRACE_SECONDS = 1.0
_ARTIFACT_PROGRESS_NOUN_RE = re.compile(
    r"(?:文件|资源|图片|图像|报告|附件|表格|语音|音频|任务输出|"
    r"\b(?:file|resource|image|report|attachment|document|pdf|audio|voice|output)\b)",
    re.IGNORECASE,
)
_ARTIFACT_DELIVERY_ACTION_RE = re.compile(
    r"(?:发送|发给|发你|发到|发往|上传|交付|附加|递交|传给|传到|"
    r"\b(?:send|sent|deliver(?:ed)?|upload(?:ed)?|attach(?:ed)?)\b)",
    re.IGNORECASE,
)
_ARTIFACT_COMPLETED_ACTION_ONLY_RE = re.compile(
    r"^\s*(?:(?:已经|已)\s*(?:发给你|发你|发送|上传|交付|附加)"
    r"(?:了|啦|完成|完毕|成功|好了)?|"
    r"(?:发给你|发你|发送|上传|交付|附加)(?:了|啦|完成|完毕|成功|好了)|"
    r"(?:successfully\s+)?(?:sent|delivered|uploaded|attached)"
    r"(?:\s+successfully)?[.!！。]?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_PRE_ACK_DELIVERY_PROGRESS = "正在验证并发送任务输出。"


def _claims_artifact_delivery(text: str) -> bool:
    return bool(
        (
            _ARTIFACT_PROGRESS_NOUN_RE.search(text)
            and _ARTIFACT_DELIVERY_ACTION_RE.search(text)
        )
        or _ARTIFACT_COMPLETED_ACTION_ONLY_RE.search(text)
    )


class App:
    def __init__(
        self,
        cfg: BridgeConfig,
        echo_only: bool = False,
        config_path: Path | str = "config.yaml",
    ) -> None:
        self.cfg = cfg
        self.echo_only = echo_only
        self.config_path = Path(config_path)
        self.adapter = OneBotAdapter(
            cfg.onebot.host,
            cfg.onebot.port,
            cfg.onebot.path,
            cfg.onebot.access_token,
            cfg.bot.self_id,
            cfg.bot.mention_name,
        )
        self.storage_gate = StorageActivityGate()
        self.agent = build_agent_adapter(cfg)
        self.cursor = self.agent  # compatibility alias for older tests/extensions
        self.gated_agent = GatedAgentAdapter(self.agent, self.storage_gate)
        self.storage_maintainer = StorageMaintainer(cfg, self.storage_gate)
        self.search = WorkspaceSearch(cfg)
        self.resources = self._build_resource_manager(cfg)
        self.memory = ConversationMemory(cfg.memory.max_messages, cfg.memory.max_chars)
        self.long_term_memory_store: LongTermMemoryStore | None = None
        self.long_term_memory_collector: MemoryCollector | None = None
        self.long_term_memory_retriever: LongTermMemoryRetriever | None = None
        self.memory_commands: MemoryCommandService | None = None
        self.memory_review_coordinator: MemoryReviewCoordinator | None = None
        self.long_term_memory_database_path: Path | None = None
        self.long_term_memory_error: str | None = None
        self._long_term_memory_accepting = False
        self._long_term_memory_protected_paths: tuple[Path, ...] = ()
        self._memory_review_tasks: set[asyncio.Task[None]] = set()
        self.ambient_memory = GroupAmbientMemory(
            max_messages=cfg.ambient_memory.max_messages,
            max_chars=cfg.ambient_memory.max_chars,
            max_message_chars=cfg.ambient_memory.max_message_chars,
            max_age_seconds=cfg.ambient_memory.max_age_seconds,
            min_chars=cfg.ambient_memory.min_chars,
            ignored_prefixes=tuple(cfg.ambient_memory.ignored_prefixes),
        )
        cache_max_items = min(cfg.resources.cache_max_items, cfg.resources.max_items)
        self.attachment_cache = AttachmentCache(
            ttl_seconds=cfg.resources.cache_ttl_seconds,
            max_items=cache_max_items,
        )
        self.policy: Policy | None = None
        self._reply_tasks: set[asyncio.Task[None]] = set()
        self._heartbeat_tasks: set[asyncio.Task[None]] = set()
        self._artifact_repair_tasks: set[asyncio.Task[str]] = set()
        self._outgoing_jobs: dict[str, Job] = {}
        self._protected_storage_paths: dict[str, tuple[Path, ...]] = {}
        self._progress_reporters: dict[str, ProgressReporter] = {}
        self._schedule_parse_outcomes: dict[str, NaturalScheduleOutcome] = {}
        self._schedule_parse_mentions: dict[str, tuple[str, ...]] = {}
        self._schedule_parse_safety_required: dict[str, bool] = {}
        self._schedule_parse_heartbeats: dict[str, asyncio.Task[None]] = {}
        self.schedule_database_path = self._schedule_database_path(cfg)
        self._scheduler_restart_required = False
        self.schedule_store = ScheduleStore(self.schedule_database_path)
        self.schedule_nl_parser = NaturalLanguageScheduleParser(cfg, self.gated_agent)
        self.scheduler = Scheduler(
            cfg.scheduler,
            self.schedule_store,
            self._execute_schedule,
            ready=lambda: bool(getattr(self.adapter, "is_connected", lambda: False)()),
        )
        self.proactive = ProactiveSpeaker(
            cfg,
            self.gated_agent,
            self._send_proactive,
            ambient_context=self._proactive_ambient_context,
            long_term_context=self._retrieve_long_term_context,
            remember=self._remember_proactive_exchange,
        )
        self._reload_lock = asyncio.Lock()

    async def _handle(self, ev: ChatEvent) -> None:
        if self._is_self_event(ev):
            return
        if ev.is_group and not ev.mentioned_bot:
            if not re.match(r"^\s*[/／]", ev.text):
                self._collect_long_term_event(ev)
            if self._should_cache_unmentioned_resources(ev):
                self.attachment_cache.remember(ev)
            if self._should_remember_ambient(ev):
                remembered = self.ambient_memory.remember(ev)
                if remembered and self.cfg.memory.enabled:
                    self.memory.append_user_message(ev)
            if not self.echo_only:
                self.proactive.observe(ev)
            return

        if self.echo_only:
            preview = redact(ev.text)[:120]
            await self._send_text(ev.chat_id, ev.is_group, f"[echo] {preview}", ev.id)
            return

        assert self.policy is not None
        if ev.is_group and ev.mentioned_bot and not ev.resources and self.cfg.resources.cache_enabled:
            cached_resources = self.attachment_cache.pop(ev.chat_id, ev.sender_id)
            if cached_resources:
                ev = replace(ev, resources=cached_resources)

        parsed = self.policy.parse(ev.text)
        if not parsed:
            parsed = self._parse_permission_command(ev.text)
        has_content = bool(ev.text.strip() or ev.resources)
        group_default_command = self.cfg.mention_mode_for_group(ev.chat_id) if ev.is_group else None
        mention_command = (
            "ask" if group_default_command == "chat" else group_default_command
        )
        default_command = "ask" if has_content and not ev.is_group else None
        preauthorized_command: str | None = None
        if not parsed and ev.is_group and ev.mentioned_bot and has_content:
            if ev.resources:
                default_command = mention_command
            else:
                assert group_default_command is not None
                assert mention_command is not None
                ok, reason = self.policy.allow(ev, mention_command)
                if not ok:
                    if reason not in {"duplicate", "no-mention"}:
                        await self._send_text(ev.chat_id, ev.is_group, f"[denied] {reason}", ev.id)
                    return
                if group_default_command == "chat":
                    decision = await self.proactive.decide_mention(ev)
                else:
                    decision = None
                if decision is None:
                    parsed = self.policy.parse(
                        ev.text, default_command=mention_command
                    ) or self.policy.parse(
                        "", default_command=mention_command
                    )
                    preauthorized_command = mention_command
                elif decision.action == "ask":
                    parsed = self.policy.parse(
                        ev.text, default_command=mention_command
                    ) or self.policy.parse(
                        "", default_command=mention_command
                    )
                    preauthorized_command = mention_command
                elif decision.action == "chat":
                    self._collect_long_term_event(ev)
                    if self.proactive.can_send_chat_interjection(ev.chat_id):
                        await self._send_mention_decision(ev, decision)
                        self.proactive.record_chat_interjection(ev.chat_id)
                    await self._cleanup_policy()
                    return
                else:
                    self._collect_long_term_event(ev)
                    await self._cleanup_policy()
                    return
        if not parsed and default_command:
            parsed = self.policy.parse(ev.text, default_command=default_command)
        if not parsed and ev.resources and default_command:
            parsed = self.policy.parse("", default_command=default_command)
        if not parsed:
            return

        if preauthorized_command != parsed.name:
            ok, reason = self.policy.allow(ev, parsed.name)
            if not ok:
                if reason not in {"duplicate", "no-mention"}:
                    await self._send_text(ev.chat_id, ev.is_group, f"[denied] {reason}", ev.id)
                await self._cleanup_policy()
                return

        if parsed.name != "help" and self._is_help_subcommand(parsed.args):
            txt = self._command_help_reply(parsed.name, ev)
            await self._send_text(ev.chat_id, ev.is_group, txt, ev.id)
            await self._cleanup_policy()
            return

        if parsed.name in {"ask", "plan", "task"}:
            self._collect_long_term_event(ev, parsed.name)

        if parsed.name == "permission":
            txt = self._handle_permission_command(ev, parsed.args)
            await self._send_text(ev.chat_id, ev.is_group, txt, ev.id)
            await self._cleanup_policy()
            return

        if parsed.name == "memory":
            review_request: MemoryReviewRequest | None = None
            if self.memory_commands is None:
                txt = (
                    "[disabled] 长期记忆功能已被全局关闭。"
                    if not self.cfg.long_term_memory.enabled
                    else "[error] 长期记忆数据库当前不可用。"
                )
            else:
                self._cancel_memory_review_for_interactive()
                if (
                    hasattr(self.memory_commands, "acknowledge")
                    and self.memory_commands.acknowledge is None
                ):
                    self.memory_commands.acknowledge = self._acknowledge_memory_command
                result = await self.memory_commands.handle(ev, parsed.args)
                txt = result.text
                review_request = result.review_request
            await self._send_text(ev.chat_id, ev.is_group, txt, ev.id)
            if review_request is not None:
                self._schedule_memory_review_delivery(ev, review_request)
            await self._cleanup_policy()
            return

        if self._missing_quoted_voice_resource(ev, parsed.name):
            await self._send_text(
                ev.chat_id,
                ev.is_group,
                "我看到了引用语音预览，但没有拿到可处理的语音文件。请直接把语音发给我，或让 QQ/NapCat 提供引用原消息。",
                ev.id,
            )
            await self._cleanup_policy()
            return

        if parsed.name == "help":
            topic = self._help_topic(parsed.args)
            txt = self._command_help_reply(topic, ev) if topic else build_help_reply(self.cfg, ev)
            await self._send_text(ev.chat_id, ev.is_group, txt, ev.id)
            await self._cleanup_policy()
            return

        if parsed.name == "ask":
            quick_reply = maybe_self_reply(parsed.args or ev.text, self.cfg, ev)
            if quick_reply:
                if self.cfg.memory.enabled:
                    self.memory.append_exchange(ev, parsed.args or ev.text, quick_reply)
                await self._send_text(ev.chat_id, ev.is_group, quick_reply, ev.id)
                await self._cleanup_policy()
                return

        if parsed.name == "status":
            ref = parsed.args.split()[0] if parsed.args else None
            st = self.policy.get_status(ref)
            await self._send_text(ev.chat_id, ev.is_group, st, ev.id)
            await self._cleanup_policy()
            return

        if parsed.name == "profile":
            profile_action, _profile_value = self._parse_profile_args(parsed.args)
            txt = self._handle_profile_command(ev, parsed.args)
            if (
                ev.is_group
                and profile_action in {"set", "clear"}
                and not txt.startswith("[denied]")
                and not txt.startswith("[error]")
            ):
                self.proactive.reset_chat(ev.chat_id)
            await self._send_text(ev.chat_id, ev.is_group, txt, ev.id)
            await self._cleanup_policy()
            return

        if parsed.name == "mode":
            txt = self._handle_mode_command(ev, parsed.args)
            await self._send_text(ev.chat_id, ev.is_group, txt, ev.id)
            await self._cleanup_policy()
            return

        if parsed.name == "schedule":
            await self._handle_schedule_command(ev, parsed.args)
            await self._cleanup_policy()
            return

        if parsed.name == "reset":
            if self.cfg.memory.enabled:
                self.memory.reset(ev)
            if self.cfg.ambient_memory.enabled and ev.is_group:
                self.ambient_memory.reset(ev)
            if ev.is_group:
                self.proactive.reset_chat(ev.chat_id)
            await self._send_text(
                ev.chat_id,
                ev.is_group,
                "已清空当前会话记忆和最近群聊背景；长期记忆不受影响。",
                ev.id,
            )
            await self._cleanup_policy()
            return

        if parsed.name == "reload":
            okc, msg = await self._reload_config()
            await self._send_text(ev.chat_id, ev.is_group, msg, ev.id)
            if okc:
                await self._cleanup_policy()
            return

        if parsed.name == "reboot":
            if not shutil.which("systemctl"):
                await self._send_text(
                    ev.chat_id, ev.is_group,
                    "[error] systemctl 不可用，无法重启 bridge",
                    ev.id,
                )
            else:
                await self._send_text(
                    ev.chat_id, ev.is_group,
                    "bridge 正在重启，稍后恢复...",
                    ev.id,
                )
                self._write_reboot_notification(ev)
                await self._cleanup_policy()
                subprocess.Popen(
                    ["systemctl", "--user", "restart", "qq-bridge.service"],
                    start_new_session=True,
                )
            return

        if parsed.name == "stop":
            ref = parsed.args.split()[0] if parsed.args else ""
            okc, jid, job, reason = self.policy.cancel_by_ref(ref, ev.sender_id)
            if okc and jid:
                reporter = self._progress_reporters.pop(jid, None)
                if reporter:
                    reporter.stop()
                label = self.policy.format_job_line(jid, job) if job else jid
                text = f"已停止 {label}"
            else:
                shown_ref = ref or "-1"
                text = f"停止失败：{reason} ({shown_ref})。可以先用 /status 看任务列表。"
            await self._send_text(ev.chat_id, ev.is_group, text, ev.id)
            await self._cleanup_policy()
            return

        if parsed.name == "approve":
            parts = parsed.args.split()
            if len(parts) >= 2:
                jid, nonce = parts[0], parts[1]
                res = await self.policy.approve(jid, nonce, ev.sender_id)
                await self._send_text(ev.chat_id, ev.is_group, f"approved {res or 'no'}", ev.id)
                if res:
                    job = self.policy.jobs.get(res)
                    if job:
                        self._create_progress_reporter(job)
                        self.policy.start_job_task(job)
                        self._schedule_reply(job)
            await self._cleanup_policy()
            return

        # run ask / plan / search / task / code
        jid, nonce = self.policy.start_job(ev, parsed)
        job = self.policy.jobs.get(jid)
        if job:
            self._configure_outgoing_resources(job)
            self._create_progress_reporter(job)
        if nonce:
            msg = (
                f"Job {jid} wants to run {parsed.name}. "
                f"Reply: /approve {jid} {nonce}；取消：/stop {jid}"
            )
            await self._send_text(ev.chat_id, ev.is_group, msg, ev.id)
            await self._cleanup_policy()
            return

        if job:
            self.policy.start_job_task(job)
            progress = self._progress_message(parsed.name)
            if progress:
                await self._send_text(ev.chat_id, ev.is_group, progress, f"{ev.id}-progress")
            self._schedule_reply(job)

    async def _acknowledge_memory_command(self, ev: ChatEvent, text: str) -> None:
        await self._send_text(ev.chat_id, ev.is_group, text, f"{ev.id}-memory-ack")

    def _collect_long_term_event(
        self,
        ev: ChatEvent,
        command_name: str | None = None,
    ) -> bool:
        collector = self.long_term_memory_collector
        coordinator = self.memory_review_coordinator
        if not self._long_term_memory_accepting or collector is None or coordinator is None:
            return False
        try:
            collected = collector.collect_event(ev, command_name=command_name)
            if not collected:
                return False
            scope = exact_memory_scope(
                is_group=ev.is_group,
                chat_id=ev.chat_id,
                sender_id=ev.sender_id,
            )
            coordinator.notify(scope)
            return True
        except Exception as exc:  # noqa: BLE001 - memory must never block chat
            logger.warning(
                "long-term memory collection failed scope=%s error=%s",
                "group" if ev.is_group else "private",
                type(exc).__name__,
            )
            return False

    def _cancel_memory_review_for_interactive(self) -> None:
        coordinator = self.memory_review_coordinator
        if coordinator is not None:
            coordinator.cancel_background_for_interactive()

    def _schedule_memory_review_delivery(
        self,
        ev: ChatEvent,
        request: MemoryReviewRequest,
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._deliver_memory_review(ev, request),
            name="memory-review-delivery",
        )
        self._memory_review_tasks.add(task)
        task.add_done_callback(self._memory_review_tasks.discard)
        return task

    async def _deliver_memory_review(
        self,
        ev: ChatEvent,
        request: MemoryReviewRequest,
    ) -> None:
        coordinator = self.memory_review_coordinator
        if coordinator is None:
            return
        try:
            outcome = await coordinator.review_now(request.scope, request.actor)
            text = self._memory_review_summary(outcome)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - fixed user-facing failure only
            logger.warning("explicit memory review failed error=%s", type(exc).__name__)
            text = "复盘暂时没有完成，保留的内容会在稍后自动重试。"
        await self._send_text(
            ev.chat_id,
            ev.is_group,
            text,
            f"{ev.id}-memory-review",
        )

    @staticmethod
    def _memory_review_summary(outcome: CuratorOutcome) -> str:
        if outcome.error is not None:
            return "复盘暂时没有完成，保留的内容会在稍后自动重试。"
        added = revised = reinforced = candidates = 0
        for proposal in outcome.accepted:
            if proposal.operation == "mark_candidate" or proposal.status == "candidate":
                candidates += 1
            elif proposal.operation == "add":
                added += 1
            elif proposal.operation == "reinforce":
                reinforced += 1
            elif proposal.operation in {"revise", "contradict", "merge"}:
                revised += 1
        return (
            f"复盘完成：新增 {added}，修订 {revised}，强化 {reinforced}，"
            f"候选 {candidates}，拒绝 {len(outcome.rejected)}。"
        )

    async def _drain_memory_review_tasks(self, *, cancel: bool = False) -> None:
        tasks = tuple(self._memory_review_tasks)
        if cancel:
            for task in tasks:
                task.cancel()
            if tasks:
                done, pending = await asyncio.wait(
                    tasks,
                    timeout=MEMORY_REVIEW_SHUTDOWN_GRACE_SECONDS,
                )
                if done:
                    await asyncio.gather(*done, return_exceptions=True)
                if pending:
                    logger.warning(
                        "memory review shutdown cleanup timed out pending=%d",
                        len(pending),
                    )
            return
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _is_self_event(self, ev: ChatEvent) -> bool:
        return bool(self.cfg.bot.self_id and ev.sender_id == self.cfg.bot.self_id)

    def _should_cache_unmentioned_resources(self, ev: ChatEvent) -> bool:
        if not self.cfg.resources.enabled or not self.cfg.resources.cache_enabled:
            return False
        if not ev.resources or not self.cfg.is_group_allowed(ev.chat_id):
            return False
        return True

    def _should_remember_ambient(self, ev: ChatEvent) -> bool:
        cfg = self.cfg.ambient_memory
        if not cfg.enabled or not ev.is_group or ev.mentioned_bot:
            return False
        if self.cfg.bot.self_id and ev.sender_id == self.cfg.bot.self_id:
            return False
        if not self.cfg.is_group_allowed(ev.chat_id):
            return False
        if cfg.allowed_groups and ev.chat_id not in cfg.allowed_groups:
            return False
        return True

    def _missing_quoted_voice_resource(self, ev: ChatEvent, cmd: str) -> bool:
        if cmd not in {"ask", "plan", "task"} or not ev.reply:
            return False
        if self._has_voice_resource(ev):
            return False
        quoted_text = (ev.reply.text or ev.reply.raw_message).strip()
        return quoted_text.startswith("[语音") and quoted_text.endswith("]")

    def _has_voice_resource(self, ev: ChatEvent) -> bool:
        if any(resource.kind == "voice" for resource in ev.resources):
            return True
        if ev.reply and any(resource.kind == "voice" for resource in ev.reply.resources):
            return True
        return False

    def _schedule_reply(self, job: Job) -> asyncio.Task[None]:
        self._start_heartbeat(job)
        task = asyncio.create_task(self._reply_when_done(job))
        self._reply_tasks.add(task)
        task.add_done_callback(self._reply_tasks.discard)
        return task

    def _progress_enabled_for(self, job: Job) -> bool:
        return self.cfg.progress.enabled and job.cmd in {"task", "code"}

    def _create_progress_reporter(self, job: Job) -> None:
        if not self._progress_enabled_for(job) or job.id in self._progress_reporters:
            return

        async def send(text: str, echo: str) -> None:
            await self._send_text(job.event.chat_id, job.event.is_group, text, echo)

        self._progress_reporters[job.id] = ProgressReporter(job.id, job.event, self.cfg.progress, send)

    def _start_heartbeat(self, job: Job) -> None:
        reporter = self._progress_reporters.get(job.id)
        if not reporter:
            return
        task = asyncio.create_task(reporter.run_heartbeat(lambda: job.state in {"done", "cancelled"}))
        self._heartbeat_tasks.add(task)
        task.add_done_callback(self._heartbeat_tasks.discard)

    def _progress_callback_for(self, job: Job):
        reporter = self._progress_reporters.get(job.id)
        if not reporter:
            return None
        redact_extra = self._outgoing_redaction_values(job)

        async def send_progress(text: str) -> None:
            cleaned = redact(strip_ansi(text), extra=redact_extra)
            if _claims_artifact_delivery(cleaned):
                cleaned = _PRE_ACK_DELIVERY_PROGRESS
            await reporter.send_progress(cleaned)

        return send_progress

    def _track_artifact_repair_task(self, task: asyncio.Task[str]) -> None:
        self._artifact_repair_tasks.add(task)

        def finish(done: asyncio.Task[str]) -> None:
            self._artifact_repair_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finish)

    async def _repair_outgoing_artifacts(self, job: Job, warnings: tuple[str, ...]) -> str:
        elapsed = max(0.0, time.time() - job.started)
        parent_timeout = (
            job.timeout_seconds
            if job.timeout_seconds is not None
            else self.cfg.effective_max_runtime()
        )
        remaining = max(0.0, parent_timeout - elapsed)
        timeout = min(90.0, remaining)
        if timeout <= 0:
            return ""

        safe_warnings = tuple(
            redact(
                warning,
                extra=self._outgoing_redaction_values(job),
            ).strip()
            for warning in warnings
            if warning.strip()
        )
        warning_text = "\n".join(f"- {warning}" for warning in safe_warnings)
        if not warning_text:
            warning_text = "- 资源声明无法验证"
        prompt = "\n".join(
            [
                "修复本次任务的输出资源交付。",
                f"原始任务：{job.args or job.event.text}",
                "验证失败原因：",
                warning_text,
                self._format_outgoing_resource_context(job),
                "要求：",
                "1. 只修复缺失的资源声明或文件，复用本次任务已经完成的工作。",
                "2. 使用当前输出目录和令牌，只输出有效的 QQBOT_SEND_* 指令。",
                "3. 不要输出解释、寒暄或任何成功声明。",
            ]
        )
        repair_task = asyncio.create_task(
            run_agent(
                self.gated_agent,
                prompt,
                self.cfg.agent.default_workspace,
                "task",
                model=self._agent_model_for("task"),
                progress=None,
                trace_id=f"{job.id}-artifact-repair",
                redact_extra=self._outgoing_redaction_values(job),
            )
        )
        self._track_artifact_repair_task(repair_task)
        try:
            done, _pending = await asyncio.wait({repair_task}, timeout=timeout)
            if repair_task not in done:
                repair_task.cancel()
                logger.warning("artifact repair failed job=%s error=TimeoutError", job.id)
                return ""
            return repair_task.result()
        except asyncio.CancelledError:
            repair_task.cancel()
            raise
        except asyncio.TimeoutError:
            logger.warning("artifact repair failed job=%s error=TimeoutError", job.id)
        except Exception as exc:  # noqa: BLE001 - repair failure becomes deterministic bridge text
            logger.warning(
                "artifact repair failed job=%s error=%s",
                job.id,
                type(exc).__name__,
            )
        return ""

    async def _drain_artifact_repair_tasks(self) -> None:
        tasks = tuple(self._artifact_repair_tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        done, pending = await asyncio.wait(
            tasks,
            timeout=max(0.0, ARTIFACT_REPAIR_SHUTDOWN_GRACE_SECONDS),
        )
        for task in done:
            if not task.cancelled():
                task.exception()
        self._artifact_repair_tasks.difference_update(done)
        if pending:
            logger.warning(
                "artifact repair shutdown cleanup timed out pending=%d",
                len(pending),
            )

    async def _cleanup_policy(self) -> None:
        if self.policy:
            await self.policy.cleanup()

    async def _reply_when_done(self, job: Job) -> None:
        try:
            await self._reply_when_done_inner(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - delivery failures become class-only logs
            if job.artifact_delivery_outcome is not None:
                job.artifact_delivery_outcome = "failed"
            logger.error(
                "job reply delivery failed job=%s error=%s",
                job.id,
                type(exc).__name__,
            )
        finally:
            await self._cleanup_reply_job(job)

    async def _cleanup_reply_job(self, job: Job) -> None:
        if job.cmd == "schedule":
            self._stop_schedule_parse_heartbeat(job.id)
            self._schedule_parse_mentions.pop(job.id, None)
            self._schedule_parse_safety_required.pop(job.id, None)
            self._schedule_parse_outcomes.pop(job.id, None)
        reporter = self._progress_reporters.pop(job.id, None)
        if reporter:
            reporter.stop()
        self._outgoing_jobs.pop(job.event.id, None)
        try:
            await self._cleanup_policy()
        except Exception:  # noqa: BLE001 - cleanup must not mask the original job outcome
            logger.exception("job cleanup failed job=%s", job.id)
        finally:
            for path in self._protected_storage_paths.pop(job.id, ()):
                self.storage_maintainer.unprotect_path(path)
            self.storage_maintainer.request_pressure_check()

    async def _reply_when_done_inner(self, job: Job) -> None:
        if not job.task:
            return
        try:
            result = await job.task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current and current.cancelling():
                raise
            return
        if job.cmd == "schedule":
            await self._finish_schedule_parse_job(job, result)
            return
        transactional = False
        if job.allow_outgoing_resources and job.outgoing_dir and job.outgoing_token:
            job.artifact_delivery_outcome = "failed"
            expected_outbox = (
                (job.outgoing_dir_dev, job.outgoing_dir_ino)
                if job.outgoing_dir_dev is not None and job.outgoing_dir_ino is not None
                else None
            )

            def inspect(text: str):
                return inspect_outgoing_resources(
                    text,
                    self.cfg,
                    outbox_dir=job.outgoing_dir,
                    token=job.outgoing_token,
                    job_id=job.id,
                    expected_outbox=expected_outbox,
                )

            artifact_result = job.artifact_result
            job.artifact_result = None
            resolution = await resolve_artifacts(
                artifact_result if artifact_result is not None else result,
                inspect=inspect,
                repair=lambda warnings: self._repair_outgoing_artifacts(job, warnings),
                max_items=self.cfg.resources.max_items,
                max_total_bytes=self.cfg.resources.max_total_bytes,
            )
            outgoing = resolution.resources
            transactional = bool(outgoing) or not resolution.verified
            if not transactional:
                job.artifact_delivery_outcome = None
            if not resolution.verified:
                reply_text = "文件没有成功生成或无法验证，本次未发送。"
            elif outgoing:
                sent = 0
                failed = 0
                for index, resource in enumerate(outgoing):
                    try:
                        await self._send_outgoing_resource(job, resource, index)
                    except Exception as exc:  # noqa: BLE001 - isolate each OneBot resource failure
                        failed += 1
                        logger.warning(
                            "outgoing resource delivery failed job=%s index=%d kind=%s error=%s",
                            job.id,
                            index,
                            resource.kind,
                            type(exc).__name__,
                        )
                    else:
                        sent += 1
                if failed == 0:
                    job.artifact_delivery_outcome = "succeeded"
                    reply_text = resolution.text
                elif sent == 0:
                    reply_text = "文件已经生成，但发送到 QQ 失败，本次未确认交付。"
                else:
                    reply_text = f"已发送 {sent} 个资源，另有 {failed} 个发送失败。"
            else:
                reply_text = resolution.text
        else:
            reply_text = result

        reply_text = redact(
            strip_ansi(reply_text),
            extra=self._outgoing_redaction_values(job),
        )[: self.cfg.effective_max_chars()]
        remember = self.cfg.memory.enabled and job.cmd in {"ask", "plan", "task", "code"}
        if not transactional and remember and reply_text.strip():
            self.memory.append_exchange(job.event, job.args or job.event.text, reply_text)
        reply_text = guard_internal_output(reply_text)
        ev = job.event
        if reply_text.strip():
            replies = self._reply_chunks(job.id, reply_text)
            delay = max(0.0, self.cfg.bot.reply_chunk_delay_seconds)
            for i, reply in enumerate(replies):
                if i and delay:
                    await asyncio.sleep(delay)
                if i == 0 and ev.is_group and job.reply_ats:
                    await self.adapter.send_ats(
                        ev.chat_id,
                        job.reply_ats,
                        reply,
                        f"{ev.id}-{i}",
                    )
                    self.proactive.record_bot_send(ev.chat_id)
                else:
                    await self._send_text(ev.chat_id, ev.is_group, reply, f"{ev.id}-{i}")
        if transactional and remember and reply_text.strip():
            self.memory.append_exchange(job.event, job.args or job.event.text, reply_text)

    async def _send_outgoing_resource(
        self,
        job: Job,
        resource: OutgoingResource,
        index: int,
    ) -> None:
        ev = job.event
        echo = f"{ev.id}-r{index}"
        if resource.kind == "image":
            await self.adapter.send_image(ev.chat_id, ev.is_group, resource.path, echo)
        elif resource.kind == "voice":
            await self.adapter.send_voice(ev.chat_id, ev.is_group, resource.path, echo)
        elif resource.kind == "file":
            await self.adapter.send_file(ev.chat_id, ev.is_group, resource.path, echo)
        else:
            raise ValueError("unsupported outgoing resource kind")

    async def _send_text(
        self,
        chat_id: str,
        is_group: bool,
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        await self.adapter.send(chat_id, is_group, text, echo, reply_to=reply_to)
        if is_group:
            self.proactive.record_bot_send(chat_id)

    async def _send_proactive(
        self,
        chat_id: str,
        text: str,
        echo: str | None = None,
        ats: tuple[str, ...] = (),
        reply_to: str | None = None,
    ) -> None:
        if ats:
            if len(ats) == 1:
                await self.adapter.send_at(chat_id, ats[0], text, echo, reply_to=reply_to)
            else:
                await self.adapter.send_ats(chat_id, ats, text, echo, reply_to=reply_to)
            self.proactive.record_bot_send(chat_id)
            return
        await self._send_text(chat_id, True, text, echo, reply_to=reply_to)

    async def _send_mention_decision(self, ev: ChatEvent, decision: MentionDecision) -> None:
        replies = decision.replies
        delay = max(0.0, self.cfg.proactive.reply_message_delay_seconds)
        for idx, reply in enumerate(replies):
            if idx and delay:
                await asyncio.sleep(delay)
            echo = f"mention-{ev.id}-{idx}" if len(replies) > 1 else f"mention-{ev.id}"
            reply_to = ev.id if idx == 0 else None
            await self._send_proactive(ev.chat_id, reply.text, echo, reply.ats, reply_to=reply_to)
        if self.cfg.memory.enabled and replies:
            self.memory.append_exchange(ev, ev.text, "\n".join(reply.text for reply in replies))

    def _remember_proactive_exchange(self, batch, replies) -> None:
        if not self.cfg.memory.enabled or not batch or not replies:
            return
        for msg in batch:
            ev = ChatEvent(
                id=msg.id,
                platform="qq",
                chat_id=msg.chat_id,
                sender_id=msg.sender_id,
                is_group=True,
                mentioned_bot=False,
                text=msg.text,
                timestamp=msg.timestamp,
            )
            self.memory.append_user_message(ev)
        last = batch[-1]
        ev = ChatEvent(
            id=f"proactive:{last.id}",
            platform="qq",
            chat_id=last.chat_id,
            sender_id=self.cfg.bot.self_id or "bot",
            is_group=True,
            mentioned_bot=False,
            text="",
            timestamp=last.timestamp,
        )
        self.memory.append_assistant_message(
            ev,
            "\n".join(reply.text for reply in replies),
            message_id=f"proactive:{last.id}:assistant",
        )

    async def _reload_config(self) -> tuple[bool, str]:
        async with self._reload_lock:
            return await self._reload_config_locked()

    async def _reload_config_locked(self) -> tuple[bool, str]:
        try:
            cfg = BridgeConfig.load(self.config_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("config reload failed")
            return False, f"[error] 配置重载失败：{type(exc).__name__}"

        try:
            new_agent = build_agent_adapter(cfg)
            new_gated_agent = GatedAgentAdapter(new_agent, self.storage_gate)
            new_search = WorkspaceSearch(cfg)
            new_transcriber, new_resources = self._resource_manager_parts(cfg)
            new_schedule_parser = NaturalLanguageScheduleParser(cfg, new_gated_agent)
            new_proactive = ProactiveSpeaker(
                cfg,
                new_gated_agent,
                self._send_proactive,
                ambient_context=self._proactive_ambient_context,
                long_term_context=self._retrieve_long_term_context,
                remember=self._remember_proactive_exchange,
            )
        except Exception as exc:  # noqa: BLE001 - no live state has changed yet
            logger.warning("config reload staging failed error=%s", type(exc).__name__)
            return False, f"[error] 配置重载失败：{type(exc).__name__}"

        old_cfg = self.cfg
        old_schedule_path = self.schedule_database_path
        new_schedule_path = self._schedule_database_path(cfg)
        scheduler_was_running = self._scheduler_is_running()
        proactive_handoff = self.proactive.begin_handoff()
        schedule_note = ""
        try:
            self.scheduler.reload_config(cfg.scheduler)
            if new_schedule_path != old_schedule_path:
                await self.scheduler.stop()
                schedule_note = " scheduler.database_path 变更需要重启。"
            elif cfg.scheduler.enabled:
                await self.scheduler.start()
            else:
                await self.scheduler.stop()
            await self.proactive.stop()
        except BaseException as exc:  # cancellation must also restore the old runtime
            await self._rollback_reload_transitions(
                old_cfg,
                was_running=scheduler_was_running,
                proactive_handoff=proactive_handoff,
            )
            if not isinstance(exc, Exception):
                raise
            logger.warning("config reload transition failed error=%s", type(exc).__name__)
            return False, f"[error] 配置重载失败：{type(exc).__name__}"

        try:
            memory_ok, memory_note = await self._reload_long_term_memory(cfg)
        except BaseException:
            await self._rollback_reload_transitions(
                old_cfg,
                was_running=scheduler_was_running,
                proactive_handoff=proactive_handoff,
            )
            raise
        if not memory_ok:
            await self._rollback_reload_transitions(
                old_cfg,
                was_running=scheduler_was_running,
                proactive_handoff=proactive_handoff,
            )
            return False, f"[error] 配置重载失败：{memory_note}"

        self.cfg = cfg
        storage_roots_changed = self.storage_maintainer.reload_config(cfg)
        self.agent = new_agent
        self.cursor = new_agent
        self.gated_agent = new_gated_agent
        self.search = new_search
        self.transcriber = new_transcriber
        self.resources = new_resources
        self.memory.max_messages = cfg.memory.max_messages
        self.memory.max_chars = cfg.memory.max_chars
        self.ambient_memory.configure(
            max_messages=cfg.ambient_memory.max_messages,
            max_chars=cfg.ambient_memory.max_chars,
            max_message_chars=cfg.ambient_memory.max_message_chars,
            max_age_seconds=cfg.ambient_memory.max_age_seconds,
            min_chars=cfg.ambient_memory.min_chars,
            ignored_prefixes=tuple(cfg.ambient_memory.ignored_prefixes),
        )
        cache_max_items = min(cfg.resources.cache_max_items, cfg.resources.max_items)
        self.attachment_cache = AttachmentCache(
            ttl_seconds=cfg.resources.cache_ttl_seconds,
            max_items=cache_max_items,
        )
        if self.policy:
            self.policy.reload_config(cfg)
        self.schedule_nl_parser = new_schedule_parser
        if new_schedule_path != old_schedule_path:
            self._scheduler_restart_required = True
        else:
            self._scheduler_restart_required = False
        self.proactive.commit_handoff(proactive_handoff, new_proactive)
        self.proactive = new_proactive
        storage_note = " 存储根目录变更需要重启。" if storage_roots_changed else ""
        return True, (
            f"配置已重载。OneBot 连接参数变更需要重启。"
            f"{schedule_note}{storage_note}{memory_note}"
        ).strip()

    def _scheduler_is_running(self) -> bool:
        running = getattr(self.scheduler, "running", None)
        if isinstance(running, bool):
            return running
        loop_task = getattr(self.scheduler, "_loop_task", None)
        return loop_task is not None and not loop_task.done()

    async def _rollback_reload_transitions(
        self,
        cfg: BridgeConfig,
        *,
        was_running: bool,
        proactive_handoff: int,
    ) -> None:
        await self.proactive.rollback_handoff(proactive_handoff)
        await self._restore_scheduler_after_failed_reload(
            cfg,
            was_running=was_running,
        )

    async def _restore_scheduler_after_failed_reload(
        self,
        cfg: BridgeConfig,
        *,
        was_running: bool,
    ) -> None:
        try:
            self.scheduler.reload_config(cfg.scheduler)
            await self.scheduler.stop()
            if was_running:
                await self.scheduler.start()
        except Exception as exc:  # noqa: BLE001 - preserve the original reload failure
            logger.warning("scheduler reload rollback failed error=%s", type(exc).__name__)

    def _long_term_memory_path(self, cfg: BridgeConfig) -> Path:
        path = Path(cfg.long_term_memory.database_path).expanduser()
        if path.is_absolute():
            return path.resolve(strict=False)
        return (self.config_path.parent / path).resolve(strict=False)

    def _apply_long_term_memory_scope_config(
        self,
        store: LongTermMemoryStore,
        cfg: BridgeConfig,
    ) -> None:
        store.apply_scope_configuration(
            default_scope_enabled=cfg.long_term_memory.default_scope_enabled,
            groups=cfg.long_term_memory.groups,
            users=cfg.long_term_memory.users,
        )

    async def _initialize_long_term_memory(
        self,
        target_cfg: BridgeConfig | None = None,
    ) -> None:
        if self.long_term_memory_store is not None:
            return
        self.long_term_memory_error = None
        self._long_term_memory_accepting = False
        app_cfg = target_cfg or self.cfg
        if self.echo_only or not app_cfg.long_term_memory.enabled:
            return

        cfg = app_cfg.long_term_memory
        database_path = self._long_term_memory_path(app_cfg)
        store = LongTermMemoryStore(
            database_path,
            default_scope_enabled=cfg.default_scope_enabled,
            raw_ttl_seconds=cfg.review.raw_ttl_seconds,
            decay_grace_seconds=cfg.decay.grace_seconds,
            dormant_threshold=cfg.decay.dormant_threshold,
        )
        coordinator: MemoryReviewCoordinator | None = None
        interpreter = None
        try:
            async with self.storage_gate.activity():
                store.initialize()
                self._apply_long_term_memory_scope_config(store, app_cfg)
            coordinator = build_memory_review_coordinator(
                app_cfg,
                store,
                self.storage_gate,
                app_cfg.agent.default_workspace,
            )
            interpreter = build_memory_command_interpreter(
                app_cfg,
                self.storage_gate,
                app_cfg.agent.default_workspace,
            )
            await coordinator.start()
        except BaseException as exc:  # cleanup must also cover startup cancellation
            if coordinator is not None:
                try:
                    await self._stop_memory_coordinator(coordinator)
                except BaseException:  # noqa: BLE001 - preserve the original failure
                    pass
            self._dispose_memory_runtime(interpreter)
            self._dispose_memory_runtime(coordinator)
            store.close()
            if not isinstance(exc, Exception):
                raise
            self.long_term_memory_error = type(exc).__name__
            logger.warning(
                "long-term memory startup failed error=%s",
                self.long_term_memory_error,
            )
            return

        protected = (
            database_path.parent,
            database_path,
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
        )
        for path in protected:
            self.storage_maintainer.protect_path(path)
        self._long_term_memory_protected_paths = protected
        self.long_term_memory_database_path = database_path
        self.long_term_memory_store = store
        self.long_term_memory_collector = MemoryCollector(store, app_cfg)
        self.long_term_memory_retriever = LongTermMemoryRetriever(
            store,
            app_cfg.long_term_memory,
        )
        self.memory_review_coordinator = coordinator
        self.memory_commands = MemoryCommandService(
            app_cfg,
            store,
            interpreter=interpreter,
            acknowledge=self._acknowledge_memory_command,
        )
        self._long_term_memory_accepting = True

    async def _reload_long_term_memory(self, cfg: BridgeConfig) -> tuple[bool, str]:
        store = self.long_term_memory_store
        if store is None:
            if cfg.long_term_memory.enabled:
                await self._initialize_long_term_memory(cfg)
                if self.long_term_memory_store is None and self.long_term_memory_error:
                    return False, f"长期记忆不可用：{self.long_term_memory_error}"
            return True, ""

        configured_path = self._long_term_memory_path(cfg)
        path_note = ""
        if configured_path != self.long_term_memory_database_path:
            path_note = " long_term_memory.database_path 变更需要重启。"
        coordinator = self.memory_review_coordinator
        coordinator_reload_attempted = False
        try:
            new_collector = MemoryCollector(store, cfg)
            new_retriever = LongTermMemoryRetriever(store, cfg.long_term_memory)
            interpreter = (
                self.memory_commands.interpreter
                if self.memory_commands is not None
                else None
            )
            new_commands = MemoryCommandService(
                cfg,
                store,
                interpreter=interpreter,
                acknowledge=self._acknowledge_memory_command,
            )
            async with self.storage_gate.activity():
                with store.scope_configuration_transaction(
                    default_scope_enabled=cfg.long_term_memory.default_scope_enabled,
                    groups=cfg.long_term_memory.groups,
                    users=cfg.long_term_memory.users,
                ):
                    if coordinator is not None:
                        coordinator_reload_attempted = True
                        coordinator.reload(cfg)
            self.long_term_memory_collector = new_collector
            self.long_term_memory_retriever = new_retriever
            self.memory_commands = new_commands
            self._long_term_memory_accepting = bool(cfg.long_term_memory.enabled)
            self.long_term_memory_error = None
        except Exception as exc:  # noqa: BLE001 - retain the open store on reload errors
            if coordinator is not None and coordinator_reload_attempted:
                try:
                    coordinator.reload(self.cfg)
                except Exception as rollback_exc:  # noqa: BLE001 - report only error class
                    logger.warning(
                        "long-term memory runtime rollback failed error=%s",
                        type(rollback_exc).__name__,
                    )
            logger.warning("long-term memory reload failed error=%s", type(exc).__name__)
            return False, type(exc).__name__
        return True, path_note

    async def _shutdown_long_term_memory(self) -> None:
        self._long_term_memory_accepting = False
        coordinator = self.memory_review_coordinator
        if coordinator is not None:
            try:
                await self._stop_memory_coordinator(coordinator)
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                logger.warning("memory coordinator shutdown failed error=%s", type(exc).__name__)
        await self._drain_memory_review_tasks(cancel=True)
        store = self.long_term_memory_store
        if store is not None:
            try:
                async with self.storage_gate.activity():
                    store.close()
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                logger.warning("memory database shutdown failed error=%s", type(exc).__name__)
        commands = self.memory_commands
        self._dispose_memory_runtime(commands.interpreter if commands is not None else None)
        self._dispose_memory_runtime(coordinator)
        self.long_term_memory_store = None
        self.long_term_memory_collector = None
        self.long_term_memory_retriever = None
        self.memory_commands = None
        self.memory_review_coordinator = None

    @staticmethod
    def _dispose_memory_runtime(runtime: object | None) -> None:
        dispose = getattr(runtime, "dispose", None)
        if not callable(dispose):
            return
        try:
            dispose()
        except Exception as exc:  # noqa: BLE001 - shutdown is best effort
            logger.warning("restricted memory runtime cleanup failed error=%s", type(exc).__name__)

    async def _stop_memory_coordinator(self, coordinator: object) -> None:
        stop_task = asyncio.create_task(
            coordinator.stop(),  # type: ignore[attr-defined]
            name="memory-coordinator-stop",
        )
        done, pending = await asyncio.wait(
            (stop_task,),
            timeout=MEMORY_REVIEW_SHUTDOWN_GRACE_SECONDS,
        )
        if done:
            results = await asyncio.gather(*done, return_exceptions=True)
            failure = next((value for value in results if isinstance(value, BaseException)), None)
            if failure is not None:
                raise failure
        if pending:
            stop_task.cancel()
            stop_task.add_done_callback(self._consume_task_result)
            logger.warning(
                "memory coordinator shutdown timed out pending=%d",
                len(pending),
            )

    @staticmethod
    def _consume_task_result(task: asyncio.Task[object]) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _chunk(self, text: str, size: int = 900) -> list[str]:
        # functional chunk
        return [text[i : i + size] for i in range(0, len(text), size)] or ["[empty]"]

    def _reply_chunks(self, jid: str, text: str, size: int = 900) -> list[str]:
        chunks = self._chunk(text, size)
        if len(chunks) == 1:
            return chunks
        return [f"（{i + 1}/{len(chunks)}）{chunk}" for i, chunk in enumerate(chunks)]

    def _progress_message(self, cmd: str) -> str | None:
        if cmd == "search":
            return "收到，我搜一下。"
        if cmd == "plan":
            return "收到，我整理一下。"
        if cmd == "task":
            return "收到，我处理一下。"
        if cmd == "code":
            return "收到，我开始处理。"
        return None

    async def _handle_schedule_command(self, ev: ChatEvent, args: str) -> None:
        raw = args.strip()
        action, _, ref = raw.partition(" ")
        action = action.lower()
        if not raw or action in {"help", "帮助", "?"}:
            await self._send_text(
                ev.chat_id,
                ev.is_group,
                self._schedule_help_text(ev),
                ev.id,
            )
            return
        if self._scheduler_restart_required:
            await self._send_text(
                ev.chat_id,
                ev.is_group,
                "定时任务数据库路径刚刚变更，需要重启 bridge 后再使用。",
                ev.id,
            )
            return
        if not self.cfg.scheduler.enabled:
            await self._send_text(ev.chat_id, ev.is_group, "定时任务功能当前没有开启。", ev.id)
            return
        if action == "list":
            await self._send_text(
                ev.chat_id,
                ev.is_group,
                self._schedule_list_text(ev),
                ev.id,
            )
            return
        if action == "show":
            schedule = self.schedule_store.resolve_ref(
                ev.chat_id,
                ev.is_group,
                ref,
                creator_id=self._schedule_creator_scope(ev),
            )
            text = self._schedule_detail_text(schedule, ev) if schedule else "没有找到这个定时任务。"
            await self._send_text(ev.chat_id, ev.is_group, text, ev.id)
            return
        denied = self._schedule_mutation_denial(ev)
        if denied:
            reason = denied
            await self._send_text(ev.chat_id, ev.is_group, f"[denied] {reason}", ev.id)
            return
        if action in {"pause", "resume", "cancel", "run"}:
            await self._handle_schedule_management(ev, action, ref)
            return
        mentions = self._schedule_mentions(ev)
        try:
            spec = parse_explicit_schedule(raw, self.cfg.scheduler, mentions=mentions)
        except ScheduleParseError as exc:
            await self._send_text(ev.chat_id, ev.is_group, f"设置失败：{exc}", ev.id)
            return
        if spec is not None:
            if not self.cfg.is_owner(ev.sender_id):
                denied = self._check_non_owner_schedule_safety(spec, ev)
                if denied:
                    await self._send_text(ev.chat_id, ev.is_group, f"设置失败：{denied}", ev.id)
                    return
            try:
                schedule = self.scheduler.create(
                    replace(spec, reply_to_message_id=self._schedule_reply_to(ev)),
                    ev,
                )
            except ValueError as exc:
                await self._send_text(ev.chat_id, ev.is_group, f"设置失败：{exc}", ev.id)
                return
            await self._send_text(
                ev.chat_id,
                ev.is_group,
                self._schedule_receipt(schedule, ev),
                ev.id,
            )
            return
        if spec is None and not self.cfg.scheduler.natural_language_enabled:
            await self._send_text(
                ev.chat_id,
                ev.is_group,
                "自然语言时间解析当前没有开启，发送 /schedule help 查看结构化写法。",
                ev.id,
            )
            return
        await self._start_schedule_parse_job(
            ev,
            raw,
            mentions,
            require_safety_review=not self.cfg.is_owner(ev.sender_id),
        )

    async def _start_schedule_parse_job(
        self,
        ev: ChatEvent,
        raw: str,
        mentions: tuple[str, ...],
        *,
        require_safety_review: bool,
    ) -> None:
        assert self.policy is not None
        command = ParsedCommand(name="schedule", args=raw, raw=f"/schedule {raw}")
        jid, _nonce = self.policy.start_job(ev, command)
        job = self.policy.jobs[jid]
        job.timeout_seconds = max(1, self.cfg.scheduler.natural_language_timeout_seconds)
        job.source = "schedule-parse"
        self._schedule_parse_mentions[jid] = mentions
        self._schedule_parse_safety_required[jid] = require_safety_review
        await self._send_text(
            ev.chat_id,
            ev.is_group,
            (
                "收到，我正在理解你说的时间和任务内容，并检查这个定时任务是否安全，稍等一下。"
                if require_safety_review
                else "收到，我正在理解你说的时间和任务内容，稍等一下。"
            ),
            f"{ev.id}-schedule-start",
        )
        self.policy.start_job_task(job)
        self._start_schedule_parse_heartbeat(job)
        self._schedule_reply(job)

    async def _handle_schedule_management(self, ev: ChatEvent, action: str, ref: str) -> None:
        schedule = self.schedule_store.resolve_ref(
            ev.chat_id,
            ev.is_group,
            ref,
            creator_id=self._schedule_creator_scope(ev),
        )
        if not schedule:
            await self._send_text(
                ev.chat_id,
                ev.is_group,
                "没有找到这个定时任务。可以先用 /schedule list 查看。",
                ev.id,
            )
            return
        if action == "pause":
            changed = self.scheduler.pause(schedule.id)
            text = "已暂停这个定时任务。" if changed else "这个定时任务当前不能暂停。"
        elif action == "resume":
            changed = self.scheduler.resume(schedule.id)
            text = "已恢复这个定时任务。" if changed else "这个定时任务当前不能恢复。"
        elif action == "cancel":
            changed = self.scheduler.cancel(schedule.id)
            text = "已取消这个定时任务。" if changed else "这个定时任务当前不能取消。"
        else:
            try:
                run = self.scheduler.run_now(schedule.id)
            except RuntimeError as exc:
                run = None
                text = f"立即执行失败：{exc}"
            else:
                text = "已加入执行队列。" if run else "这个定时任务当前不能立即执行。"
        await self._send_text(ev.chat_id, ev.is_group, text, ev.id)

    def _schedule_mutation_denial(self, ev: ChatEvent) -> str | None:
        if ev.is_group:
            return self._schedule_access_denial(ev.sender_id, ev.chat_id)
        if not self.cfg.scheduler.allow_private_users:
            return "private-schedule-disabled"
        if not self.cfg.is_user_allowed(ev.sender_id):
            return "user-denied"
        return self._schedule_access_denial(ev.sender_id, None)

    def _schedule_creator_scope(self, ev: ChatEvent) -> str | None:
        """Owners see all schedules in this chat; everyone else sees their own."""
        return None if self.cfg.is_owner(ev.sender_id) else ev.sender_id

    def _schedule_access_denial(self, sender_id: str, group_id: str | None) -> str | None:
        access = self.cfg.command_access("schedule", group_id)
        if access == "disabled":
            return "cmd-disabled"
        if access == "owner" and not self.cfg.is_owner(sender_id):
            return "owner-only"
        return None

    def _check_non_owner_schedule_safety(
        self,
        spec: object,
        ev: ChatEvent,
    ) -> str | None:
        """Hard safety constraints for non-owner explicit schedule creation.

        These are conservative, non-LLM checks that mirror the rules in the
        natural-language safety prompt (skills/…/schedule-safety.md).  Owners
        always bypass this gate.  The natural-language path has its own LLM
        safety review and does not route through this method.
        """
        cfg = self.cfg.scheduler
        # Cooldown — prevent rapid-fire creation
        if cfg.non_owner_cooldown_seconds > 0:
            recent = self.schedule_store.list_for_chat(
                ev.chat_id, ev.is_group, creator_id=ev.sender_id, active_only=True
            )
            if recent:
                now = int(time.time())
                newest_created = max(item.created_at for item in recent)
                if now - newest_created < cfg.non_owner_cooldown_seconds:
                    return f"创建太频繁，请 {cfg.non_owner_cooldown_seconds} 秒后再试"

        # Total active count per chat
        user_active = self.schedule_store.list_for_chat(
            ev.chat_id, ev.is_group, creator_id=ev.sender_id, active_only=True
        )
        if len(user_active) >= max(1, cfg.non_owner_max_schedules_per_chat):
            return f"每个会话最多 {cfg.non_owner_max_schedules_per_chat} 个定时任务"

        rrule = getattr(spec, "rrule", None)
        kind = getattr(spec, "kind", None)
        mentions = getattr(spec, "mentions", ())

        # Recurring-schedule checks
        if kind == "rrule" and isinstance(rrule, str) and rrule:
            # Interval floor
            min_iv = rrule_min_interval_seconds(rrule)
            if 0 < min_iv < max(1, cfg.non_owner_min_interval_seconds):
                return f"定时任务周期不能少于 {cfg.non_owner_min_interval_seconds} 秒"

            # No forever schedules for non-owners
            if rrule_is_unbounded(rrule) and not cfg.non_owner_allow_unbounded:
                return "不允许创建无限次数的定时任务"

            # Occurrence cap
            count = rrule_occurrence_count(rrule)
            if count is not None and count > max(1, cfg.non_owner_max_occurrences):
                return f"定时任务次数不能超过 {cfg.non_owner_max_occurrences}"

        # @mention cap
        mention_count = len(mentions)
        if mention_count > max(0, cfg.non_owner_max_mentions):
            return f"最多只能 @ {cfg.non_owner_max_mentions} 个人"

        return None

    def _schedule_mentions(self, ev: ChatEvent) -> tuple[str, ...]:
        values: list[str] = []
        for segment in ev.segments:
            qq = segment.qq or ""
            if (
                segment.type != "mention"
                or not qq.isdigit()
                or qq == self.cfg.bot.self_id
                or qq in values
            ):
                continue
            values.append(qq)
        return tuple(values)

    def _schedule_reply_to(self, ev: ChatEvent) -> str | None:
        if not ev.reply or not ev.reply.message_id:
            return None
        return ev.reply.message_id

    def _schedule_help_text(self, ev: ChatEvent | None = None) -> str:
        zone = self.cfg.scheduler.timezone
        permission = self.cfg.command_access("schedule", ev.chat_id if ev and ev.is_group else None)
        is_owner = ev is not None and self.cfg.is_owner(ev.sender_id)
        lines = [
            f"/schedule 权限：{permission}。",
            "用法：/schedule <自然语言规则>，或 /schedule <结构化规则>。",
            f"定时任务使用时区：{zone}",
            "自然语言（推荐）：",
            "/schedule 明天早上十点提醒我喝水 噔噔噔",
            "/schedule 每天早上八点告诉我北京市天气",
            "/schedule 每月最后一个工作日下午六点整理本月工作",
            "结构化写法：",
            "/schedule once 2026-07-14 08:00 -- send 记得开会",
            "/schedule in 10m -- send 起来活动一下",
            "/schedule daily 08:00 -- task 查询北京市天气",
            "/schedule weekly 周二 08:00 -- send 喝水",
            "/schedule every 2h count 5 -- task 检查服务状态",
            "/schedule every 30m from 2026-07-14 09:00 until 2026-07-14 12:00 -- ask 讲个笑话",
            "/schedule every 1h forever -- task 检查服务状态",
            "任意周期高级写法：",
            "/schedule rrule 2026-07-31 18:00 FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1 -- task 整理本月工作",
            "管理：/schedule list、show <索引>、pause <索引>、resume <索引>、run <索引>、cancel <索引>",
            "索引支持 0、1、2 和 -1、-2；省略时默认 -1，例如 /schedule cancel -1。",
        ]
        if not is_owner:
            cfg = self.cfg.scheduler
            lines.append("")
            lines.append("非 owner 用户结构化限制：")
            lines.append(f"  最小周期：{cfg.non_owner_min_interval_seconds} 秒")
            lines.append(f"  最多次数：{cfg.non_owner_max_occurrences}")
            if not cfg.non_owner_allow_unbounded:
                lines.append("  无限循环：不允许")
            lines.append(f"  最大 @人数：{cfg.non_owner_max_mentions}")
            lines.append(f"  每会话最多：{cfg.non_owner_max_schedules_per_chat} 个定时任务")
            lines.append(f"  创建冷却：{cfg.non_owner_cooldown_seconds} 秒")
            lines.append("自然语言创建不受上述限制，但需通过安全审查。")
        return "\n".join(lines)

    def _schedule_list_text(self, ev: ChatEvent) -> str:
        schedules = self.schedule_store.list_for_chat(
            ev.chat_id,
            ev.is_group,
            creator_id=self._schedule_creator_scope(ev),
        )
        if not schedules:
            return "当前没有进行中或暂停的定时任务。"
        lines = ["当前定时任务："]
        lines.extend(self._schedule_line(index, schedule) for index, schedule in enumerate(schedules))
        lines.append("用 /schedule show <索引> 查看详情。")
        return "\n".join(lines)

    def _schedule_line(self, index: int, schedule: Schedule) -> str:
        status = {"active": "运行中", "paused": "已暂停", "finishing": "执行中"}.get(
            schedule.status,
            schedule.status,
        )
        description = schedule.description or (
            "执行一次" if schedule.kind == "once" else schedule.rrule or "周期任务"
        )
        payload = " ".join(schedule.payload.split())
        if len(payload) > 46:
            payload = payload[:43].rstrip() + "..."
        next_text = self._format_schedule_time(schedule.next_run_at, schedule.timezone)
        return f"{index}. [{status}] {description} | {schedule.action}：{payload} | 下次 {next_text}"

    def _schedule_detail_text(self, schedule: Schedule, ev: ChatEvent) -> str:
        items = self.schedule_store.list_for_chat(
            ev.chat_id,
            ev.is_group,
            creator_id=self._schedule_creator_scope(ev),
        )
        try:
            index = items.index(schedule)
        except ValueError:
            index = -1
        rule = schedule.rrule or "单次"
        return "\n".join(
            [
                self._schedule_line(index, schedule),
                f"ID：{schedule.id}",
                f"规则：{rule}",
                f"已执行 {schedule.run_count} 次，成功 {schedule.success_count} 次，失败 {schedule.failure_count} 次，错过 {schedule.missed_count} 次",
            ]
        )

    def _schedule_receipt(self, schedule: Schedule, ev: ChatEvent) -> str:
        schedules = self.schedule_store.list_for_chat(
            ev.chat_id,
            ev.is_group,
            creator_id=self._schedule_creator_scope(ev),
        )
        try:
            index = schedules.index(schedule)
        except ValueError:
            index = len(schedules) - 1
        return f"已经设置好了：\n{self._schedule_line(index, schedule)}"

    def _format_schedule_time(self, epoch: int | None, timezone: str) -> str:
        if epoch is None:
            return "无"
        return datetime.fromtimestamp(epoch, tz=UTC).astimezone(ZoneInfo(timezone)).strftime(
            "%Y-%m-%d %H:%M"
        )

    def _start_schedule_parse_heartbeat(self, job: Job) -> None:
        interval = max(0, self.cfg.scheduler.natural_language_progress_seconds)
        if interval <= 0:
            return

        async def heartbeat() -> None:
            try:
                await asyncio.sleep(interval)
                while job.state in {"queued", "running"}:
                    await self._send_text(
                        job.event.chat_id,
                        job.event.is_group,
                        "还在确认具体的时间规则，马上好。",
                        f"{job.event.id}-schedule-progress",
                    )
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(heartbeat())
        self._schedule_parse_heartbeats[job.id] = task

    def _stop_schedule_parse_heartbeat(self, job_id: str) -> None:
        task = self._schedule_parse_heartbeats.pop(job_id, None)
        if task:
            task.cancel()

    async def _finish_schedule_parse_job(self, job: Job, result: str) -> None:
        self._stop_schedule_parse_heartbeat(job.id)
        mentions = self._schedule_parse_mentions.pop(job.id, ())
        self._schedule_parse_safety_required.pop(job.id, None)
        outcome = self._schedule_parse_outcomes.pop(job.id, None)
        if outcome is None:
            if result == "[timeout]":
                text = "时间规则理解超时了，定时任务没有创建。可以说得更明确一点再试。"
            else:
                text = "这次没能理解时间规则，定时任务没有创建。可以发送 /schedule help 查看示例。"
            await self._send_text(job.event.chat_id, job.event.is_group, text, job.event.id)
            return
        if outcome.safety_blocked or outcome.spec is None:
            text = f"{outcome.clarification}\n没有创建定时任务。"
            await self._send_text(job.event.chat_id, job.event.is_group, text, job.event.id)
            return
        if (
            self._scheduler_restart_required
            or not self.cfg.scheduler.enabled
            or self.cfg.command_access(
                "schedule",
                job.event.chat_id if job.event.is_group else None,
            )
            == "disabled"
        ):
            await self._send_text(
                job.event.chat_id,
                job.event.is_group,
                "时间规则已经理解，但配置刚刚发生变化，定时任务没有创建。",
                job.event.id,
            )
            return
        denied = self._schedule_mutation_denial(job.event)
        if denied:
            await self._send_text(
                job.event.chat_id,
                job.event.is_group,
                f"时间规则已经理解，但当前权限已不允许创建定时任务：{denied}。",
                job.event.id,
            )
            return
        spec = replace(
            outcome.spec,
            mentions=mentions,
            reply_to_message_id=self._schedule_reply_to(job.event),
        )
        try:
            schedule = self.scheduler.create(spec, job.event)
        except ValueError as exc:
            text = f"理解完成了，但没有创建定时任务：{exc}"
        else:
            text = self._schedule_receipt(schedule, job.event)
        await self._send_text(job.event.chat_id, job.event.is_group, text, job.event.id)

    async def _execute_schedule(
        self,
        schedule: Schedule,
        run: ScheduleRun,
    ) -> ScheduleExecutionResult:
        allowed, reason = self._schedule_execution_allowed(schedule)
        if not allowed:
            await self._send_schedule_text(
                schedule,
                f"定时任务未执行：{reason}",
                f"schedule-{schedule.id}-{run.id}-denied",
            )
            return ScheduleExecutionResult("failed", reason)
        if schedule.action == "send":
            await self._send_schedule_text(
                schedule,
                schedule.payload,
                f"schedule-{schedule.id}-{run.id}",
            )
            if self.cfg.memory.enabled:
                self.memory.append_assistant_message(
                    self._schedule_event(schedule, run),
                    schedule.payload,
                    message_id=f"schedule:{schedule.id}:{run.id}:assistant",
                )
            return ScheduleExecutionResult("succeeded")
        assert self.policy is not None
        ev = self._schedule_event(schedule, run)
        command = ParsedCommand(
            name=schedule.action,  # type: ignore[arg-type]
            args=schedule.payload,
            raw=f"/{schedule.action} {schedule.payload}",
        )
        ok, reason = self.policy.allow(ev, schedule.action)
        if not ok:
            await self._send_schedule_text(
                schedule,
                f"定时任务未执行：{reason}",
                f"schedule-{schedule.id}-{run.id}-denied",
            )
            return ScheduleExecutionResult("failed", reason)
        jid, nonce = self.policy.start_job(ev, command)
        if nonce:
            return ScheduleExecutionResult("failed", "unexpected approval request")
        job = self.policy.jobs[jid]
        job.source = "schedule"
        job.schedule_id = schedule.id
        job.schedule_run_id = run.id
        job.scheduled_for = run.due_at
        job.reply_ats = schedule.mentions if schedule.is_group else ()
        self._configure_outgoing_resources(job)
        self._create_progress_reporter(job)
        await self._send_schedule_text(
            schedule,
            f"定时任务开始执行：{self._short_schedule_payload(schedule.payload)}",
            f"schedule-{schedule.id}-{run.id}-start",
        )
        self.policy.start_job_task(job)
        reply_task = self._schedule_reply(job)
        assert job.task is not None
        try:
            result = await job.task
        except asyncio.CancelledError:
            return ScheduleExecutionResult("cancelled", "cancelled", jid)
        try:
            await reply_task
        except Exception:
            reporter = self._progress_reporters.pop(job.id, None)
            if reporter:
                reporter.stop()
            self._outgoing_jobs.pop(job.event.id, None)
            await self._cleanup_policy()
            raise
        if result in {"[timeout]", "[cancelled]"} or result.startswith("[error]"):
            return ScheduleExecutionResult("failed", result, jid)
        if job.artifact_delivery_outcome == "failed":
            return ScheduleExecutionResult("failed", "artifact delivery failed", jid)
        return ScheduleExecutionResult("succeeded", job_id=jid)

    def _schedule_execution_allowed(self, schedule: Schedule) -> tuple[bool, str]:
        if not self.cfg.scheduler.enabled:
            return False, "scheduler-disabled"
        if schedule.is_group:
            if not self.cfg.is_group_allowed(schedule.chat_id):
                return False, "group-denied"
            denied = self._schedule_access_denial(schedule.creator_id, schedule.chat_id)
            if denied:
                return False, denied
        elif not self.cfg.scheduler.allow_private_users or not self.cfg.is_user_allowed(
            schedule.creator_id
        ):
            return False, "user-denied"
        if schedule.action in {"ask", "task"}:
            action_access = self.cfg.command_access(
                schedule.action,
                schedule.chat_id if schedule.is_group else None,
            )
            if action_access == "disabled":
                return False, "cmd-disabled"
            if action_access == "owner" and not self.cfg.is_owner(schedule.creator_id):
                return False, "owner-only"
        return True, "ok"

    def _schedule_event(self, schedule: Schedule, run: ScheduleRun) -> ChatEvent:
        return ChatEvent(
            id=f"schedule:{schedule.id}:{run.id}",
            platform="qq",
            chat_id=schedule.chat_id,
            sender_id=schedule.creator_id,
            is_group=schedule.is_group,
            mentioned_bot=True,
            text=schedule.payload,
            timestamp=run.due_at,
            segments=tuple(
                ChatSegment(
                    type="mention",
                    text=f"@{qq} ",
                    qq=str(qq),
                    raw_type="schedule",
                    raw_data={"source": "schedule"},
                )
                for qq in schedule.mentions
                if schedule.is_group and str(qq).isdigit()
            ),
        )

    async def _send_schedule_text(self, schedule: Schedule, text: str, echo: str) -> None:
        if schedule.is_group and schedule.mentions:
            await self.adapter.send_ats(
                schedule.chat_id,
                schedule.mentions,
                text,
                echo,
                reply_to=schedule.reply_to_message_id,
            )
            self.proactive.record_bot_send(schedule.chat_id)
            return
        await self._send_text(
            schedule.chat_id,
            schedule.is_group,
            text,
            echo,
            reply_to=schedule.reply_to_message_id,
        )

    def _short_schedule_payload(self, payload: str) -> str:
        text = " ".join(payload.split())
        return text if len(text) <= 80 else text[:77].rstrip() + "..."

    def _schedule_database_path(self, cfg: BridgeConfig) -> Path:
        path = Path(cfg.scheduler.database_path).expanduser()
        if path.is_absolute():
            return path.resolve(strict=False)
        return (self.config_path.parent / path).resolve(strict=False)

    def _handle_profile_command(self, ev: ChatEvent, args: str) -> str:
        action, value = self._parse_profile_args(args)
        if action in {"set", "clear"} and ev.is_group and not self.cfg.is_owner(ev.sender_id):
            return "[denied] owner-only"
        if action == "set":
            if not value.strip():
                return "用法：/profile set <新的角色设定>"
            previous = deepcopy(self.cfg.profiles)
            self._set_profile(ev, value.strip())
            reply = self._persist_profile_reply(ev, updated=True)
            if reply.startswith("[error]"):
                self.cfg.profiles = previous
            return reply
        if action == "clear":
            previous = deepcopy(self.cfg.profiles)
            self._clear_profile(ev)
            reply = self._persist_profile_reply(ev, updated=False)
            if reply.startswith("[error]"):
                self.cfg.profiles = previous
            return reply
        return self._profile_view_reply(ev)

    def _parse_profile_args(self, args: str) -> tuple[str, str]:
        text = args.strip()
        if not text:
            return "show", ""
        parts = text.split(maxsplit=1)
        action = parts[0].lower()
        value = parts[1] if len(parts) > 1 else ""
        if action in {"set", "clear", "show"}:
            return action, value
        return "show", ""

    def _set_profile(self, ev: ChatEvent, profile: str) -> None:
        if ev.is_group:
            self.cfg.profiles.groups[ev.chat_id] = profile
        else:
            self.cfg.profiles.users[ev.sender_id] = profile

    def _clear_profile(self, ev: ChatEvent) -> None:
        if ev.is_group:
            self.cfg.profiles.groups.pop(ev.chat_id, None)
        else:
            self.cfg.profiles.users.pop(ev.sender_id, None)

    def _persist_profile_reply(self, ev: ChatEvent, *, updated: bool) -> str:
        try:
            write_profiles_to_config(self.config_path, self.cfg.profiles)
        except OSError:
            logger.exception("profile persistence failed")
            return "[error] profile 写入失败"
        if ev.is_group:
            return "已更新本群 profile" if updated else "已清除本群 profile，将使用默认 profile"
        return "已更新你的私聊 profile" if updated else "已清除你的私聊 profile，将使用默认 profile"

    def _profile_view_reply(self, ev: ChatEvent) -> str:
        scoped = self.cfg.profiles.groups.get(ev.chat_id) if ev.is_group else self.cfg.profiles.users.get(ev.sender_id)
        if scoped:
            return f"当前 profile：\n{scoped}"
        if self.cfg.profiles.default.strip():
            return f"当前没有单独 profile，正在使用默认 profile：\n{self.cfg.profiles.default.strip()}"
        return "当前没有单独 profile，正在使用内置默认 profile。"

    def _handle_mode_command(self, ev: ChatEvent, args: str) -> str:
        if not ev.is_group:
            return "/mode 仅用于群聊。"
        action, mode = self._parse_mode_args(args)
        if action == "show":
            return self._mode_view_reply(ev.chat_id)
        if action == "invalid":
            return "用法：/mode、/mode set chat|ask|plan|task、/mode clear。"
        if not self.cfg.is_owner(ev.sender_id):
            return "[denied] owner-only"
        if action == "set":
            if mode not in MENTION_MODES:
                return f"可选模式：{'、'.join(MENTION_MODE_OPTIONS)}。"
            permission_command = "ask" if mode == "chat" else mode
            if not self.cfg.is_command_allowed(permission_command, ev.chat_id):
                return f"设置失败：/{mode} 当前未启用。"
            return self._set_group_mode(ev.chat_id, mode)
        return self._clear_group_mode(ev.chat_id)

    def _is_help_subcommand(self, args: str) -> bool:
        return args.strip().lower() in {"help", "帮助", "?"}

    def _help_topic(self, args: str) -> str:
        parts = args.strip().split(maxsplit=1)
        return parts[0].lower() if parts else ""

    def _command_help_reply(self, command: str, ev: ChatEvent) -> str:
        if command.strip().lower().lstrip("/") == "schedule":
            return self._schedule_help_text(ev)
        return build_command_help(command, self.cfg, ev)

    def _handle_permission_command(self, ev: ChatEvent, args: str) -> str:
        action, command, access = self._parse_permission_args(args)
        usage = "用法：/permission、/permission set <命令> user|owner|disabled、/permission clear [命令]"
        if action == "invalid":
            return usage
        if action == "help":
            return usage
        if action == "show":
            return self._permission_view_reply(ev)
        if not ev.is_group:
            return "/permission 仅用于群聊。"
        if not self.cfg.is_owner(ev.sender_id):
            return "[denied] owner-only"

        known_commands = self._permission_command_names()
        if action == "set":
            if command not in known_commands or command == "groups" or access not in COMMAND_ACCESS_LEVELS:
                return usage
            return self._update_permission_override(ev.chat_id, command, access, clear=False)
        if action == "clear":
            if command and (command not in known_commands or command == "groups"):
                return usage
            return self._update_permission_override(ev.chat_id, command, "", clear=True)
        return usage

    def _parse_permission_args(self, args: str) -> tuple[str, str, str]:
        parts = args.strip().lower().split()
        if not parts:
            return "show", "", ""
        if parts == ["help"]:
            return "help", "", ""
        if parts[0] == "set" and len(parts) == 3:
            return "set", parts[1], parts[2]
        if parts[0] == "clear" and len(parts) in {1, 2}:
            return "clear", parts[1] if len(parts) == 2 else "", ""
        return "invalid", "", ""

    def _permission_command_names(self) -> set[str]:
        return set(COMMANDS) | {"permission"}

    def _permission_group_map(self) -> dict[str, dict[str, str]]:
        groups = getattr(self.cfg, "command_groups", None)
        if isinstance(groups, dict):
            return groups
        groups = {}
        setattr(self.cfg, "command_groups", groups)
        return groups

    def _permission_group_id(self, group_id: str) -> str:
        return str(group_id).strip().lower()

    def _permission_override(self, group_id: str, command: str) -> str | None:
        group = self._permission_group_map().get(self._permission_group_id(group_id))
        if not isinstance(group, dict):
            return None
        value = group.get(command)
        if value is None:
            return None
        return str(value).strip().lower()

    def _permission_global_access(self, command: str) -> str:
        if command == "memory" and "memory" not in self.cfg.commands:
            return "user"
        return str(self.cfg.command_access(command)).strip().lower()

    def _permission_effective_access(self, group_id: str | None, command: str) -> str:
        global_access = self._permission_global_access(command)
        if group_id is None:
            return global_access
        override = self._permission_override(group_id, command)
        if override is not None:
            return override
        if command == "memory" and "memory" not in self.cfg.commands:
            return global_access
        try:
            return str(self.cfg.command_access(command, group_id)).strip().lower()
        except TypeError:
            return global_access

    def _permission_view_reply(self, ev: ChatEvent) -> str:
        group_id = self._permission_group_id(ev.chat_id) if ev.is_group else None
        scope = (
            f"当前群 {group_id}"
            if group_id is not None
            else "私聊；群级覆盖仅适用于群聊"
        )
        lines = [f"命令权限（{scope}）："]
        group_label = "-"
        for command in sorted(self._permission_command_names()):
            global_access = self._permission_global_access(command)
            override = self._permission_override(group_id, command) if group_id else None
            effective = self._permission_effective_access(group_id, command)
            shown_override = override or group_label
            lines.append(
                f"/{command}：全局 {global_access}，本群 {shown_override}，生效 {effective}"
            )
        lines.append("说明：user=已授权用户，owner=群 owner，disabled=禁用。")
        return "\n".join(lines)

    def _update_permission_override(
        self,
        group_id: str,
        command: str,
        access: str,
        *,
        clear: bool,
    ) -> str:
        groups = self._permission_group_map()
        previous = deepcopy(groups)
        normalized_group_id = self._permission_group_id(group_id)
        if clear:
            if command:
                group = groups.get(normalized_group_id)
                if isinstance(group, dict):
                    group.pop(command, None)
                    if not group:
                        groups.pop(normalized_group_id, None)
            else:
                groups.pop(normalized_group_id, None)
        else:
            groups.setdefault(normalized_group_id, {})[command] = access

        try:
            write_command_access_to_config(self.config_path, self.cfg.commands, groups)
        except OSError:
            groups.clear()
            groups.update(previous)
            logger.exception("permission persistence failed")
            return "[error] permission 写入失败，已恢复之前设置"

        if clear:
            if command:
                return f"已清除本群 /{command} 权限覆盖。"
            return "已清除本群全部命令权限覆盖。"
        return f"已将本群 /{command} 权限设为 {access}。"

    def _parse_permission_command(self, text: str) -> ParsedCommand | None:
        t = text.strip()
        while t.startswith("@"):
            parts = t.split(maxsplit=1)
            if len(parts) < 2:
                return None
            t = parts[1].strip()
        if not t.startswith("/"):
            return None
        parts = t[1:].split(maxsplit=1)
        if not parts or parts[0].lower() != "permission":
            return None
        args = parts[1] if len(parts) == 2 else ""
        return ParsedCommand(name="permission", args=args, raw=t)  # type: ignore[arg-type]

    def _parse_mode_args(self, args: str) -> tuple[str, str]:
        parts = args.strip().lower().split()
        if not parts or parts == ["show"]:
            return "show", ""
        if len(parts) == 2 and parts[0] == "set":
            return "set", parts[1]
        if parts == ["clear"]:
            return "clear", ""
        return "invalid", ""

    def _set_group_mode(self, group_id: str, mode: str) -> str:
        had_previous = group_id in self.cfg.mention_modes.groups
        previous = self.cfg.mention_modes.groups.get(group_id)
        self.cfg.mention_modes.groups[group_id] = mode
        try:
            write_mention_modes_to_config(self.config_path, self.cfg.mention_modes)
        except OSError:
            if had_previous and previous is not None:
                self.cfg.mention_modes.groups[group_id] = previous
            else:
                self.cfg.mention_modes.groups.pop(group_id, None)
            logger.exception("mention mode persistence failed")
            return "[error] mode 写入失败"
        if mode == "chat":
            detail = "@我时会先经过闲聊判定。"
        else:
            detail = f"@我时会直接进入 {mode}，不再经过闲聊判定。"
        return f"已将本群无命令 @ 的默认模式设为 {mode}。{detail}"

    def _clear_group_mode(self, group_id: str) -> str:
        had_previous = group_id in self.cfg.mention_modes.groups
        previous = self.cfg.mention_modes.groups.pop(group_id, None)
        try:
            write_mention_modes_to_config(self.config_path, self.cfg.mention_modes)
        except OSError:
            if had_previous and previous is not None:
                self.cfg.mention_modes.groups[group_id] = previous
            logger.exception("mention mode persistence failed")
            return "[error] mode 写入失败"
        default = self.cfg.mention_modes.default
        return f"已清除本群单独设置，默认模式恢复为 {default}。"

    def _mode_view_reply(self, group_id: str) -> str:
        mode = self.cfg.mention_mode_for_group(group_id)
        source = "本群单独设置" if group_id in self.cfg.mention_modes.groups else "全局默认"
        return f"本群无命令 @ 的默认模式：{mode}（{source}）。显式命令不受影响。"

    def _configure_outgoing_resources(self, job: Job) -> None:
        if job.cmd not in {"task", "code"} or not self.cfg.resources.enabled:
            return
        workspace = Path(self.cfg.agent.default_workspace).expanduser().resolve(strict=False)
        if not self.cfg.is_workspace_allowed(str(workspace)):
            return
        outbox = (workspace / self.cfg.resources.root / "outgoing" / self._safe_job_id(job.id)).resolve(
            strict=False
        )
        try:
            outbox.relative_to(workspace)
        except ValueError:
            return
        outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        outbox_stat = outbox.lstat()
        job.allow_outgoing_resources = True
        job.outgoing_dir = str(outbox)
        job.outgoing_dir_relative = outbox.relative_to(workspace).as_posix()
        job.outgoing_token = secrets.token_urlsafe(12)
        job.outgoing_dir_dev = outbox_stat.st_dev
        job.outgoing_dir_ino = outbox_stat.st_ino
        self._outgoing_jobs[job.event.id] = job
        sending = workspace / self.cfg.resources.root / "sending" / self._safe_job_id(job.id)
        protected_paths = (outbox, sending)
        self._protected_storage_paths[job.id] = protected_paths
        for path in protected_paths:
            self.storage_maintainer.protect_path(path, subtree=True)

    def _safe_job_id(self, job_id: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in job_id).strip(
            ".-"
        )
        return safe[:64] or "job"

    # -- reboot notification (persist chat destination across restarts) --

    _REBOOT_NOTIFY_FILE = Path("data") / ".reboot-notify.json"

    def _write_reboot_notification(self, ev: ChatEvent) -> None:
        import json as _json

        payload = {
            "chat_id": ev.chat_id,
            "is_group": ev.is_group,
        }
        try:
            self._REBOOT_NOTIFY_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._REBOOT_NOTIFY_FILE.write_text(
                _json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    async def _send_reboot_complete(self) -> None:
        import json as _json

        try:
            data = _json.loads(
                self._REBOOT_NOTIFY_FILE.read_text(encoding="utf-8"),
            )
        except (OSError, ValueError):
            return
        finally:
            try:
                self._REBOOT_NOTIFY_FILE.unlink(missing_ok=True)
            except OSError:
                pass

        chat_id: str = data.get("chat_id", "")
        is_group: bool = data.get("is_group", False)
        if not chat_id:
            return
        try:
            await self._send_text(chat_id, is_group, "重启完毕。", "")
        except Exception:
            logger.warning("failed to send reboot-complete notification", exc_info=True)

    def _build_resource_manager(self, cfg: BridgeConfig) -> ResourceManager:
        self.transcriber, resources = self._resource_manager_parts(cfg)
        return resources

    def _resource_manager_parts(
        self,
        cfg: BridgeConfig,
    ) -> tuple[WhisperRunner | None, ResourceManager]:
        transcriber = (
            WhisperRunner(cfg.whisper)
            if cfg.whisper.enabled and cfg.whisper.binary and cfg.whisper.model
            else None
        )
        record_url = getattr(self.adapter, "resolve_record_url", None)
        resources = ResourceManager(
            cfg,
            record_url=record_url if callable(record_url) else None,
            transcriber=transcriber,
        )
        return transcriber, resources

    async def run(self) -> None:
        logger.info("loading config, echo_only=%s", self.echo_only)
        try:
            if not self.echo_only:
                self.policy = Policy(self.cfg, self._agent_runner)
                if self.cfg.scheduler.enabled:
                    self.scheduler.initialize()
                await self._initialize_long_term_memory()
            try:
                await self.storage_maintainer.start()
            except Exception as exc:  # noqa: BLE001 - maintenance cannot block bridge startup
                logger.warning(
                    "storage maintenance startup failed error=%s",
                    type(exc).__name__,
                )
            await self.adapter.start(self._handle)
            if hasattr(self.adapter, "set_on_connected"):
                self.adapter.set_on_connected(self._send_reboot_complete)
            if not self.echo_only:
                await self.scheduler.start()
            await asyncio.Future()  # run forever
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await self.scheduler.stop()
            except Exception as exc:  # noqa: BLE001 - continue the shutdown sequence
                logger.warning("scheduler shutdown failed error=%s", type(exc).__name__)
            for task in list(self._reply_tasks):
                task.cancel()
            for task in list(self._heartbeat_tasks):
                task.cancel()
            for task in list(self._schedule_parse_heartbeats.values()):
                task.cancel()
            if self._reply_tasks:
                await asyncio.gather(*self._reply_tasks, return_exceptions=True)
            if self._heartbeat_tasks:
                await asyncio.gather(*self._heartbeat_tasks, return_exceptions=True)
            try:
                await self._drain_artifact_repair_tasks()
            except Exception as exc:  # noqa: BLE001 - continue the shutdown sequence
                logger.warning("artifact repair shutdown failed error=%s", type(exc).__name__)
            if self._schedule_parse_heartbeats:
                await asyncio.gather(
                    *self._schedule_parse_heartbeats.values(),
                    return_exceptions=True,
                )
            self._schedule_parse_heartbeats.clear()
            try:
                await self.proactive.stop()
            except Exception as exc:  # noqa: BLE001 - continue the shutdown sequence
                logger.warning("proactive shutdown failed error=%s", type(exc).__name__)
            try:
                await self.adapter.stop()
            except Exception as exc:  # noqa: BLE001 - continue the shutdown sequence
                logger.warning("onebot shutdown failed error=%s", type(exc).__name__)
            try:
                await self.storage_maintainer.stop()
            except Exception as exc:  # noqa: BLE001 - continue the shutdown sequence
                logger.warning("storage maintenance shutdown failed error=%s", type(exc).__name__)
            try:
                await self._shutdown_long_term_memory()
            except Exception as exc:  # noqa: BLE001 - continue the shutdown sequence
                logger.warning("long-term memory shutdown failed error=%s", type(exc).__name__)

    async def _agent_runner(self, job: Job) -> str:
        self._cancel_memory_review_for_interactive()
        async with self.storage_gate.activity():
            return await self._agent_runner_inner(job)

    async def _agent_runner_inner(self, job: Job) -> str:
        cmd = job.cmd
        args = job.args
        ev = job.event
        if cmd == "schedule":
            outcome = await self.schedule_nl_parser.parse(
                args,
                mentions=self._schedule_parse_mentions.get(job.id, ()),
                require_safety_review=self._schedule_parse_safety_required.get(job.id, False),
            )
            self._schedule_parse_outcomes[job.id] = outcome
            return "parsed"
        if cmd == "search":
            return await self.search.search(args)
        history = self.memory.format_history(ev) if self.cfg.memory.enabled else ""
        ambient_context = self._ambient_context_for(cmd, args or ev.text, ev)
        self_knowledge = build_prompt_self_knowledge(self.cfg, ev)
        resource_context = ""
        prepared_resources = ()
        try:
            if cmd in {"ask", "plan", "task", "code"}:
                prepared_resources = await self.resources.prepare(ev)
                resource_context = format_resource_context(prepared_resources)
            outgoing_resource_context = ""
            outgoing_job = self._outgoing_jobs.get(ev.id)
            if cmd in {"task", "code"} and outgoing_job:
                outgoing_resource_context = self._format_outgoing_resource_context(outgoing_job)
            runtime_reference_base = ""
            if cmd in {"task", "code"}:
                runtime_reference_base = self._prepare_runtime_skill_bundle()
            schedule_context = self._schedule_prompt_context(job)
            long_term_memory = self._long_term_context_for(ev, args or ev.text)
            prompt = build_agent_prompt(
                cmd,
                args or ev.text,
                ev,
                history=history,
                ambient_context=ambient_context,
                self_knowledge=self_knowledge,
                resource_context=resource_context,
                outgoing_resource_context=outgoing_resource_context,
                profile_prompt=select_profile_prompt(self.cfg, ev),
                long_term_memory=long_term_memory,
                runtime_reference_base=runtime_reference_base,
                schedule_context=schedule_context,
            )
            ws = self.cfg.agent.default_workspace
            if cmd == "shell":
                return "[error] shell command is not implemented"
            agent_mode = "code" if cmd == "code" else "plan" if cmd == "plan" else "task" if cmd == "task" else "ask"
            model = self._agent_model_for(cmd)
            progress = self._progress_callback_for(job) if cmd in {"task", "code"} else None
            return await run_agent(
                self.agent,
                prompt,
                ws,
                agent_mode,
                model=model,
                progress=progress,
                trace_id=job.id,
                redact_extra=self._agent_trace_redaction_values(job, long_term_memory),
            )
        finally:
            self.resources.cleanup_prepared(prepared_resources)

    def _long_term_context_for(self, ev: ChatEvent, query: str) -> str:
        scope = exact_memory_scope(
            is_group=ev.is_group,
            chat_id=ev.chat_id,
            sender_id=ev.sender_id,
        )
        mentions = tuple(
            dict.fromkeys(
                str(segment.qq)
                for segment in ev.segments
                if segment.type in {"mention", "at"}
                and segment.qq
                and str(segment.qq) != str(self.cfg.bot.self_id or "")
            )
        )
        quoted_sender = trusted_reply_sender_id(ev.reply)
        return self._retrieve_long_term_context(
            scope,
            ev.sender_id,
            mentions,
            quoted_sender,
            query,
        )

    def _retrieve_long_term_context(
        self,
        scope: MemoryScope,
        current_sender: str,
        real_mentions: tuple[str, ...],
        quoted_sender: str | None,
        query: str,
    ) -> str:
        retriever = self.long_term_memory_retriever
        if retriever is None:
            return ""
        try:
            return retriever.retrieve(
                scope,
                current_sender,
                real_mentions,
                quoted_sender,
                query,
            )
        except Exception as exc:  # noqa: BLE001 - memory lookup must not block chat
            logger.warning(
                "long-term memory retrieval failed scope=%s error=%s",
                scope.kind,
                type(exc).__name__,
            )
            return ""

    def _schedule_prompt_context(self, job: Job) -> str:
        if job.source != "schedule" or job.scheduled_for is None:
            return ""
        planned = datetime.fromtimestamp(job.scheduled_for, tz=UTC).astimezone(
            ZoneInfo(self.cfg.scheduler.timezone)
        )
        return "\n".join(
            [
                f"定时任务 ID：{job.schedule_id or 'unknown'}",
                f"计划触发时间：{planned.strftime('%Y-%m-%d %H:%M:%S')} {self.cfg.scheduler.timezone}",
                f"本次运行 ID：{job.schedule_run_id}",
                "这是用户此前明确设置、现在到点触发的任务；请执行当前用户消息，不要重新创建定时任务。",
            ]
        )

    def _ambient_context_for(self, cmd: str, text: str, ev: ChatEvent) -> str:
        if not self.cfg.ambient_memory.enabled or not ev.is_group:
            return ""
        if self.cfg.ambient_memory.allowed_groups and ev.chat_id not in self.cfg.ambient_memory.allowed_groups:
            return ""
        if not self.cfg.is_group_allowed(ev.chat_id):
            return ""
        if cmd in {"ask", "plan", "task", "code"}:
            return self.ambient_memory.format_context(ev)
        return ""

    def _proactive_ambient_context(self, chat_id: str, now: int) -> str:
        if not self.cfg.ambient_memory.enabled:
            return ""
        if self.cfg.ambient_memory.allowed_groups and chat_id not in self.cfg.ambient_memory.allowed_groups:
            return ""
        if not self.cfg.is_group_allowed(chat_id):
            return ""
        ev = ChatEvent(
            id="",
            platform="qq",
            chat_id=chat_id,
            sender_id="",
            is_group=True,
            mentioned_bot=False,
            text="",
            timestamp=now,
        )
        return self.ambient_memory.format_context(ev, now=now)

    def _format_outgoing_resource_context(self, job: Job) -> str:
        if not job.outgoing_dir or not job.outgoing_token:
            return ""
        _outbox_absolute, outbox_rel = self._outgoing_path_values(job)
        if not outbox_rel:
            return ""
        token = job.outgoing_token
        return "\n".join(
            [
                f"可发送资源目录：{outbox_rel}",
                f"资源发送令牌：{token}",
                f"发送图片指令：QQBOT_SEND_IMAGE: {token} {outbox_rel}/image.png",
                f"发送文件指令：QQBOT_SEND_FILE: {token} {outbox_rel}/file.pdf",
                f"发送人声语音指令：QQBOT_SEND_VOICE: {token} {outbox_rel}/voice.wav duration=12",
                f"发送泛音频文件指令：QQBOT_SEND_AUDIO: {token} {outbox_rel}/audio.mp3",
                (
                    "限制：只能发送本次任务在上述目录内生成的文件；"
                    f"最多 {self.cfg.resources.max_items} 个，单个不超过 {self.cfg.resources.max_bytes} 字节；"
                    "QQ语音只用于生成的人声/短语音，必须提供真实 duration 且不超过60秒；"
                    "泛音频、音乐、较长音频请按文件发送。"
                ),
            ]
        )

    def _outgoing_path_values(self, job: Job) -> tuple[str, str]:
        if not job.outgoing_dir:
            return "", ""
        workspace = Path(self.cfg.agent.default_workspace).expanduser().resolve(strict=False)
        outbox = Path(job.outgoing_dir).expanduser().resolve(strict=False)
        outbox_rel = job.outgoing_dir_relative or ""
        if not outbox_rel:
            try:
                outbox_rel = outbox.relative_to(workspace).as_posix()
            except ValueError:
                outbox_rel = ""
        return outbox.as_posix(), outbox_rel

    def _outgoing_redaction_values(self, job: Job) -> tuple[str, ...]:
        outbox_absolute, outbox_relative = self._outgoing_path_values(job)
        return tuple(
            value
            for value in (job.outgoing_token or "", outbox_absolute, outbox_relative)
            if value
        )

    def _agent_trace_redaction_values(
        self, job: Job, long_term_memory: str
    ) -> tuple[str, ...]:
        values = list(self._outgoing_redaction_values(job))
        if long_term_memory:
            values.append(long_term_memory)
            for line in long_term_memory.splitlines():
                match = re.fullmatch(
                    r"- \[category=[^\]\r\n]+\]\[subject=(?:group|user):[^\]\r\n]+\] (.+)",
                    line,
                )
                if match and match.group(1):
                    values.append(match.group(1))
        return tuple(dict.fromkeys(value for value in values if value))

    def _prepare_runtime_skill_bundle(self) -> str:
        workspace = Path(self.cfg.agent.default_workspace).expanduser().resolve(strict=False)
        if not self.cfg.is_workspace_allowed(str(workspace)):
            return ""
        try:
            return prepare_runtime_skill_bundle(workspace, self.cfg.resources.root)
        except (OSError, ValueError):
            logger.exception("runtime skill bundle preparation failed")
            return ""

    def _agent_model_for(self, cmd: str) -> str | None:
        if cmd == "ask":
            return self.cfg.agent.chat_model or None
        if cmd in {"plan", "task", "code"}:
            return self.cfg.agent.task_model or None
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--echo-only", action="store_true")
    args = parser.parse_args()

    cfg = BridgeConfig.load(Path(args.config))
    if cfg.log_level:
        logging.getLogger().setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))

    if not cfg.bot.self_id:
        logger.warning("bot.self_id empty; mention detection may be loose")

    app = App(cfg, echo_only=args.echo_only, config_path=Path(args.config))
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logger.info("bye")


if __name__ == "__main__":
    main()
