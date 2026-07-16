"""Config template regression tests."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore


ROOT = Path(__file__).resolve().parents[1]


def test_example_config_enables_read_only_commands() -> None:
    cfg = BridgeConfig.load(ROOT / "config.example.yaml")

    for command in ("ask", "plan", "search", "task", "status", "help", "profile", "mode"):
        assert cfg.is_command_allowed(command), command


def test_command_access_levels_load_from_config(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
owners: [owner]
commands:
  ask: user
  code: owner
  shell: disabled
""",
        encoding="utf-8",
    )

    cfg = BridgeConfig.load(config)

    assert cfg.command_access("ask") == "user"
    assert cfg.command_access("code") == "owner"
    assert cfg.command_access("shell") == "disabled"
    assert cfg.is_command_allowed("ask")
    assert cfg.is_command_allowed("code")
    assert not cfg.is_command_allowed("shell")


def test_legacy_boolean_command_config_keeps_existing_defaults() -> None:
    cfg = BridgeConfig(commands={"ask": True, "code": True, "shell": False})

    assert cfg.command_access("ask") == "user"
    assert cfg.command_access("code") == "owner"
    assert cfg.command_access("shell") == "disabled"


def test_example_config_enables_owner_reset_and_memory() -> None:
    cfg = BridgeConfig.load(ROOT / "config.example.yaml")

    assert cfg.is_command_allowed("reset")
    assert cfg.is_command_allowed("reload")
    assert cfg.memory.enabled
    assert cfg.memory.max_messages > 0
    assert cfg.memory.max_chars > 0
    assert cfg.ambient_memory.enabled
    assert cfg.ambient_memory.max_messages > 0
    assert cfg.ambient_memory.max_chars > 0
    assert cfg.ambient_memory.max_age_seconds > 0


def test_agent_config_has_fast_chat_and_task_models() -> None:
    cfg = BridgeConfig.load(ROOT / "config.example.yaml")

    assert cfg.agent.runtime == ""
    assert cfg.agent.binary == ""
    assert cfg.agent.command == {}
    assert cfg.agent.chat_model == "auto"
    assert cfg.agent.task_model == "composer"
    assert cfg.agent.env_runner == "micromamba"
    assert cfg.agent.env_name == "base"
    assert cfg.agent.require_env
    assert cfg.agent.default_workspace == "/opt/qq-agent-bridge/workspace"
    assert cfg.workspaces == {"/opt/qq-agent-bridge/workspace": True}
    assert cfg.agent.use_bwrap
    assert cfg.agent.bwrap_binary == "bwrap"
    assert cfg.agent.force_task_tools
    assert cfg.agent.sandbox_home == "/tmp/qq-agent-bridge/agent-home"
    assert cfg.bot.reply_chunk_delay_seconds == 0.2


def test_example_config_enables_long_task_progress() -> None:
    cfg = BridgeConfig.load(ROOT / "config.example.yaml")

    assert cfg.progress.enabled
    assert cfg.progress.first_heartbeat_seconds == 30
    assert cfg.progress.heartbeat_seconds == 45
    assert cfg.progress.max_heartbeat_messages == 6
    assert cfg.progress.min_progress_interval_seconds == 8
    assert cfg.progress.max_progress_messages == 8
    assert cfg.progress.max_progress_chars == 240


def test_example_config_enables_proactive_chat_with_limits() -> None:
    cfg = BridgeConfig.load(ROOT / "config.example.yaml")

    assert cfg.proactive.enabled
    assert not cfg.proactive.debug
    assert cfg.proactive.batch_seconds == 8
    assert cfg.proactive.min_messages == 2
    assert cfg.proactive.cooldown_seconds == 16
    assert cfg.proactive.quiet_after_bot_seconds == 16
    assert cfg.proactive.max_per_hour == 180
    assert cfg.proactive.model == "auto"
    assert cfg.proactive.max_reply_messages == 3
    assert cfg.proactive.reply_message_delay_seconds == 0.6
    assert "/" in cfg.proactive.ignored_prefixes


