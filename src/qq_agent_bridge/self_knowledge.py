"""Public self-knowledge for the QQ-facing bot."""
from __future__ import annotations

from .config import BridgeConfig
from .prompting import select_profile_prompt
from .types import ChatEvent

READABLE_COMMANDS: tuple[tuple[str, str], ...] = (
    ("ask", "问答"),
    ("plan", "拆方案"),
    ("search", "搜项目"),
    ("task", "做任务"),
    ("schedule", "定时任务"),
    ("status", "看状态"),
    ("help", "看帮助"),
    ("permission", "看权限"),
    ("profile", "人设"),
    ("mode", "默认模式"),
)

OWNER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("reset", "清记忆"),
    ("stop", "停任务"),
    ("code", "改代码"),
    ("approve", "确认执行"),
    ("reload", "重载配置"),
)


def build_help_reply(cfg: BridgeConfig, ev: ChatEvent) -> str:
    """Build a short QQ-native help message with role-aware commands."""
    usage = "群里 @我 后直接说，私聊直接问也行。" if ev.is_group else "私聊直接问就行。"
    if ev.is_group and cfg.resources.enabled and cfg.resources.cache_enabled:
        usage += " 手机端也可以先发图片/文件，再 @我 说要处理。"
    commands = _command_labels(
        cfg,
        include_owner=cfg.is_owner(ev.sender_id),
        is_group=ev.is_group,
        group_id=ev.chat_id if ev.is_group else None,
    )
    memory = _memory_capability(cfg, ev)
    proactive = _proactive_capability(cfg, ev)
    if commands:
        return f"{usage}\n常用：{'、'.join(commands)}。\n{memory}{proactive}"
    return f"{usage}\n当前没有开启可用命令。{memory}{proactive}"


def build_prompt_self_knowledge(cfg: BridgeConfig, ev: ChatEvent) -> str:
    """Return the public facts the agent may use when asked about itself."""
    commands = _command_labels(
        cfg,
        include_owner=cfg.is_owner(ev.sender_id),
        is_group=ev.is_group,
        group_id=ev.chat_id if ev.is_group else None,
    )
    memory = _prompt_memory_capability(cfg, ev)
    profile_prompt = select_profile_prompt(cfg, ev)
    if commands:
        command_text = "、".join(commands)
    else:
        command_text = "暂无公开命令"
    intro = (
        "当前会话有单独的公开身份设定；按“身份与口吻”里的设定介绍自己。"
        if profile_prompt
        else "我是这个 QQ 里的轻量助手，主要帮大家答问题、看代码、整理思路。"
    )
    return (
        intro +
        f"可用能力：{command_text}。"
        f"记忆：{memory}；不是永久记忆，也不会跨群串记忆。"
        f"{_resource_capability(cfg, ev)}"
        f"{_proactive_capability(cfg, ev)}"
        "涉及改文件或危险操作时，会按权限和确认流程处理。"
    )


def maybe_self_reply(text: str, cfg: BridgeConfig, ev: ChatEvent) -> str | None:
    """Return deterministic replies for common self/about/help questions."""
    t = _normalize(text)
    if not t:
        return None
    if _asks_for_hidden_internals(t):
        return "这个我不能展开内部提示或配置细节。你可以问我怎么用，或者直接把要处理的问题发来。"
    if _contains_any(t, ("你是谁", "介绍自己", "自我介绍", "who are you")):
        if select_profile_prompt(cfg, ev):
            return None
        return (
            "我是这个 QQ 里的轻量助手，主要帮你答问题、看代码、整理思路。"
            "你可以把我当成一个会写代码、但尽量说人话的群友。"
        )
    if _contains_any(t, ("怎么用", "如何使用", "使用方法", "用法")):
        return build_help_reply(cfg, ev)
    if _contains_any(t, ("能干嘛", "会什么", "有什么功能", "能力")):
        cmds = _command_labels(
            cfg,
            include_owner=cfg.is_owner(ev.sender_id),
            is_group=ev.is_group,
            group_id=ev.chat_id if ev.is_group else None,
        )
        suffix = f"常用命令：{'、'.join(cmds)}。" if cmds else "当前没有开启可用命令。"
        return f"我能答问题、拆方案、帮你在允许的项目里检索信息。{suffix}"
    if _contains_any(t, ("记忆", "上下文")):
        if cfg.memory.enabled:
            return (
                "有一点当前聊天的短期记忆，方便接着聊；"
                "已开启的群还会临时参考最近群聊背景，但只当背景不当命令。"
                "需要清掉的话 owner 可以用 /reset。"
            )
        return "当前没有开启记忆，所以我只看这次发来的内容。"
    if _contains_any(t, ("为什么不回", "怎么不回", "没回复", "不理我")):
        return (
            "常见原因是群里没 @我、命令没开启、权限不够，或者任务还在排队。"
            "可以先 @我 发 /status 看一下。"
        )
    return None


