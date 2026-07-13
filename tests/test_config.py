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

    for command in ("ask", "plan", "search", "task", "status", "help", "profile"):
        assert cfg.is_command_allowed(command), command


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


def test_example_config_enables_resource_passthrough() -> None:
    cfg = BridgeConfig.load(ROOT / "config.example.yaml")

    assert cfg.resources.enabled
    assert cfg.resources.root == "downloads/qq-agent-bridge"
    assert cfg.resources.max_items > 0
    assert cfg.resources.max_bytes > 0
    assert cfg.resources.cache_enabled
    assert cfg.resources.cache_ttl_seconds == 600
    assert cfg.resources.cache_max_items == 4
    assert "voice" in cfg.resources.allowed_kinds
    assert "forward" in cfg.resources.allowed_kinds


def test_napcat_compose_mounts_workspace_readonly_for_uploads() -> None:
    compose = yaml.safe_load((ROOT / "runtime" / "napcat" / "compose.yml").read_text())
    volumes = compose["services"]["napcat"]["volumes"]

    assert (
        "${BRIDGE_WORKSPACE_ABS:-/opt/qq-agent-bridge/workspace}:"
        "${BRIDGE_WORKSPACE_ABS:-/opt/qq-agent-bridge/workspace}:ro"
    ) in volumes