def test_config_loads_default_group_and_user_profiles(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
profiles:
  default: "默认身份"
  groups:
    "2000000001": "群 2000000001 身份"
    223344: "群 223344 身份"
  users:
    "1000000001": "用户 1000000001 身份"
    556677: "用户 556677 身份"
""",
        encoding="utf-8",
    )

    cfg = BridgeConfig.load(config_path)

    assert cfg.profiles.default == "默认身份"
    assert cfg.profiles.groups == {
        "2000000001": "群 2000000001 身份",
        "223344": "群 223344 身份",
    }
    assert cfg.profiles.users == {
        "1000000001": "用户 1000000001 身份",
        "556677": "用户 556677 身份",
    }


def test_example_config_documents_empty_profile_maps() -> None:
    cfg = BridgeConfig.load(ROOT / "config.example.yaml")

    assert cfg.profiles.default == ""
    assert cfg.profiles.groups == {}
    assert cfg.profiles.users == {}


def test_example_config_defaults_group_mentions_to_ask() -> None:
    cfg = BridgeConfig.load(ROOT / "config.example.yaml")

    assert cfg.mention_modes.default == "ask"
    assert cfg.mention_modes.groups == {}
    assert cfg.mention_mode_for_group("any-group") == "ask"


def test_config_loads_isolated_group_mention_modes_and_drops_unsafe_values(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mention_modes:
  default: task
  groups:
    1001: plan
    "1002": ask
    "1003": code
""",
        encoding="utf-8",
    )

    cfg = BridgeConfig.load(config_path)

    assert cfg.mention_modes.default == "task"
    assert cfg.mention_modes.groups == {"1001": "plan", "1002": "ask"}
    assert cfg.mention_mode_for_group("1001") == "plan"
    assert cfg.mention_mode_for_group("1002") == "ask"
    assert cfg.mention_mode_for_group("other") == "task"


def test_example_config_enables_resource_passthrough() -> None:
    cfg = BridgeConfig.load(ROOT / "config.example.yaml")

    assert cfg.resources.enabled
    assert cfg.resources.root == "downloads/qq-agent-bridge"
    assert cfg.resources.max_items > 0
    assert cfg.resources.max_bytes > 0
    assert cfg.resources.cache_enabled
    assert cfg.resources.cache_ttl_seconds == 600
    assert cfg.resources.cache_max_items == 4
    assert cfg.resources.local_media_roots == []
    assert "voice" in cfg.resources.allowed_kinds
    assert "forward" in cfg.resources.allowed_kinds


def test_config_loads_local_media_roots(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
resources:
  local_media_roots:
    - /var/lib/onebot/media
    - /srv/qq-records
""",
        encoding="utf-8",
    )

    cfg = BridgeConfig.load(config_path)

    assert cfg.resources.local_media_roots == ["/var/lib/onebot/media", "/srv/qq-records"]


def test_example_config_keeps_whisper_disabled_with_safe_defaults() -> None:
    cfg = BridgeConfig.load(ROOT / "config.example.yaml")

    assert not cfg.whisper.enabled
    assert cfg.whisper.binary == ""
    assert cfg.whisper.model == ""
    assert cfg.whisper.language == "zh"
    assert cfg.whisper.timeout_seconds == 90
    assert cfg.whisper.max_concurrent == 1
    assert cfg.whisper.cache_enabled
    assert cfg.whisper.cache_root == "data/whisper-cache"
    assert cfg.whisper.cache_ttl_seconds == 86400
    assert cfg.whisper.cache_max_items == 256


def test_config_loads_whisper_fields_and_clamps_concurrency(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
whisper:
  enabled: true
  binary: "/usr/local/bin/whisper"
  model: "/models/zh.bin"
  language: en
  timeout_seconds: 12.5
  max_concurrent: 0
  cache_enabled: false
  cache_root: "/var/cache/whisper"
  cache_ttl_seconds: 300
  cache_max_items: 12
""",
        encoding="utf-8",
    )

    cfg = BridgeConfig.load(config_path)

    assert cfg.whisper.enabled
    assert cfg.whisper.binary == "/usr/local/bin/whisper"
    assert cfg.whisper.model == "/models/zh.bin"
    assert cfg.whisper.language == "en"
    assert cfg.whisper.timeout_seconds == 12.5
    assert cfg.whisper.max_concurrent == 1
    assert not cfg.whisper.cache_enabled
    assert cfg.whisper.cache_root == "/var/cache/whisper"
    assert cfg.whisper.cache_ttl_seconds == 300
    assert cfg.whisper.cache_max_items == 12


def test_config_clamps_whisper_timeout_and_concurrency_upper_bounds(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
whisper:
  timeout_seconds: 999999
  max_concurrent: 999999
""",
        encoding="utf-8",
    )

    cfg = BridgeConfig.load(config_path)

    assert cfg.whisper.timeout_seconds == 3600
    assert cfg.whisper.max_concurrent == 4


def test_config_uses_whisper_defaults_for_non_finite_limits(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
whisper:
  timeout_seconds: .inf
  max_concurrent: .nan
""",
        encoding="utf-8",
    )

    cfg = BridgeConfig.load(config_path)

    assert cfg.whisper.timeout_seconds == 90
    assert cfg.whisper.max_concurrent == 1


def test_napcat_compose_mounts_workspace_readonly_for_uploads() -> None:
    compose = yaml.safe_load((ROOT / "runtime" / "napcat" / "compose.yml").read_text())
    volumes = compose["services"]["napcat"]["volumes"]

    assert (
        "${BRIDGE_WORKSPACE_ABS:-/opt/qq-agent-bridge/workspace}:"
        "${BRIDGE_WORKSPACE_ABS:-/opt/qq-agent-bridge/workspace}:ro"
    ) in volumes
