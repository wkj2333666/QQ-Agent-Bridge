"""Config loading and policy data structures. Safe defaults."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any, Literal

import yaml


MENTION_MODE_OPTIONS: tuple[str, ...] = ("chat", "ask", "plan", "task")
MENTION_MODES = frozenset(MENTION_MODE_OPTIONS)
CommandAccess = Literal["disabled", "user", "owner"]
COMMAND_ACCESS_LEVELS = frozenset({"disabled", "user", "owner"})
# Preserve the authorization implied by the historical commands: true/false format.
LEGACY_OWNER_COMMANDS = frozenset({"code", "shell", "reset", "stop", "approve", "reload", "reboot"})


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
    share_network: bool = False
    bwrap_binary: str = "bwrap"
    force_task_tools: bool = True
    hardened_read_only: bool = False
    log_subprocess_output: bool = True
    sandbox_home: str = "~/.local/state/qq-agent-bridge/agent-home"
    chat_model: str = "auto"
    task_model: str = "composer"
    env_allowlist: list[str] = field(default_factory=lambda: ["PATH", "HOME", "USER"])
    max_concurrent_jobs: int = 2
    max_runtime_seconds: int = 300
    max_output_chars: int = 4000
    trace_enabled: bool = False
    trace_root: str = "runtime/agent-traces"
    trace_max_bytes: int = 5 * 1024 * 1024
    trace_max_line_chars: int = 2000


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
class MemoryReviewConfig:
    message_threshold: int = 40
    minimum_messages: int = 10
    idle_seconds: int = 600
    interval_seconds: int = 21_600
    raw_ttl_seconds: int = 604_800
    max_concurrent: int = 1
    model: str = "auto"
    timeout_seconds: int = 90
    max_attempts: int = 3


@dataclass
class MemoryRetrievalConfig:
    max_items: int = 12
    max_chars: int = 1_500
    minimum_score: float = 0.45


@dataclass
class MemoryDecayConfig:
    enabled: bool = True
    interval_seconds: int = 86_400
    grace_seconds: int = 2_592_000
    dormant_threshold: float = 0.40


@dataclass
class LongTermMemoryConfig:
    enabled: bool = True
    default_scope_enabled: bool = False
    groups: dict[str, bool] = field(default_factory=dict)
    users: dict[str, bool] = field(default_factory=dict)
    database_path: str = "data/long-term-memory.sqlite3"
    review: MemoryReviewConfig = field(default_factory=MemoryReviewConfig)
    retrieval: MemoryRetrievalConfig = field(default_factory=MemoryRetrievalConfig)
    decay: MemoryDecayConfig = field(default_factory=MemoryDecayConfig)


@dataclass
class ResourcesConfig:
    enabled: bool = True
    root: str = "downloads/qq-agent-bridge"
    local_media_roots: list[str] = field(default_factory=list)
    max_items: int = 4
    max_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 20 * 1024 * 1024
    cache_enabled: bool = True
    cache_ttl_seconds: int = 600
    cache_max_items: int = 4
    animation_enabled: bool = True
    animation_ffmpeg_binary: str = "ffmpeg"
    animation_ffprobe_binary: str = "ffprobe"
    animation_max_frames: int = 8
    animation_max_duration_seconds: int = 30
    animation_max_dimension: int = 1024
    animation_max_source_pixels: int = 40_000_000
    animation_timeout_seconds: float = 20.0
    allowed_kinds: list[str] = field(
        default_factory=lambda: ["image", "file", "audio", "voice", "video", "url", "forward"]
    )


@dataclass
class WhisperConfig:
    enabled: bool = False
    binary: str = ""
    model: str = ""
    language: str = "zh"
    timeout_seconds: float = 90.0
    max_concurrent: int = 1
    cache_enabled: bool = True
    cache_root: str = "data/whisper-cache"
    cache_ttl_seconds: int = 86400
    cache_max_items: int = 256


@dataclass
class StorageAreaMaintenanceConfig:
    max_bytes: int
    retention_seconds: int


@dataclass
class StorageResourceMaintenanceConfig(StorageAreaMaintenanceConfig):
    transient_retention_seconds: int = 86_400


@dataclass
class StorageMaintenanceConfig:
    enabled: bool = True
    interval_seconds: int = 21_600
    min_free_bytes: int = 5 * 1024**3
    sandbox: StorageAreaMaintenanceConfig = field(
        default_factory=lambda: StorageAreaMaintenanceConfig(2 * 1024**3, 14 * 86_400)
    )
    traces: StorageAreaMaintenanceConfig = field(
        default_factory=lambda: StorageAreaMaintenanceConfig(512 * 1024**2, 14 * 86_400)
    )
    resources: StorageResourceMaintenanceConfig = field(
        default_factory=lambda: StorageResourceMaintenanceConfig(
            5 * 1024**3,
            7 * 86_400,
            86_400,
        )
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
    # Non-owner safety constraints — hardened defaults for explicit structured
    # schedule creation.  Owners always bypass these; the natural-language path
    # runs its own LLM safety review and is not affected by these limits.
    non_owner_min_interval_seconds: int = 300
    non_owner_allow_unbounded: bool = True
    non_owner_max_occurrences: int = 10
    non_owner_max_mentions: int = 1
    non_owner_max_schedules_per_chat: int = 5
    non_owner_cooldown_seconds: int = 30


@dataclass
class ProfileConfig:
    default: str = ""
    groups: dict[str, str] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)


@dataclass
class MentionModeConfig:
    default: str = "chat"
    groups: dict[str, str] = field(default_factory=dict)


@dataclass
class BridgeConfig:
    owners: list[str] = field(default_factory=list)
    allowed_users: list[str] = field(default_factory=list)
    allowed_groups: list[str] = field(default_factory=list)
    workspaces: dict[str, bool] = field(default_factory=dict)
    commands: dict[str, bool | CommandAccess] = field(default_factory=dict)
    command_groups: dict[str, dict[str, CommandAccess]] = field(default_factory=dict)
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
    long_term_memory: LongTermMemoryConfig = field(default_factory=LongTermMemoryConfig)
    resources: ResourcesConfig = field(default_factory=ResourcesConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    storage_maintenance: StorageMaintenanceConfig = field(
        default_factory=StorageMaintenanceConfig
    )
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
        long_term_memory = _load_long_term_memory(raw.get("long_term_memory", {}))
        resources = ResourcesConfig(**raw.get("resources", {}))
        resources.animation_max_frames = min(16, max(2, int(resources.animation_max_frames)))
        resources.animation_max_duration_seconds = min(
            120, max(1, int(resources.animation_max_duration_seconds))
        )
        resources.animation_max_dimension = min(
            2048, max(256, int(resources.animation_max_dimension))
        )
        resources.animation_max_source_pixels = min(
            100_000_000, max(1_000_000, int(resources.animation_max_source_pixels))
        )
        animation_timeout = float(resources.animation_timeout_seconds)
        resources.animation_timeout_seconds = (
            min(120.0, max(1.0, animation_timeout))
            if math.isfinite(animation_timeout)
            else ResourcesConfig.animation_timeout_seconds
        )
        whisper_raw = raw.get("whisper", {})
        whisper = (
            WhisperConfig(**whisper_raw) if isinstance(whisper_raw, dict) else WhisperConfig()
        )
        timeout_seconds = float(whisper.timeout_seconds)
        whisper.timeout_seconds = (
            min(3600.0, max(1.0, timeout_seconds))
            if math.isfinite(timeout_seconds)
            else WhisperConfig.timeout_seconds
        )
        max_concurrent = float(whisper.max_concurrent)
        whisper.max_concurrent = (
            min(4, max(1, int(max_concurrent)))
            if math.isfinite(max_concurrent)
            else WhisperConfig.max_concurrent
        )
        whisper.cache_ttl_seconds = max(1, int(whisper.cache_ttl_seconds))
        whisper.cache_max_items = max(1, int(whisper.cache_max_items))
        storage_maintenance = _load_storage_maintenance(raw.get("storage_maintenance", {}))
        progress = ProgressConfig(**raw.get("progress", {}))
        proactive = ProactiveConfig(**raw.get("proactive", {}))
        scheduler = SchedulerConfig(**raw.get("scheduler", {}))
        profiles = _load_profiles(raw.get("profiles", {}))
        mention_modes = _load_mention_modes(raw.get("mention_modes", {}))
        commands_raw = raw.get("commands", {})
        return cls(
            owners=raw.get("owners", []),
            allowed_users=raw.get("allowed_users", []),
            allowed_groups=raw.get("allowed_groups", []),
            workspaces=raw.get("workspaces", {}),
            commands=_load_commands(commands_raw),
            command_groups=_load_command_groups(commands_raw),
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
            long_term_memory=long_term_memory,
            resources=resources,
            whisper=whisper,
            storage_maintenance=storage_maintenance,
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

    def is_command_allowed(self, name: str, group_id: str | None = None) -> bool:
        return self.command_access(name, group_id) != "disabled"

    def command_access(self, name: str, group_id: str | None = None) -> CommandAccess:
        command = str(name).strip().lower()
        if group_id is not None:
            group_commands = self.command_groups.get(_normalize_group_id(group_id))
            if group_commands and command in group_commands:
                return group_commands[command]
        return _resolve_command_access(command, self.commands.get(command, False))

    def mention_mode_for_group(self, gid: str) -> str:
        return self.mention_modes.groups.get(str(gid), self.mention_modes.default)

    def effective_max_runtime(self) -> int:
        return min(self.max_runtime_seconds, self.agent.max_runtime_seconds)

    def effective_max_chars(self) -> int:
        return min(self.max_output_chars, self.agent.max_output_chars)


def _load_storage_maintenance(raw: Any) -> StorageMaintenanceConfig:
    defaults = StorageMaintenanceConfig()
    values = raw if isinstance(raw, dict) else {}
    enabled = values.get("enabled", defaults.enabled)
    sandbox = _load_storage_area(values.get("sandbox"), defaults.sandbox)
    traces = _load_storage_area(values.get("traces"), defaults.traces)
    resources = _load_storage_resources(values.get("resources"), defaults.resources)
    return StorageMaintenanceConfig(
        enabled=enabled if isinstance(enabled, bool) else defaults.enabled,
        interval_seconds=_bounded_int(
            values.get("interval_seconds"), defaults.interval_seconds, 60, 7 * 86_400
        ),
        min_free_bytes=_bounded_int(
            values.get("min_free_bytes"), defaults.min_free_bytes, 0, 1024**4
        ),
        sandbox=sandbox,
        traces=traces,
        resources=resources,
    )


def _load_long_term_memory(raw: Any) -> LongTermMemoryConfig:
    defaults = LongTermMemoryConfig()
    values = raw if isinstance(raw, dict) else {}
    review_values = values.get("review")
    review_values = review_values if isinstance(review_values, dict) else {}
    retrieval_values = values.get("retrieval")
    retrieval_values = retrieval_values if isinstance(retrieval_values, dict) else {}
    decay_values = values.get("decay")
    decay_values = decay_values if isinstance(decay_values, dict) else {}

    database_path = values.get("database_path", defaults.database_path)
    if not isinstance(database_path, str) or not database_path.strip():
        database_path = defaults.database_path

    return LongTermMemoryConfig(
        enabled=_bool_or_default(values.get("enabled"), defaults.enabled),
        default_scope_enabled=_bool_or_default(
            values.get("default_scope_enabled"), defaults.default_scope_enabled
        ),
        groups=_bool_scope_map(values.get("groups")),
        users=_bool_scope_map(values.get("users")),
        database_path=database_path.strip(),
        review=MemoryReviewConfig(
            message_threshold=_bounded_int(
                review_values.get("message_threshold"), 40, 1, 10_000
            ),
            minimum_messages=_bounded_int(
                review_values.get("minimum_messages"), 10, 1, 10_000
            ),
            idle_seconds=_bounded_int(
                review_values.get("idle_seconds"), 600, 1, 604_800
            ),
            interval_seconds=_bounded_int(
                review_values.get("interval_seconds"), 21_600, 60, 2_592_000
            ),
            raw_ttl_seconds=_bounded_int(
                review_values.get("raw_ttl_seconds"), 604_800, 60, 2_592_000
            ),
            max_concurrent=_bounded_int(
                review_values.get("max_concurrent"), 1, 1, 1
            ),
            model=_nonempty_string(review_values.get("model"), "auto"),
            timeout_seconds=_bounded_int(
                review_values.get("timeout_seconds"), 90, 1, 3_600
            ),
            max_attempts=_bounded_int(
                review_values.get("max_attempts"), 3, 1, 20
            ),
        ),
        retrieval=MemoryRetrievalConfig(
            max_items=_bounded_int(retrieval_values.get("max_items"), 12, 1, 100),
            max_chars=_bounded_int(
                retrieval_values.get("max_chars"), 1_500, 1, 100_000
            ),
            minimum_score=_bounded_float(
                retrieval_values.get("minimum_score"), 0.45, 0.0, 1.0
            ),
        ),
        decay=MemoryDecayConfig(
            enabled=_bool_or_default(decay_values.get("enabled"), True),
            interval_seconds=_bounded_int(
                decay_values.get("interval_seconds"), 86_400, 60, 2_592_000
            ),
            grace_seconds=_bounded_int(
                decay_values.get("grace_seconds"), 2_592_000, 0, 31_536_000
            ),
            dormant_threshold=_bounded_float(
                decay_values.get("dormant_threshold"), 0.40, 0.0, 1.0
            ),
        ),
    )


def _load_storage_area(
    raw: Any,
    defaults: StorageAreaMaintenanceConfig,
) -> StorageAreaMaintenanceConfig:
    values = raw if isinstance(raw, dict) else {}
    return StorageAreaMaintenanceConfig(
        max_bytes=_bounded_int(values.get("max_bytes"), defaults.max_bytes, 0, 1024**4),
        retention_seconds=_bounded_int(
            values.get("retention_seconds"),
            defaults.retention_seconds,
            0,
            365 * 86_400,
        ),
    )


def _load_storage_resources(
    raw: Any,
    defaults: StorageResourceMaintenanceConfig,
) -> StorageResourceMaintenanceConfig:
    area = _load_storage_area(raw, defaults)
    values = raw if isinstance(raw, dict) else {}
    return StorageResourceMaintenanceConfig(
        max_bytes=area.max_bytes,
        retention_seconds=area.retention_seconds,
        transient_retention_seconds=_bounded_int(
            values.get("transient_retention_seconds"),
            defaults.transient_retention_seconds,
            0,
            365 * 86_400,
        ),
    )


def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(upper, max(lower, int(number)))


def _bounded_float(value: Any, default: float, lower: float, upper: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(upper, max(lower, number))


def _bool_or_default(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _nonempty_string(value: Any, default: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return value.strip()


def _bool_scope_map(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, bool)}


def _load_profiles(raw: Any) -> ProfileConfig:
    if not isinstance(raw, dict):
        return ProfileConfig()
    return ProfileConfig(
        default=str(raw.get("default", "") or "").rstrip(),
        groups=_string_map(raw.get("groups", {})),
        users=_string_map(raw.get("users", {})),
    )


def _load_commands(raw: Any) -> dict[str, bool | CommandAccess]:
    if not isinstance(raw, dict):
        return {}
    commands: dict[str, bool | CommandAccess] = {}
    for name, value in raw.items():
        command = str(name).strip().lower()
        if command == "groups":
            continue
        if isinstance(value, bool):
            commands[command] = value
            continue
        if isinstance(value, str) and value.strip().lower() in COMMAND_ACCESS_LEVELS:
            commands[command] = value.strip().lower()  # type: ignore[assignment]
            continue
        raise ValueError(
            f"commands.{command} must be true, false, owner, user, or disabled"
        )
    return commands


def _load_command_groups(raw: Any) -> dict[str, dict[str, CommandAccess]]:
    if not isinstance(raw, dict):
        return {}

    groups_key = next(
        (key for key in raw if str(key).strip().lower() == "groups"),
        None,
    )
    if groups_key is None:
        return {}

    raw_groups = raw[groups_key]
    if not isinstance(raw_groups, dict):
        raise ValueError("commands.groups must be a mapping")

    groups: dict[str, dict[str, CommandAccess]] = {}
    for group_id, raw_commands in raw_groups.items():
        normalized_group_id = _normalize_group_id(group_id)
        if not isinstance(raw_commands, dict):
            raise ValueError(f"commands.groups.{normalized_group_id} must be a mapping")
        group_commands: dict[str, CommandAccess] = {}
        for name, value in raw_commands.items():
            command = str(name).strip().lower()
            group_commands[command] = _parse_group_command_access(
                value, f"commands.groups.{normalized_group_id}.{command}"
            )
        groups[normalized_group_id] = group_commands
    return groups


def _parse_group_command_access(value: Any, location: str) -> CommandAccess:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in COMMAND_ACCESS_LEVELS:
            return normalized  # type: ignore[return-value]
    raise ValueError(f"{location} must be user, owner, or disabled")


def _normalize_group_id(group_id: Any) -> str:
    return str(group_id).strip().lower()


def _resolve_command_access(name: str, value: bool | CommandAccess) -> CommandAccess:
    if isinstance(value, bool):
        if not value:
            return "disabled"
        return "owner" if name in LEGACY_OWNER_COMMANDS else "user"
    normalized = str(value).strip().lower()
    if normalized in COMMAND_ACCESS_LEVELS:
        return normalized  # type: ignore[return-value]
    return "disabled"


def _load_mention_modes(raw: Any) -> MentionModeConfig:
    if not isinstance(raw, dict):
        return MentionModeConfig()
    default = _mention_mode(raw.get("default")) or "chat"
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
