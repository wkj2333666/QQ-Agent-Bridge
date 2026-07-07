"""Main entry: glue OneBot, policy, and agent runtime."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import logging
import secrets
import sys
from pathlib import Path

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
from .self_knowledge import build_help_reply, build_prompt_self_knowledge, maybe_self_reply
from .types import ChatEvent
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
            st = self.policy.get_status()
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
            jid = parsed.args.split()[0] if parsed.args else ""
            okc = self.policy.cancel(jid, ev.sender_id)
            if okc:
                reporter = self._progress_reporters.pop(jid, None)
                if reporter:
                    reporter.stop()
            await self._send_text(ev.chat_id, ev.is_group, f"stop {jid}: {okc}", ev.id)
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

    def _schedule_reply(self, job: Job) -> None:
        self._start_heartbeat(job)
        task = asyncio.create_task(self._reply_when_done(job))
        self._reply_tasks.add(task)
        task.add_done_callback(self._reply_tasks.discard)

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
            result = "[cancelled]"
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
        await self.proactive.stop()
        self.proactive = ProactiveSpeaker(
            cfg,
            self.agent,
            self._send_proactive,
            ambient_context=self._proactive_ambient_context,
            remember=self._remember_proactive_exchange,
        )
        return True, "配置已重载。OneBot 连接参数变更需要重启。"

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
        await self.adapter.start(self._handle)
        try:
            await asyncio.Future()  # run forever
        except asyncio.CancelledError:
            pass
        finally:
            for task in list(self._reply_tasks):
                task.cancel()
            for task in list(self._heartbeat_tasks):
                task.cancel()
            if self._reply_tasks:
                await asyncio.gather(*self._reply_tasks, return_exceptions=True)
            if self._heartbeat_tasks:
                await asyncio.gather(*self._heartbeat_tasks, return_exceptions=True)
            await self.proactive.stop()
            await self.adapter.stop()

    async def _agent_runner(self, job: Job) -> str:
        cmd = job.cmd
        args = job.args
        ev = job.event
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
        )
        ws = self.cfg.agent.default_workspace
        if cmd == "shell":
            return "[error] shell command is not implemented"
        agent_mode = "code" if cmd == "code" else "plan" if cmd == "plan" else "task" if cmd == "task" else "ask"
        model = self._agent_model_for(cmd)
        progress = self._progress_callback_for(job) if cmd in {"task", "code"} else None
        kwargs = {"progress": progress} if progress else {}
        return await self.agent.run(prompt, ws, agent_mode, model=model, **kwargs)

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
