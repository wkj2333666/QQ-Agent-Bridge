"""Persist global and per-group command access settings in config.yaml."""
from __future__ import annotations

from pathlib import Path

from .config import COMMAND_ACCESS_LEVELS, CommandAccess
from .config_block_store import write_top_level_block


def write_command_access_to_config(
    path: Path,
    commands: dict[str, bool | CommandAccess],
    groups: dict[str, dict[str, CommandAccess]],
) -> None:
    """Replace the top-level commands block without touching other config."""
    write_top_level_block(path, "commands", _format_commands_block(commands, groups))


def _format_commands_block(
    commands: dict[str, bool | CommandAccess],
    groups: dict[str, dict[str, CommandAccess]],
) -> str:
    lines = ["commands:"]
    command_items = [
        (str(name).strip().lower(), value)
        for name, value in commands.items()
        if str(name).strip().lower() != "groups"
    ]
    for name, value in sorted(command_items):
        lines.append(f"  {name}: {_format_global_access(value)}")

    if not groups:
        lines.append("  groups: {}")
        return "\n".join(lines)

    lines.append("  groups:")
    for group_id in sorted(groups, key=str):
        group_key = str(group_id)
        group_commands = groups[group_id]
        if not group_commands:
            lines.append(f"    {_quote_key(group_key)}: {{}}")
            continue
        lines.append(f"    {_quote_key(group_key)}:")
        for name in sorted(group_commands, key=lambda item: str(item).strip().lower()):
            command = str(name).strip().lower()
            lines.append(f"      {command}: {_format_group_access(group_commands[name])}")
    return "\n".join(lines)


def _format_global_access(value: bool | CommandAccess) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return _format_access(value)


def _format_group_access(value: CommandAccess) -> str:
    if not isinstance(value, str):
        raise ValueError("group command access must be user, owner, or disabled")
    return _format_access(value)


def _format_access(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in COMMAND_ACCESS_LEVELS:
        raise ValueError("command access must be user, owner, or disabled")
    return normalized


def _quote_key(key: str) -> str:
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
