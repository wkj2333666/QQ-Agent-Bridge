"""Persist per-group implicit mention modes in config.yaml."""
from __future__ import annotations

from pathlib import Path

from .config import MentionModeConfig
from .config_block_store import write_top_level_block


def write_mention_modes_to_config(path: Path, modes: MentionModeConfig) -> None:
    lines = ["mention_modes:", f"  default: {modes.default}"]
    if not modes.groups:
        lines.append("  groups: {}")
    else:
        lines.append("  groups:")
        for group_id in sorted(modes.groups):
            lines.append(f"    {_quote_key(group_id)}: {modes.groups[group_id]}")
    write_top_level_block(path, "mention_modes", "\n".join(lines))


def _quote_key(key: str) -> str:
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
