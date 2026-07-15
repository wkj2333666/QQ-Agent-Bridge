"""Config loading and policy data structures. Safe defaults."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


MENTION_MODE_OPTIONS: tuple[str, ...] = ("ask", "plan", "task")
MENTION_MODES = frozenset(MENTION_MODE_OPTIONS)


@dataclass
class OneBotConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/onebot"
    access_token: str = ""


@dataclass
class AgentConfig:
    runtime: str = ""
    binary: str = ""
    command: dict[str, list[str]] = field(default_factory=dict)
    default_workspace: str = "/opt/workspaces"
    env_runner: str = "micromamba"
    env_name: str = "base"
    require_env: bool = True
    use_bwrap: bool = True
    bwrap_binary: str = "bwrap"
    force_task_tools: bool = True
    sandbox_home: str = "/tmp/qq-agent-bridge/agent-home"
    chat_model: str = "auto"
    task_model: str = "composer"
    env_allowlist: list[str] = field(default_factory=lambda: ["PATH", "HOME", "USER"])
    max_concurrent_jobs: int = 2
    max_runtime_seconds: int = 300
    max_output_chars: int = 4000


@dataclass
class BotConfig:
    self_id: str = ""
    mention_name: str = ""
    reply_chunk_delay_seconds: float = 0.2


@dataclass
class MemoryConfig:
    enabled: bool = True
    max_messages: int = 32
    max_chars: int = 6000


@dataclass
class AmbientMemoryConfig:
    enabled: bool = True
    allowed_groups: list[str] = field(default_factory=list)
    max_messages: int = 8
    max_chars: int = 1200
    max_message_chars: int = 180
    max_age_seconds: int = 900
    min_chars: int = 4
    ignored_prefixes: list[str] = field(default_factory=lambda: ["/", "／", "!", "！"])


@dataclass
class ResourcesConfig:
    enabled: bool = True
    root: str = "downloads/qq-agent-bridge"
    max_items: int = 4
    max_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 20 * 1024 * 1024
    cache_enabled: bool = True
    cache_ttl_seconds: int = 600
    cache_max_items: int = 4
    allowed_kinds: list[str] = field(
        default_factory=lambda: ["image", "file", "audio", "voice", "video", "url", "forward"]
    )


@dataclass
class ProgressConfig:
    enabled: bool = True
    first_heartbeat_seconds: int = 30
    heartbeat_seconds: int = 45
    max_heartbeat_messages: int = 6
    min_progress_interval_seconds: int = 8
    max_progress_messages: int = 8
    max_progress_chars: int = 240


@dataclass
class ProactiveConfig:
    enabled: bool = True
    debug: bool = False
    allowed_groups: list[str] = field(default_factory=list)
    batch_seconds: float = 8.0
    min_messages: int = 2
    max_batch_messages: int = 8
    cooldown_seconds: int = 16
    quiet_after_bot_seconds: int = 16
    max_per_hour: int = 180
    max_prompt_chars: int = 1200
    max_reply_chars: int = 160
    max_reply_messages: int = 3
    reply_message_delay_seconds: float = 0.6
    model: str = "auto"
    blacklist_keywords: list[str] = field(
        default_factory=lambda: ["别插嘴", "不要插嘴", "闭嘴", "机器人别说话"]
    )
    ignored_prefixes: list[str] = field(default_factory=lambda: ["/"])


@dataclass
class SchedulerConfig:
    enabled: bool = False
    database_path: str = "data/schedules.sqlite3"
    timezone: str = "Asia/Shanghai"
    natural_language_enabled: bool = True
    natural_language_model: str = "auto"
    natural_language_timeout_seconds: int = 60
    natural_language_progress_seconds: int = 15
    allow_private_users: bool = True
    allow_unbounded: bool = True
    min_interval_seconds: int = 60
    max_schedules_per_chat: int = 20
    max_concurrent_runs: int = 4
    max_run_history_per_schedule: int = 100
    max_occurrences: int = 100
    max_payload_chars: int = 2000
    misfire_grace_seconds: int = 300
    max_consecutive_failures: int = 5
    debug: bool = False


@dataclass
class ProfileConfig:
    default: str = ""
    groups: dict[str, str] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)


@dataclass
class MentionModeConfig:
    default: str = "ask"
    groups: dict[str, str] = field(default_factory=dict)


@dataclass
class BridgeConfig:
    owners: list[str] = field(default_factory=list)
    allowed_users: list[str] = field(default_factory=list)
    allowed_groups: list[str] = field(default_factory=list)
    workspaces: dict[str, bool] = field(default_factory=dict)
    commands: dict[str, bool] = field(default_factory=dict)
    dangerous_requires_confirm: bool = True
    max_runtime_seconds: int = 300
    max_output_chars: int = 4000
    max_finished_jobs: int = 200
    max_seen_messages: int = 1000
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    ambient_memory: AmbientMemoryConfig = field(default_factory=AmbientMemoryConfig)
    resources: ResourcesConfig = field(default_factory=ResourcesConfig)
    progress: ProgressConfig = field(default_factory=ProgressConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    profiles: ProfileConfig = field(default_factory=ProfileConfig)
    mention_modes: MentionModeConfig = field(default_factory=MentionModeConfig)
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: Path | str = "config.yaml") -> BridgeConfig:
        p = Path(path)
        if not p.exists():
            return cls()  # all deny by default
        raw: dict[str, Any] = yaml.safe_load(p.read_text()) or {}
        onebot = OneBotConfig(**raw.get("onebot", {}))
        agent = AgentConfig(**raw.get("agent", {}))
        botc = BotConfig(**raw.get("bot", {}))
        memory = MemoryConfig(**raw.get("memory", {}))
        ambient_memory = AmbientMemoryConfig(**raw.get("ambient_memory", {}))
        resources = ResourcesConfig(**raw.get("resources", {}))
        progress = ProgressConfig(**raw.get("progress", {}))
        proactive = ProactiveConfig(**raw.get("proactive", {}))
        scheduler = SchedulerConfig(**raw.get("scheduler", {}))
        profiles = _load_profiles(raw.get("profiles", {}))
        mention_modes = _load_mention_modes(raw.get("mention_modes", {}))
        return cls(
            owners=raw.get("owners", []),
            allowed_users=raw.get("allowed_users", []),
            allowed_groups=raw.get("allowed_groups", []),
            workspaces=raw.get("workspaces", {}),
            commands=raw.get("commands", {}),
            dangerous_requires_confirm=raw.get("dangerous_requires_confirm", True),
            max_runtime_seconds=raw.get("max_runtime_seconds", 300),
            max_output_chars=raw.get("max_output_chars", 4000),
            max_finished_jobs=raw.get("max_finished_jobs", 200),
            max_seen_messages=raw.get("max_seen_messages", 1000),
            onebot=onebot,
            agent=agent,
            bot=botc,
            memory=memory,
            ambient_memory=ambient_memory,
            resources=resources,
            progress=progress,
            proactive=proactive,
            scheduler=scheduler,
            profiles=profiles,
            mention_modes=mention_modes,
            log_level=raw.get("log_level", "INFO"),
        )

    def is_owner(self, uid: str) -> bool:
        return uid in self.owners

    def is_user_allowed(self, uid: str) -> bool:
        return uid in self.allowed_users or self.is_owner(uid)

    def is_group_allowed(self, gid: str) -> bool:
        return gid in self.allowed_groups

    def is_workspace_allowed(self, ws: str) -> bool:
        target = Path(ws).expanduser().resolve(strict=False)
        for allowed, enabled in self.workspaces.items():
            if not enabled:
                continue
            base = Path(allowed).expanduser().resolve(strict=False)
            if target == base:
                return True
            try:
                target.relative_to(base)
            except ValueError:
                continue
            return True
        return False

    def is_command_allowed(self, name: str) -> bool:
        return self.commands.get(name, False)

    def mention_mode_for_group(self, gid: str) -> str:
        return self.mention_modes.groups.get(str(gid), self.mention_modes.default)

    def effective_max_runtime(self) -> int:
        return min(self.max_runtime_seconds, self.agent.max_runtime_seconds)

    def effective_max_chars(self) -> int:
        return min(self.max_output_chars, self.agent.max_output_chars)


def _load_profiles(raw: Any) -> ProfileConfig:
    if not isinstance(raw, dict):
        return ProfileConfig()
    return ProfileConfig(
        default=str(raw.get("default", "") or "").rstrip(),
        groups=_string_map(raw.get("groups", {})),
        users=_string_map(raw.get("users", {})),
    )


def _load_mention_modes(raw: Any) -> MentionModeConfig:
    if not isinstance(raw, dict):
        return MentionModeConfig()
    default = _mention_mode(raw.get("default")) or "ask"
    groups: dict[str, str] = {}
    raw_groups = raw.get("groups", {})
    if isinstance(raw_groups, dict):
        for key, value in raw_groups.items():
            mode = _mention_mode(value)
            if mode:
                groups[str(key)] = mode
    return MentionModeConfig(default=default, groups=groups)


def _mention_mode(value: Any) -> str | None:
    mode = str(value or "").strip().lower()
    return mode if mode in MENTION_MODES else None


def _string_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value or "").rstrip() for key, value in raw.items()}