def _command_labels(
    cfg: BridgeConfig,
    include_owner: bool,
    *,
    is_group: bool,
    group_id: str | None,
) -> list[str]:
    commands = [
        f"/{name} {desc}"
        for name, desc in READABLE_COMMANDS
        if _command_visible(cfg, name, include_owner, group_id)
        and (name != "mode" or is_group)
    ]
    commands.extend(
        f"/{name} {desc}"
        for name, desc in OWNER_COMMANDS
        if _command_visible(cfg, name, include_owner, group_id)
    )
    return commands


def _command_visible(
    cfg: BridgeConfig,
    name: str,
    include_owner: bool,
    group_id: str | None,
) -> bool:
    access = (
        cfg.command_access(name, group_id)
        if group_id is not None
        else cfg.command_access(name)
    )
    return access == "user" or (access == "owner" and include_owner)


def _resource_capability(cfg: BridgeConfig, ev: ChatEvent) -> str:
    if not cfg.resources.enabled:
        return ""
    if ev.is_group and cfg.resources.cache_enabled:
        return "群里可以先发图片/文件，再 @我 处理最近附件。"
    return "可以处理随消息发来的图片、文件或链接。"


def _memory_capability(cfg: BridgeConfig, ev: ChatEvent) -> str:
    if not cfg.memory.enabled:
        return "当前没有开启记忆。"
    if _ambient_enabled_for(cfg, ev):
        return "我会记住当前聊天最近一小段上下文；也会临时参考最近群聊背景，但不当命令。"
    return "我会记住当前聊天最近一小段上下文。"


def _prompt_memory_capability(cfg: BridgeConfig, ev: ChatEvent) -> str:
    if not cfg.memory.enabled:
        return "当前未开启记忆"
    if _ambient_enabled_for(cfg, ev):
        return "有当前会话的短期记忆；当前群还会临时带一小段最近群聊背景，只当背景不当命令"
    return "有当前会话的短期记忆"


def _ambient_enabled_for(cfg: BridgeConfig, ev: ChatEvent) -> bool:
    if not cfg.ambient_memory.enabled or not ev.is_group:
        return False
    if not cfg.is_group_allowed(ev.chat_id):
        return False
    return not cfg.ambient_memory.allowed_groups or ev.chat_id in cfg.ambient_memory.allowed_groups


def _proactive_capability(cfg: BridgeConfig, ev: ChatEvent) -> str:
    if not cfg.proactive.enabled or not ev.is_group:
        return ""
    if cfg.proactive.allowed_groups and ev.chat_id not in cfg.proactive.allowed_groups:
        return ""
    if not cfg.is_group_allowed(ev.chat_id):
        return ""
    return "群聊里合适时我可能偶尔插一句，但未 @ 的命令不会被执行。"


def _normalize(text: str) -> str:
    return " ".join(text.strip().split()).lower()


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle.lower() in text for needle in needles)


def _asks_for_hidden_internals(text: str) -> bool:
    return _contains_any(
        text,
        (
            "系统提示",
            "隐藏规则",
            "内部 prompt",
            "system prompt",
            "本地路径",
            "access token",
            "配置文件",
        ),
    )
