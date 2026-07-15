"""Persist QQ profile overrides in config.yaml without rewriting unrelated config."""
from __future__ import annotations

from pathlib import Path

from .config import ProfileConfig
from .config_block_store import write_top_level_block


def write_profiles_to_config(path: Path, profiles: ProfileConfig) -> None:
    """Replace or append the top-level profiles block."""
    block = _format_profiles_block(profiles)
    write_top_level_block(path, "profiles", block)


def _format_profiles_block(profiles: ProfileConfig) -> str:
    lines = ["profiles:"]
    lines.extend(_format_scalar("default", profiles.default, indent=2))
    lines.extend(_format_map("groups", profiles.groups, indent=2))
    lines.extend(_format_map("users", profiles.users, indent=2))
    return "\n".join(lines)


def _format_map(name: str, values: dict[str, str], indent: int) -> list[str]:
    pad = " " * indent
    if not values:
        return [f"{pad}{name}: {{}}"]
    lines = [f"{pad}{name}:"]
    for key in sorted(values):
        lines.extend(_format_scalar(_quote_key(key), values[key], indent=indent + 2))
    return lines


def _format_scalar(key: str, value: str, indent: int) -> list[str]:
    pad = " " * indent
    text = value.rstrip()
    if not text:
        return [f'{pad}{key}: ""']
    lines = [f"{pad}{key}: |"]
    for line in text.splitlines():
        lines.append(f"{pad}  {line}")
    return lines


def _quote_key(key: str) -> str:
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
