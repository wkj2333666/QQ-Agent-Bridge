"""Cheap intent routing for mentioned group chat."""
from __future__ import annotations


QUESTION_MARKERS: tuple[str, ...] = (
    "?",
    "？",
    "怎么",
    "如何",
    "为什么",
    "为啥",
    "啥",
    "什么",
    "谁",
    "哪",
    "能不能",
    "可不可以",
    "可以不",
    "有没有",
    "是不是",
    "怎么看",
    "咋",
)

TASK_MARKERS: tuple[str, ...] = (
    "帮我",
    "帮忙",
    "查一下",
    "搜一下",
    "百度一下",
    "搜索",
    "分析",
    "总结",
    "整理",
    "写一",
    "生成",
    "翻译",
    "解释",
    "看看",
    "看下",
    "处理",
    "修一下",
    "报错",
    "bug",
    "代码",
    "文件",
    "图片",
    "链接",
    "刚才",
    "上面",
    "继续",
)

CASUAL_REPLIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("谢谢", "谢了", "thx", "thanks"), "不客气"),
    (("早", "早上好"), "早"),
    (("晚安",), "晚安"),
)


def strip_leading_mentions(text: str) -> str:
    """Remove one or more leading QQ mentions from text."""
    t = text.strip()
    while t.startswith("@"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2:
            return ""
        t = parts[1].strip()
    return t


def should_implicit_ask_in_group(text: str, *, has_resources: bool = False) -> bool:
    """Return true when a no-command group mention is clearly a request."""
    if has_resources:
        return True
    t = strip_leading_mentions(text)
    if not t:
        return False
    lowered = t.lower()
    if _is_simple_casual(lowered):
        return False
    if any(marker in lowered for marker in QUESTION_MARKERS + TASK_MARKERS):
        return True
    return len(t) >= 18


def casual_reply_for_group_mention(text: str) -> str:
    """Return a short local QQ-style reply for casual no-command mentions."""
    t = strip_leading_mentions(text)
    lowered = t.lower()
    for needles, reply in CASUAL_REPLIES:
        if any(needle in lowered for needle in needles):
            return reply
    return "在呢"


def _is_simple_casual(text: str) -> bool:
    compact = "".join(text.split())
    if not compact:
        return True
    greetings = {
        "hi",
        "hello",
        "hey",
        "你好",
        "您好",
        "在吗",
        "在不在",
        "出来",
        "冒泡",
    }
    return compact in greetings
