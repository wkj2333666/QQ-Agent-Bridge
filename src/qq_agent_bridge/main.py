"""Main entry: glue OneBot, policy, and agent runtime."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import logging
import secrets
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from .attachment_cache import AttachmentCache
from .agent_runtime import build_agent_adapter
from .config import BridgeConfig
from .memory import ConversationMemory, GroupAmbientMemory
from .onebot import OneBotAdapter
from .output_guard import guard_internal_output
from .outgoing_resources import collect_outgoing_resources
from .policy import Job, Policy
from .prompting import build_agent_prompt, select_profile_prompt
from .profile_store import write_profiles_to_config
from .proactive import MentionDecision, ProactiveSpeaker
from .progress import ProgressReporter
from .redactor import redact
from .resources import ResourceManager, format_resource_context
from .runtime_skill import prepare_runtime_skill_bundle
from .schedule_parser import (
    NaturalLanguageScheduleParser,
    NaturalScheduleOutcome,
    ScheduleParseError,
    parse_explicit_schedule,
)
from .schedule_store import ScheduleStore
from .scheduler import Schedule, ScheduleExecutionResult, ScheduleRun, Scheduler
from .self_knowledge import build_help_reply, build_prompt_self_knowledge, maybe_self_reply
from .types import ChatEvent, ParsedCommand
from .workspace_search import WorkspaceSearch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("qq-bridge")


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
        self.agent = build_agent_adapter(cfg)
        self.cursor = self.agent  # compatibility alias for older tests/extensions
        self.search = WorkspaceSearch(cfg)
        self.resources = ResourceManager(cfg)
        self.memory = ConversationMemory(cfg.memory.max_messages, cfg.memory.max_chars)
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
        self._outgoing_jobs: dict[str, Job] = {}
        self._progress_reporters: dict[str, ProgressReporter] = {}
        self._schedule_parse_outcomes: dict[str, NaturalScheduleOutcome] = {}
        self._schedule_parse_mentions: dict[str, tuple[str, ...]] = {}
        self._schedule_parse_heartbeats: dict[str, asyncio.Task[None]] = {}
        self.schedule_database_path = self._schedule_database_path(cfg)
        self._scheduler_restart_required = False
        self.schedule_store = ScheduleStore(self.schedule_database_path)
        self.schedule_nl_parser = NaturalLanguageScheduleParser(cfg, self.agent)
        self.scheduler = Scheduler(
            cfg.scheduler,
            self.schedule_store,
            self._execute_schedule,
            ready=lambda: bool(getattr(self.adapter, "is_connected", lambda: False)()),
        )
        self.proactive = ProactiveSpeaker(
            cfg,
            self.agent,
            self._send_proactive,
            ambient_context=self._proactive_ambient_context,
            remember=self._remember_proactive_exchange,
        )

    async def _handle(self, ev: ChatEvent) -> None:
        if self._is_self_event(ev):
            return
        if ev.is_group and not ev.mentioned_bot:
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
        has_content = bool(ev.text.strip() or ev.resources)
        default_command = "ask" if has_content and not ev.is_group else None
        preauthorized_command: str | None = None
        if not parsed and ev.is_group and ev.mentioned_bot and has_content:
            if ev.resources:
                default_command = "ask"
            else:
                ok, reason = self.policy.allow(ev, "ask")
                if not ok:
                    if reason not in {"duplicate", "no-mention"}:
                        await self._send_text(ev.chat_id, ev.is_group, f"[denied] {reason}", ev.id)
                    return
                decision = await self.proactive.decide_mention(ev)
                if decision.action == "ask":
                    parsed = self.policy.parse(ev.text, default_command="ask") or self.policy.parse(
                        "", default_command="ask"
                    )
                    preauthorized_command = "ask"
                elif decision.action == "chat":
                    if self.proactive.can_send_chat_interjection(ev.chat_id):
                        await self._send_mention_decision(ev, decision)
                        self.proactive.record_chat_interjection(ev.chat_id)
                    await self._cleanup_policy()
                    return
                else:
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
            txt = build_help_reply(self.cfg, ev)
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
            await self._send_text(ev.chat_id, ev.is_group, "已清空当前会话记忆和最近群聊背景", ev.id)
            await self._cleanup_policy()
            return

        if parsed.name == "reload":
            okc, msg = await self._reload_config()
            await self._send_text(ev.chat_id, ev.is_group, msg, ev.id)
            if okc:
                await self._cleanup_policy()
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
        return reporter.send_progress if reporter else None

    async def _cleanup_policy(self) -> None:
        if self.policy:
            await self.policy.cleanup()

    async def _reply_when_done(self, job: Job) -> None:
        if not job.task:
            return
        try:
            result = await job.task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current and current.cancelling():
                raise
            if job.cmd == "schedule":
                self._stop_schedule_parse_heartbeat(job.id)
                self._schedule_parse_mentions.pop(job.id, None)
                self._schedule_parse_outcomes.pop(job.id, None)
            reporter = self._progress_reporters.pop(job.id, None)
            if reporter:
                reporter.stop()
            self._outgoing_jobs.pop(job.event.id, None)
            await self._cleanup_policy()
            return
        if job.cmd == "schedule":
            await self._finish_schedule_parse_job(job, result)
            await self._cleanup_policy()
            return
        if job.allow_outgoing_resources and job.outgoing_dir and job.outgoing_token:
            clean_result, outgoing, warnings = collect_outgoing_resources(
                result,
                self.cfg,
                outbox_dir=job.outgoing_dir,
                token=job.outgoing_token,
                job_id=job.id,
                expected_outbox=(
                    (job.outgoing_dir_dev, job.outgoing_dir_ino)
                    if job.outgoing_dir_dev is not None and job.outgoing_dir_ino is not None
                    else None
                ),
            )
        else:
            clean_result, outgoing, warnings = result, (), []
        if self.cfg.memory.enabled and job.cmd in {"ask", "plan", "task", "code"} and clean_result.strip():
            self.memory.append_exchange(job.event, job.args or job.event.text, clean_result)
        reply_text = clean_result
        if warnings:
            warning_text = "\n".join(warnings)
            reply_text = f"{reply_text}\n{warning_text}".strip()
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
        for i, resource in enumerate(outgoing):
            echo = f"{ev.id}-r{i}"
            if resource.kind == "image":
                await self.adapter.send_image(ev.chat_id, ev.is_group, resource.path, echo)
            elif resource.kind == "voice":
                await self.adapter.send_voice(ev.chat_id, ev.is_group, resource.path, echo)
            elif resource.kind == "file":
                await self.adapter.send_file(ev.chat_id, ev.is_group, resource.path, echo)
        reporter = self._progress_reporters.pop(job.id, None)
        if reporter:
            reporter.stop()
        self._outgoing_jobs.pop(job.event.id, None)
        await self._cleanup_policy()

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
        try:
            cfg = BridgeConfig.load(self.config_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("config reload failed")
            return False, f"[error] 配置重载失败：{type(exc).__name__}"

        self.cfg = cfg
        self.agent = build_agent_adapter(cfg)
        self.cursor = self.agent
        self.search = WorkspaceSearch(cfg)
        self.resources = ResourceManager(cfg)
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
        old_schedule_path = self.schedule_database_path
        new_schedule_path = self._schedule_database_path(cfg)
        self.schedule_nl_parser = NaturalLanguageScheduleParser(cfg, self.agent)
        self.scheduler.reload_config(cfg.scheduler)
        schedule_note = ""
        if new_schedule_path != old_schedule_path:
            self._scheduler_restart_required = True
            await self.scheduler.stop()
            schedule_note = " scheduler.database_path 变更需要重启。"
        else:
            self._scheduler_restart_required = False
            if cfg.scheduler.enabled:
                await self.scheduler.start()
            else:
                await self.scheduler.stop()
        await self.proactive.stop()
        self.proactive = ProactiveSpeaker(
            cfg,
            self.agent,
            self._send_proactive,
            ambient_context=self._proactive_ambient_context,
            remember=self._remember_proactive_exchange,
        )
        return True, f"配置已重载。OneBot 连接参数变更需要重启。{schedule_note}".strip()

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
                self._schedule_help_text(),
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
            schedule = self.schedule_store.resolve_ref(ev.chat_id, ev.is_group, ref)
            text = self._schedule_detail_text(schedule, ev) if schedule else "没有找到这个定时任务。"
            await self._send_text(ev.chat_id, ev.is_group, text, ev.id)
            return
        if not self._can_mutate_schedules(ev):
            reason = "owner-only" if ev.is_group else "private-schedule-disabled"
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
            try:
                schedule = self.scheduler.create(spec, ev)
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
        if not self.cfg.scheduler.natural_language_enabled:
            await self._send_text(
                ev.chat_id,
                ev.is_group,
                "自然语言时间解析当前没有开启，发送 /schedule help 查看结构化写法。",
                ev.id,
            )
            return
        assert self.policy is not None
        command = ParsedCommand(name="schedule", args=raw, raw=f"/schedule {raw}")
        jid, _nonce = self.policy.start_job(ev, command)
        job = self.policy.jobs[jid]
        job.timeout_seconds = max(1, self.cfg.scheduler.natural_language_timeout_seconds)
        job.source = "schedule-parse"
        self._schedule_parse_mentions[jid] = mentions
        await self._send_text(
            ev.chat_id,
            ev.is_group,
            "收到，我正在理解你说的时间和任务内容，稍等一下。",
            f"{ev.id}-schedule-start",
        )
        self.policy.start_job_task(job)
        self._start_schedule_parse_heartbeat(job)
        self._schedule_reply(job)

    async def _handle_schedule_management(self, ev: ChatEvent, action: str, ref: str) -> None:
        schedule = self.schedule_store.resolve_ref(ev.chat_id, ev.is_group, ref)
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

    def _can_mutate_schedules(self, ev: ChatEvent) -> bool:
        if ev.is_group:
            return self.cfg.is_owner(ev.sender_id)
        return self.cfg.scheduler.allow_private_users and self.cfg.is_user_allowed(ev.sender_id)

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

    def _schedule_help_text(self) -> str:
        zone = self.cfg.scheduler.timezone
        return "\n".join(
            [
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
        )

    def _schedule_list_text(self, ev: ChatEvent) -> str:
        schedules = self.schedule_store.list_for_chat(ev.chat_id, ev.is_group)
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
        items = self.schedule_store.list_for_chat(ev.chat_id, ev.is_group)
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
        schedules = self.schedule_store.list_for_chat(ev.chat_id, ev.is_group)
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
        outcome = self._schedule_parse_outcomes.pop(job.id, None)
        if outcome is None:
            if result == "[timeout]":
                text = "时间规则理解超时了，定时任务没有创建。可以说得更明确一点再试。"
            else:
                text = "这次没能理解时间规则，定时任务没有创建。可以发送 /schedule help 查看示例。"
            await self._send_text(job.event.chat_id, job.event.is_group, text, job.event.id)
            return
        if outcome.spec is None:
            text = f"{outcome.clarification}\n没有创建定时任务。"
            await self._send_text(job.event.chat_id, job.event.is_group, text, job.event.id)
            return
        if (
            self._scheduler_restart_required
            or not self.cfg.scheduler.enabled
            or not self.cfg.is_command_allowed("schedule")
        ):
            await self._send_text(
                job.event.chat_id,
                job.event.is_group,
                "时间规则已经理解，但配置刚刚发生变化，定时任务没有创建。",
                job.event.id,
            )
            return
        if not self._can_mutate_schedules(job.event):
            await self._send_text(
                job.event.chat_id,
                job.event.is_group,
                "时间规则已经理解，但当前权限已不允许创建定时任务。",
                job.event.id,
            )
            return
        spec = replace(outcome.spec, mentions=mentions)
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
        return ScheduleExecutionResult("succeeded", job_id=jid)

    def _schedule_execution_allowed(self, schedule: Schedule) -> tuple[bool, str]:
        if not self.cfg.scheduler.enabled:
            return False, "scheduler-disabled"
        if schedule.is_group:
            if not self.cfg.is_group_allowed(schedule.chat_id):
                return False, "group-denied"
            if not self.cfg.is_owner(schedule.creator_id):
                return False, "owner-only"
        elif not self.cfg.scheduler.allow_private_users or not self.cfg.is_user_allowed(
            schedule.creator_id
        ):
            return False, "user-denied"
        if schedule.action in {"ask", "task"} and not self.cfg.is_command_allowed(schedule.action):
            return False, "cmd-disabled"
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
        )

    async def _send_schedule_text(self, schedule: Schedule, text: str, echo: str) -> None:
        if schedule.is_group and schedule.mentions:
            await self.adapter.send_ats(schedule.chat_id, schedule.mentions, text, echo)
            self.proactive.record_bot_send(schedule.chat_id)
            return
        await self._send_text(schedule.chat_id, schedule.is_group, text, echo)

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
            self._set_profile(ev, value.strip())
            return self._persist_profile_reply(ev, updated=True)
        if action == "clear":
            self._clear_profile(ev)
            return self._persist_profile_reply(ev, updated=False)
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
        job.outgoing_token = secrets.token_urlsafe(12)
        job.outgoing_dir_dev = outbox_stat.st_dev
        job.outgoing_dir_ino = outbox_stat.st_ino
        self._outgoing_jobs[job.event.id] = job

    def _safe_job_id(self, job_id: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in job_id)
        return safe[:64] or "job"

    async def run(self) -> None:
        logger.info("loading config, echo_only=%s", self.echo_only)
        if not self.echo_only:
            self.policy = Policy(self.cfg, self._agent_runner)
            if self.cfg.scheduler.enabled:
                self.scheduler.initialize()
        await self.adapter.start(self._handle)
        if not self.echo_only:
            await self.scheduler.start()
        try:
            await asyncio.Future()  # run forever
        except asyncio.CancelledError:
            pass
        finally:
            await self.scheduler.stop()
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
            if self._schedule_parse_heartbeats:
                await asyncio.gather(
                    *self._schedule_parse_heartbeats.values(),
                    return_exceptions=True,
                )
            self._schedule_parse_heartbeats.clear()
            await self.proactive.stop()
            await self.adapter.stop()

    async def _agent_runner(self, job: Job) -> str:
        cmd = job.cmd
        args = job.args
        ev = job.event
        if cmd == "schedule":
            outcome = await self.schedule_nl_parser.parse(
                args,
                mentions=self._schedule_parse_mentions.get(job.id, ()),
            )
            self._schedule_parse_outcomes[job.id] = outcome
            return "parsed"
        if cmd == "search":
            return await self.search.search(args)
        history = self.memory.format_history(ev) if self.cfg.memory.enabled else ""
        ambient_context = self._ambient_context_for(cmd, args or ev.text, ev)
        self_knowledge = build_prompt_self_knowledge(self.cfg, ev)
        resource_context = ""
        if cmd in {"ask", "plan", "task", "code"}:
            resource_context = format_resource_context(await self.resources.prepare(ev))
        outgoing_resource_context = ""
        outgoing_job = self._outgoing_jobs.get(ev.id)
        if cmd in {"task", "code"} and outgoing_job:
            outgoing_resource_context = self._format_outgoing_resource_context(outgoing_job)
        runtime_reference_base = ""
        if cmd in {"task", "code"}:
            runtime_reference_base = self._prepare_runtime_skill_bundle()
        schedule_context = self._schedule_prompt_context(job)
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
            runtime_reference_base=runtime_reference_base,
            schedule_context=schedule_context,
        )
        ws = self.cfg.agent.default_workspace
        if cmd == "shell":
            return "[error] shell command is not implemented"
        agent_mode = "code" if cmd == "code" else "plan" if cmd == "plan" else "task" if cmd == "task" else "ask"
        model = self._agent_model_for(cmd)
        progress = self._progress_callback_for(job) if cmd in {"task", "code"} else None
        kwargs = {"progress": progress} if progress else {}
        return await self.agent.run(prompt, ws, agent_mode, model=model, **kwargs)

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
        workspace = Path(self.cfg.agent.default_workspace).expanduser().resolve(strict=False)
        outbox = Path(job.outgoing_dir).resolve(strict=False)
        try:
            outbox_rel = outbox.relative_to(workspace).as_posix()
        except ValueError:
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
