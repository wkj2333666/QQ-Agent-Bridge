"""Structured, context-aware help for bridge commands."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import BridgeConfig, LEGACY_OWNER_COMMANDS
from .types import ChatEvent


@dataclass(frozen=True)
class CommandHelpSpec:
    """Static help content for one command."""

    summary: str
    usage: tuple[str, ...]
    examples: tuple[str, ...] = ()
    restrictions: tuple[str, ...] = ()


COMMAND_HELP_METADATA: dict[str, CommandHelpSpec] = {
    "ask": CommandHelpSpec(
        summary="直接提问或让助手处理一段内容。",
        usage=("/ask <问题或内容>",),
        examples=("/ask 帮我解释这段报错",),
        restrictions=("群聊需要先 @我；私聊可直接使用。",),
    ),
    "plan": CommandHelpSpec(
        summary="把目标拆成可执行的方案。",
        usage=("/plan <目标或问题>",),
        examples=("/plan 设计一个备份方案",),
        restrictions=("群聊需要先 @我。",),
    ),
    "search": CommandHelpSpec(
        summary="在允许访问的项目工作区中搜索信息。",
        usage=("/search <关键词或问题>",),
        examples=("/search 查找登录失败的处理逻辑",),
        restrictions=("只能搜索配置允许的工作区；群聊需要先 @我。",),
    ),
    "task": CommandHelpSpec(
        summary="执行需要多步操作的任务。",
        usage=("/task <任务描述>",),
        examples=("/task 整理这个项目的测试入口",),
        restrictions=("群聊需要先 @我；可能受并发、工作区和确认策略限制。",),
    ),
    "code": CommandHelpSpec(
        summary="在允许的工作区中修改代码。",
        usage=("/code <修改目标>",),
        examples=("/code 修复这个函数的边界条件",),
        restrictions=("通常需要 owner 权限，并可能需要确认；群聊需要先 @我。",),
    ),
    "status": CommandHelpSpec(
        summary="查看当前会话中的任务状态。",
        usage=("/status",),
        examples=("/status",),
        restrictions=("群聊需要先 @我。",),
    ),
    "stop": CommandHelpSpec(
        summary="停止一个正在运行或等待确认的任务。",
        usage=("/stop [任务编号]",),
        examples=("/stop j123-abc",),
        restrictions=("通常需要 owner 权限；省略编号时使用默认活动任务。",),
    ),
    "approve": CommandHelpSpec(
        summary="确认一个等待执行的危险任务。",
        usage=("/approve <任务编号> <确认码>",),
        examples=("/approve j123-abc 7f3a91c2",),
        restrictions=("需要 owner 权限，且任务必须处于等待确认状态。",),
    ),
    "shell": CommandHelpSpec(
        summary="在允许的工作区中执行受控 shell 任务。",
        usage=("/shell <任务描述>",),
        examples=("/shell 检查当前项目的 Python 版本",),
        restrictions=("通常需要 owner 权限，并可能需要确认；只允许配置的工作区。",),
    ),
    "help": CommandHelpSpec(
        summary="查看全部命令或某个命令的详细帮助。",
        usage=("/help", "/help <命令>"),
        examples=("/help task", "/help schedule"),
        restrictions=("禁用命令仍可显示说明，但不会因此启用或执行。",),
    ),
    "profile": CommandHelpSpec(
        summary="查看或使用当前会话的人设信息。",
        usage=("/profile",),
        examples=("/profile",),
        restrictions=("内容由当前用户和群聊配置决定。",),
    ),
    "mode": CommandHelpSpec(
        summary="查看或设置群聊中 @我 后的默认工作模式。",
        usage=("/mode", "/mode set ask|plan|task"),
        examples=("/mode", "/mode set plan"),
        restrictions=("只适用于群聊；设置模式需要对应权限。",),
    ),
    "reset": CommandHelpSpec(
        summary="清空当前会话记忆和可用的群聊背景。",
        usage=("/reset",),
        examples=("/reset",),
        restrictions=("通常需要 owner 权限；只影响当前会话。",),
    ),
    "reload": CommandHelpSpec(
        summary="重新加载配置文件。",
        usage=("/reload",),
        examples=("/reload",),
        restrictions=("通常需要 owner 权限；正在运行的任务按现有策略处理。",),
    ),
    "schedule": CommandHelpSpec(
        summary="创建、查看和管理定时任务。",
        usage=(
            "/schedule <自然语言时间规则和任务>",
            "/schedule list|show <索引>|pause <索引>|resume <索引>",
            "/schedule run <索引>|cancel <索引>",
        ),
        examples=(
            "/schedule once 2026-07-14 08:00 -- send 记得开会",
            "/schedule in 10m -- send 起来活动一下",
            "/schedule daily 08:00 -- task 查询北京市天气",
            "/schedule list",
        ),
        restrictions=("自然语言和结构化写法都使用当前配置的时区。",),
    ),
    "permission": CommandHelpSpec(
        summary="查看或修改当前群的命令权限覆盖。",
        usage=(
            "/permission",
            "/permission set <命令> user|owner|disabled",
            "/permission clear [命令]",
        ),
        examples=(
            "/permission",
            "/permission set task disabled",
            "/permission clear task",
        ),
        restrictions=("只用于群聊；查看需命令可用，设置和清除仅群 owner 可执行。",),
    ),
}

# Short aliases make the metadata easy to consume from later command routing.
COMMAND_HELP = COMMAND_HELP_METADATA
COMMAND_NAMES = tuple(COMMAND_HELP_METADATA)

_MISSING = object()


def build_command_help(name: str, cfg: BridgeConfig, ev: ChatEvent) -> str:
    """Render detailed help for a command in the current chat context."""
    command = _normalize_command_name(name)
    spec = COMMAND_HELP_METADATA.get(command)
    if spec is None:
        return _unknown_command_reply(command)

    access = _effective_access(command, cfg, ev)
    lines = [f"/{command}：{spec.summary}", "用法："]
    lines.extend(f"  {usage}" for usage in spec.usage)
    lines.append(_permission_line(access, ev))

    restrictions = list(spec.restrictions)
    if command == "schedule":
        timezone = _scheduler_timezone(cfg)
        restrictions.append(f"当前时区：{timezone}。")
    if access == "disabled":
        restrictions.append("当前命令已禁用，帮助仅作说明。")
    if restrictions:
        lines.append("限制：" + "；".join(restrictions))

    if spec.examples:
        lines.append("示例：")
        lines.extend(f"  {example}" for example in spec.examples)
    return "\n".join(lines)


def _normalize_command_name(name: str) -> str:
    raw = str(name or "").strip().lower()
    if raw.startswith("/"):
        raw = raw[1:]
    return raw.split(maxsplit=1)[0] if raw else ""


def _unknown_command_reply(name: str) -> str:
    known = "、".join(f"/{command}" for command in COMMAND_NAMES)
    shown = name or "（空）"
    return f"未知命令“{shown}”。可用命令：{known}。使用 /help <命令> 查看详细用法。"


def _permission_line(access: str, ev: ChatEvent) -> str:
    if getattr(ev, "is_group", False):
        group_id = str(getattr(ev, "chat_id", "") or "未知群")
        return f"权限：{access}（当前群 {group_id} 的有效权限）"
    return f"权限：{access}（私聊按全局配置）"


def _effective_access(name: str, cfg: Any, ev: ChatEvent) -> str:
    group_id = _group_id(ev)
    resolver = getattr(cfg, "command_access", None)
    if callable(resolver):
        if group_id is not None:
            try:
                return _normalize_access(resolver(name, group_id), name)
            except TypeError:
                pass
        try:
            global_access = resolver(name)
        except TypeError:
            global_access = _MISSING
        if global_access is not _MISSING:
            if group_id is not None:
                override = _group_override(cfg, group_id, name)
                if override is not _MISSING:
                    return _normalize_access(override, name)
            return _normalize_access(global_access, name)

    global_access = _mapping_access(getattr(cfg, "commands", {}), name)
    if group_id is not None:
        override = _group_override(cfg, group_id, name)
        if override is not _MISSING:
            return _normalize_access(override, name)
    return _normalize_access(global_access, name)


def _group_id(ev: Any) -> str | None:
    if not getattr(ev, "is_group", False):
        return None
    value = getattr(ev, "chat_id", None)
    return str(value) if value is not None and str(value) else None


def _group_override(cfg: Any, group_id: str, name: str) -> Any:
    groups = getattr(cfg, "command_groups", _MISSING)
    if not isinstance(groups, Mapping):
        commands = getattr(cfg, "commands", {})
        groups = commands.get("groups", _MISSING) if isinstance(commands, Mapping) else _MISSING
    if not isinstance(groups, Mapping):
        return _MISSING
    group = groups.get(group_id, _MISSING)
    if group is _MISSING:
        group = groups.get(str(group_id), _MISSING)
    if not isinstance(group, Mapping):
        return _MISSING
    return group.get(name, _MISSING)


def _mapping_access(commands: Any, name: str) -> Any:
    if not isinstance(commands, Mapping):
        return _MISSING
    return commands.get(name, _MISSING)


def _normalize_access(value: Any, name: str) -> str:
    if isinstance(value, bool):
        if not value:
            return "disabled"
        return "owner" if name in LEGACY_OWNER_COMMANDS else "user"
    normalized = str(value).strip().lower()
    if normalized in {"disabled", "user", "owner"}:
        return normalized
    return "disabled"


def _scheduler_timezone(cfg: Any) -> str:
    scheduler = getattr(cfg, "scheduler", None)
    timezone = getattr(scheduler, "timezone", "Asia/Shanghai")
    return str(timezone or "Asia/Shanghai")


__all__ = [
    "COMMAND_HELP",
    "COMMAND_HELP_METADATA",
    "COMMAND_NAMES",
    "CommandHelpSpec",
    "build_command_help",
]
