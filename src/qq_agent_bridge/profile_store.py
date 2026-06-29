"""Persist QQ profile overrides in config.yaml without rewriting unrelated config."""
from __future__ import annotations

from pathlib import Path

from .config import ProfileConfig


def write_profiles_to_config(path: Path, profiles: ProfileConfig) -> None:
    """Replace or append the top-level profiles block."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = _format_profiles_block(profiles)
    if not text.strip():
        path.write_text(block + "\n", encoding="utf-8")
        return
    updated = _replace_top_level_block(text, "profiles", block)
    if updated == text:
        updated = _append_profiles_block(text, block)
    path.write_text(updated, encoding="utf-8")


def _replace_top_level_block(text: str, key: str, block: str) -> str:
    lines = text.splitlines(keepends=True)
    start: int | None = None
    end = len(lines)
    prefix = f"{key}:"
    for idx, line in enumerate(lines):
        if line.startswith(prefix):
            start = idx
            break
    if start is None:
        return text
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        if line.strip() and not line.startswith((" ", "\t")) and not line.lstrip().startswith("#"):
            end = idx
            break
    replacement = [item + "\n" for item in block.splitlines()]
    return "".join(lines[:start] + replacement + lines[end:])


def _append_profiles_block(text: str, block: str) -> str:
    marker = "\n# OneBot"
    if marker in text:
        before, after = text.split(marker, 1)
        return before.rstrip() + "\n\n" + block + "\n" + marker + after
    return text.rstrip() + "\n\n" + block + "\n"


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
