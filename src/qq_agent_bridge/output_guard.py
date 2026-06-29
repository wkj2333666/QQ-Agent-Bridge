"""Guards for user-visible agent output."""
from __future__ import annotations


_INTERNAL_PROMPT_MARKERS = (
    "你现在是在 QQ 里回复用户的 QQ聊天机器人",
    "身份与口吻：",
    "上下文：",
    "历史对话：",
    "最近群聊背景：",
    "用户附带资源：",
    "输出资源：",
    "用户消息：",
    "QQ_COMMAND=",
    "<skill name=",
)


def guard_internal_output(text: str) -> str:
    """Block accidental prompt/context echoes before they reach QQ."""
    if not _looks_like_internal_prompt_echo(text):
        return text
    return "[error] 助手输出异常：疑似泄露内部提示，已拦截。请重试。"


def _looks_like_internal_prompt_echo(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith(_INTERNAL_PROMPT_MARKERS[0]):
        return True
    hits = sum(1 for marker in _INTERNAL_PROMPT_MARKERS if marker in text)
    return hits >= 2
