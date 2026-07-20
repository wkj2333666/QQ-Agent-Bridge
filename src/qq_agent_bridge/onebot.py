"""OneBot v11 reverse WebSocket adapter."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection

from .types import ChatEvent, ChatReply, ChatResource, ChatSegment

logger = logging.getLogger(__name__)

MessageHandler = Callable[[ChatEvent], Awaitable[None]]


_URL_RE = re.compile(r"https?://[^\s<>'\"，。！？、]+")
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^,\]]+)(?:,[^\]]*)?\]\s*")
_CQ_REPLY_RE = re.compile(r"\[CQ:reply,id=([^,\]]+)(?:,[^\]]*)?\]\s*")
_NAPCAT_DISPLAY_REPLY_RE = re.compile(
    r"\[回复消息\s+\[[^\]]*?\((?P<sender>\d+)\)\]\s*(?P<text>.*?)\s*\]\s*"
)


def _extract_text(msg: Any) -> str:
    if isinstance(msg, str):
        return _normalize_cq_text(msg)
    if isinstance(msg, list):
        parts: list[str] = []
        for seg in msg:
            if isinstance(seg, dict):
                if seg.get("type") == "text":
                    parts.append(seg.get("data", {}).get("text", ""))
                elif seg.get("type") == "at":
                    parts.append(f"@{seg.get('data', {}).get('qq', '')} ")
            else:
                parts.append(str(seg))
        return "".join(parts)
    return str(msg)


def _normalize_cq_text(text: str) -> str:
    text = _CQ_REPLY_RE.sub("", text)
    text = _NAPCAT_DISPLAY_REPLY_RE.sub("", text)
    return _CQ_AT_RE.sub(lambda match: f"@{match.group(1)} ", text)


def _normalize_structured_text(text: str) -> str:
    """Keep decoded text literal; only remove the optional NapCat reply preview."""
    return _NAPCAT_DISPLAY_REPLY_RE.sub("", text)


def _extract_segments_and_resources(
    msg: Any,
) -> tuple[str, tuple[ChatSegment, ...], tuple[ChatResource, ...], ChatReply | None]:
    if not isinstance(msg, list):
        text = _extract_text(msg).strip()
        resources = tuple(_url_resources(text, source_segment=0))
        reply = _reply_from_cq_text(msg) if isinstance(msg, str) else None
        if isinstance(msg, str):
            reply = reply or _reply_from_napcat_display_text(msg)
        reply_segment = (
            (
                ChatSegment(
                    type="reply",
                    text=reply.text,
                    raw_type="reply",
                    raw_data=reply.raw_data,
                ),
            )
            if reply
            else ()
        )
        message_segments = (
            _segments_from_cq_string(msg)
            if isinstance(msg, str)
            else ((ChatSegment(type="text", text=text, raw_type="raw"),) if text else ())
        )
        return text, reply_segment + message_segments, resources, reply

    text_parts: list[str] = []
    segments: list[ChatSegment] = []
    resources: list[ChatResource] = []
    reply: ChatReply | None = None
    for idx, seg in enumerate(msg):
        if not isinstance(seg, dict):
            text = str(seg)
            text_parts.append(text)
            segments.append(ChatSegment(type="text", text=text, raw_type="text"))
            resources.extend(_url_resources(text, source_segment=idx))
            continue
        raw_type = str(seg.get("type", "unknown"))
        data = seg.get("data", {})
        if not isinstance(data, dict):
            data = {}
        if raw_type == "text":
            raw_text = str(data.get("text", ""))
            if reply is None:
                reply = _reply_from_napcat_display_text(raw_text)
            text = _normalize_structured_text(raw_text)
            text_parts.append(text)
            segments.append(ChatSegment(type="text", text=text, raw_type=raw_type, raw_data=data))
            resources.extend(_url_resources(text, source_segment=idx))
        elif raw_type == "at":
            qq = str(data.get("qq", ""))
            text = f"@{qq} "
            text_parts.append(text)
            segments.append(ChatSegment(type="mention", text=text, qq=qq, raw_type=raw_type, raw_data=data))
        elif raw_type == "reply":
            reply = _reply_from_segment_data(data)
            segments.append(
                ChatSegment(
                    type="reply",
                    text=reply.text if reply else "",
                    raw_type=raw_type,
                    raw_data=data,
                )
            )
            if reply:
                resources.extend(reply.resources)
        elif raw_type in {"forward", "json", "node"}:
            resource = _forward_resource_from_segment(raw_type, data, idx)
            if resource:
                resources.append(resource)
                segments.append(
                    ChatSegment(
                        type="forward",
                        resource=resource,
                        raw_type=raw_type,
                        raw_data=data,
                    )
                )
            else:
                segments.append(ChatSegment(type="unknown", raw_type=raw_type, raw_data=data))
        else:
            resource = _resource_from_segment(raw_type, data, idx)
            if resource:
                resources.append(resource)
                segments.append(
                    ChatSegment(
                        type=resource.kind,
                        resource=resource,
                        raw_type=raw_type,
                        raw_data=data,
                    )
                )
            else:
                segments.append(ChatSegment(type="unknown", raw_type=raw_type, raw_data=data))
    return "".join(text_parts).strip(), tuple(segments), tuple(resources), reply


def _segments_from_cq_string(text: str) -> tuple[ChatSegment, ...]:
    clean = _NAPCAT_DISPLAY_REPLY_RE.sub("", _CQ_REPLY_RE.sub("", text))
    if _CQ_AT_RE.search(clean) is None:
        normalized = clean.strip()
        return (
            (ChatSegment(type="text", text=normalized, raw_type="raw"),)
            if normalized
            else ()
        )
    segments: list[ChatSegment] = []
    cursor = 0
    for match in _CQ_AT_RE.finditer(clean):
        preceding = clean[cursor : match.start()]
        if preceding:
            segments.append(ChatSegment(type="text", text=preceding, raw_type="raw"))
        qq = match.group(1)
        rendered = f"@{qq} "
        if qq.isdigit():
            segments.append(
                ChatSegment(
                    type="mention",
                    text=rendered,
                    qq=qq,
                    raw_type="at",
                    raw_data={"qq": qq, "source": "cq-string"},
                )
            )
        else:
            segments.append(ChatSegment(type="text", text=rendered, raw_type="raw"))
        cursor = match.end()
    trailing = clean[cursor:]
    if trailing:
        segments.append(ChatSegment(type="text", text=trailing, raw_type="raw"))
    return tuple(segments)


def _reply_from_cq_text(text: str) -> ChatReply | None:
    match = _CQ_REPLY_RE.search(text)
    if not match:
        return None
    return ChatReply(
        message_id=match.group(1),
        raw_data={"id": match.group(1), "source": "onebot-cq-reply"},
    )


def _reply_from_napcat_display_text(text: str) -> ChatReply | None:
    match = _NAPCAT_DISPLAY_REPLY_RE.search(text)
    if not match:
        return None
    quoted = " ".join(match.group("text").strip().split())
    sender = match.group("sender")
    return ChatReply(
        sender_id=sender,
        text=quoted,
        raw_message=quoted,
        raw_data={"sender_id": sender, "text": quoted, "source": "napcat-display"},
    )


def _reply_from_segment_data(data: dict[str, Any]) -> ChatReply | None:
    message_id = _first_text(data, "id", "message_id")
    if not message_id:
        return None
    sender = _sender_id_from_any(data.get("sender")) or _first_text(data, "qq", "user_id", "sender_id")
    raw_message = _first_text(data, "raw_message") or ""
    text = _first_text(data, "text", "content") or raw_message
    segments: tuple[ChatSegment, ...] = ()
    resources: tuple[ChatResource, ...] = ()
    nested_message = data.get("message")
    if nested_message is not None:
        nested_text, nested_segments, nested_resources, _ = _extract_segments_and_resources(nested_message)
        text = text or nested_text
        segments = nested_segments
        resources = nested_resources
    raw_data = dict(data)
    raw_data["source"] = "onebot-reply-segment"
    return ChatReply(
        message_id=message_id,
        sender_id=sender,
        text=text,
        raw_message=raw_message,
        segments=segments,
        resources=resources,
        raw_data=raw_data,
    )


def _reply_from_message_data(data: dict[str, Any], fallback_id: str = "") -> ChatReply | None:
    message_id = _first_text(data, "message_id", "id") or fallback_id
    if not message_id:
        return None
    sender = _sender_id_from_any(data.get("sender")) or _first_text(data, "user_id", "sender_id", "qq")
    raw_message = _first_text(data, "raw_message") or ""
    text = raw_message
    segments: tuple[ChatSegment, ...] = ()
    resources: tuple[ChatResource, ...] = ()
    message = data.get("message")
    if message is not None:
        text, segments, resources, _ = _extract_segments_and_resources(message)
    raw_data = dict(data)
    raw_data["source"] = "onebot-get-msg"
    return ChatReply(
        message_id=message_id,
        sender_id=sender,
        text=text or raw_message,
        raw_message=raw_message,
        segments=segments,
        resources=resources,
        raw_data=raw_data,
    )


def _first_text(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return None


def _sender_id_from_any(value: Any) -> str | None:
    if isinstance(value, dict):
        return _first_text(value, "user_id", "sender_id", "qq")
    return _str_or_none(value)


def _url_resources(text: str, source_segment: int) -> list[ChatResource]:
    return [
        ChatResource(kind="url", url=url, name=url, source_segment=source_segment)
        for url in _URL_RE.findall(text)
    ]


def _resource_from_segment(raw_type: str, data: dict[str, Any], idx: int) -> ChatResource | None:
    if raw_type == "image":
        return ChatResource(
            kind="image",
            url=_str_or_none(data.get("url")),
            file_id=_str_or_none(data.get("file_id")) or _str_or_none(data.get("file")),
            name=_str_or_none(data.get("file")) or _str_or_none(data.get("name")),
            size=_int_or_none(data.get("file_size", data.get("size"))),
            mime_type=_str_or_none(data.get("mime_type")),
            source_segment=idx,
            raw_data=data,
        )
    if raw_type == "file":
        return ChatResource(
            kind="file",
            url=_str_or_none(data.get("url")),
            file_id=_str_or_none(data.get("file_id")) or _str_or_none(data.get("file")),
            name=(
                _str_or_none(data.get("name"))
                or _str_or_none(data.get("file_name"))
                or _str_or_none(data.get("file"))
            ),
            size=_int_or_none(data.get("file_size", data.get("size"))),
            mime_type=_str_or_none(data.get("mime_type")),
            source_segment=idx,
            raw_data=data,
        )
    if raw_type in {"share", "url"}:
        url = _str_or_none(data.get("url"))
        if not url:
            return None
        return ChatResource(
            kind="url",
            url=url,
            name=_str_or_none(data.get("title")) or _str_or_none(data.get("name")) or url,
            source_segment=idx,
            raw_data=data,
        )
    if raw_type == "record":
        return ChatResource(
            kind="voice",
            url=_str_or_none(data.get("url")),
            file_id=_str_or_none(data.get("file_id")) or _str_or_none(data.get("file")),
            name=_str_or_none(data.get("file")),
            size=_int_or_none(data.get("file_size", data.get("size"))),
            mime_type=_str_or_none(data.get("mime_type")),
            duration_seconds=_int_or_none(data.get("duration"))
            or _int_or_none(data.get("duration_seconds")),
            source_segment=idx,
            raw_data=data,
        )
    if raw_type == "video":
        return ChatResource(
            kind="video",
            url=_str_or_none(data.get("url")),
            file_id=_str_or_none(data.get("file_id")) or _str_or_none(data.get("file")),
            name=_str_or_none(data.get("file")),
            size=_int_or_none(data.get("file_size", data.get("size"))),
            source_segment=idx,
            raw_data=data,
        )
    return None


def _forward_resource_from_segment(raw_type: str, data: dict[str, Any], idx: int) -> ChatResource | None:
    if raw_type == "forward":
        forward_id = _first_text(data, "id", "message_id", "resid")
        name = _first_text(data, "summary", "title", "name", "prompt") or "聊天记录"
        return ChatResource(
            kind="forward",
            file_id=forward_id,
            name=name,
            source_segment=idx,
            raw_data={
                "source": "onebot-forward",
                "id": forward_id,
                "summary": name,
                "messages": _forward_messages_from_any(data.get("messages") or data.get("content")),
            },
        )
    if raw_type == "node":
        messages = _forward_messages_from_any([data])
        if not messages:
            return None
        return ChatResource(
            kind="forward",
            name="聊天记录",
            source_segment=idx,
            raw_data={"source": "onebot-node", "messages": messages},
        )
    if raw_type == "json":
        payload = _json_payload_from_segment(data)
        if not payload:
            return None
        return _forward_resource_from_json_payload(payload, idx)
    return None


def _json_payload_from_segment(data: dict[str, Any]) -> dict[str, Any] | None:
    value = data.get("data")
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _forward_resource_from_json_payload(payload: dict[str, Any], idx: int) -> ChatResource | None:
    app = str(payload.get("app") or "")
    meta = payload.get("meta")
    detail: dict[str, Any] = {}
    if isinstance(meta, dict):
        raw_detail = meta.get("detail")
        if isinstance(raw_detail, dict):
            detail = raw_detail
    if "multimsg" not in app and not any(key in detail for key in ("resid", "news", "summary")):
        return None
    prompt = _str_or_none(payload.get("prompt"))
    summary = _str_or_none(detail.get("summary")) or _str_or_none(payload.get("desc")) or prompt
    forward_id = _str_or_none(detail.get("resid")) or _str_or_none(detail.get("uniseq"))
    messages: list[dict[str, Any]] = []
    news = detail.get("news")
    if isinstance(news, list):
        for item in news:
            if isinstance(item, dict):
                text = _str_or_none(item.get("text"))
                if text:
                    messages.append({"text": text})
    return ChatResource(
        kind="forward",
        file_id=forward_id,
        name=summary or "聊天记录",
        source_segment=idx,
        raw_data={
            "source": "onebot-json",
            "id": forward_id,
            "summary": summary or prompt or "聊天记录",
            "messages": messages,
        },
    )


def _forward_messages_from_any(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("messages", "nodes"):
            if key in value:
                return _forward_messages_from_any(value.get(key))
        return [_forward_message_from_node(value)]
    if isinstance(value, list):
        messages: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                message = _forward_message_from_node(item)
                if message:
                    messages.append(message)
            elif isinstance(item, str) and item.strip():
                messages.append({"text": item.strip()})
        return messages
    if isinstance(value, str) and value.strip():
        return [{"text": value.strip()}]
    return []


def _forward_message_from_node(node: dict[str, Any]) -> dict[str, Any]:
    sender = node.get("sender")
    sender_id = _sender_id_from_any(sender) or _first_text(node, "user_id", "sender_id", "uin", "qq")
    sender_name = _sender_name_from_any(sender) or _first_text(node, "nickname", "name")
    content = node.get("message")
    if content is None:
        content = node.get("content")
    text = _first_text(node, "text", "raw_message") or ""
    resources: tuple[ChatResource, ...] = ()
    if content is not None:
        text, _segments, resources, _reply = _extract_segments_and_resources(content)
    message: dict[str, Any] = {}
    if sender_id:
        message["sender_id"] = sender_id
    if sender_name:
        message["sender_name"] = sender_name
    message["text"] = text
    if resources:
        message["resources"] = [_resource_context_data(resource) for resource in resources]
    if not (sender_id or sender_name or text or resources):
        return {}
    return message


def _sender_name_from_any(value: Any) -> str | None:
    if isinstance(value, dict):
        return _first_text(value, "nickname", "card", "name")
    return None


def _resource_context_data(resource: ChatResource) -> dict[str, Any]:
    data: dict[str, Any] = {"kind": resource.kind}
    if resource.name:
        data["name"] = resource.name
    if resource.url:
        data["url"] = resource.url
    if resource.file_id and not resource.url:
        data["file_id"] = resource.file_id
    return data


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_mentioned(msg: Any, self_id: str, mention_name: str = "") -> bool:
    if isinstance(msg, list):
        for seg in msg:
            if isinstance(seg, dict) and seg.get("type") == "at":
                if str(seg.get("data", {}).get("qq", "")) == str(self_id):
                    return True
    if isinstance(msg, str):
        return any(str(qq) == str(self_id) for qq in _CQ_AT_RE.findall(msg))
    return False


def _normalize_event(raw: dict[str, Any], self_id: str, mention_name: str = "") -> ChatEvent | None:
    if raw.get("post_type") != "message":
        return None
    if self_id and str(raw.get("self_id", "")) != str(self_id):
        return None
    mtype = raw.get("message_type")
    if mtype not in ("private", "group"):
        return None
    is_group = mtype == "group"
    chat_id = str(raw.get("group_id" if is_group else "user_id", ""))
    sender = str(raw.get("user_id", ""))
    if not sender.isdigit() or not chat_id.isdigit():
        return None
    msg = raw.get("message") or raw.get("raw_message", "")
    text, segments, resources, reply = _extract_segments_and_resources(msg)
    mentioned = _is_mentioned(msg, self_id, mention_name) or (not is_group)
    ts = int(raw.get("time", time.time()))
    mid = str(raw.get("message_id", f"{ts}-{sender}"))
    return ChatEvent(
        id=mid,
        platform="qq",
        chat_id=chat_id,
        sender_id=sender,
        is_group=is_group,
        mentioned_bot=mentioned,
        text=text,
        timestamp=ts,
        segments=segments,
        resources=resources,
        reply=reply,
        raw_message=str(raw.get("raw_message", "")),
    )


class OneBotAdapter:
    """Reverse WS server. Gateway connects to us."""

    SEND_ACTION_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        access_token: str,
        self_id: str,
        mention_name: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.path = path.rstrip("/")
        self.access_token = access_token
        self.self_id = self_id or ""
        self.mention_name = mention_name or ""
        self._server: Any = None
        self._handler: MessageHandler | None = None
        self._conns: set[ServerConnection] = set()
        self._pending_actions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_action_connections: dict[str, ServerConnection] = {}
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._recent_messages: dict[str, ChatReply] = {}
        self._recent_message_ids: deque[str] = deque()
        self._max_recent_messages = 500
        self._on_connected: Any = None

    def is_connected(self) -> bool:
        return bool(self._conns)

    def set_on_connected(self, callback: Any) -> None:
        self._on_connected = callback

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler
        addr = f"{self.host}:{self.port}"
        logger.info("starting onebot reverse ws on ws://%s%s", addr, self.path)

        async def _serve(conn: ServerConnection) -> None:
            req_path = getattr(conn.request, "path", "") or ""
            req_target = req_path.split("?", 1)[0].rstrip("/") or "/"
            if req_target != self.path:
                logger.warning("bad path %s", req_path)
                await conn.close(1008, "bad path")
                return
            if self.access_token:
                token = conn.request.headers.get("Authorization", "").replace("Bearer ", "").strip()
                if token != self.access_token:
                    logger.warning("rejected ws conn, bad token")
                    await conn.close(1008, "bad token")
                    return
            self._conns.add(conn)
            logger.info("gateway connected")
            if self._on_connected:
                try:
                    await self._on_connected()
                except Exception:  # noqa: BLE001
                    logger.warning("on_connected callback failed", exc_info=True)
            try:
                async for raw in conn:
                    try:
                        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                        if not isinstance(data, dict):
                            continue
                        if self._complete_action_response(data, conn):
                            continue
                        ev = _normalize_event(data, self.self_id, self.mention_name)
                        if ev and self._handler:
                            self._remember_event(ev)
                            task = asyncio.create_task(self._dispatch_event(ev))
                            self._dispatch_tasks.add(task)
                            task.add_done_callback(self._dispatch_tasks.discard)
                    except Exception as e:  # noqa: BLE001
                        logger.exception("event parse error: %s", e)
            except websockets.ConnectionClosed:
                pass
            finally:
                self._remove_connection(conn)

        self._server = await websockets.serve(
            _serve,
            self.host,
            self.port,
        )

    async def _dispatch_event(self, ev: ChatEvent) -> None:
        try:
            if not self._handler:
                return
            ev = await self._enrich_reply(ev)
            ev = await self._enrich_forward(ev)
            await self._handler(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("event handler error: %s", e)

    async def _enrich_reply(self, ev: ChatEvent) -> ChatEvent:
        reply = ev.reply
        if not reply or not reply.message_id:
            return ev
        if reply.text.strip() and reply.resources:
            return ev
        cached = self._recent_messages.get(reply.message_id)
        if cached:
            enriched = self._merge_reply_context(reply, cached)
            return replace(ev, reply=enriched, resources=self._merge_resources(ev.resources, enriched.resources))
        fetched = await self.fetch_message(reply.message_id)
        if not fetched:
            return ev
        enriched = self._merge_reply_context(reply, fetched)
        return replace(ev, reply=enriched, resources=self._merge_resources(ev.resources, enriched.resources))

    def _merge_reply_context(self, current: ChatReply, enriched: ChatReply) -> ChatReply:
        """Prefer fetched/cache detail while preserving useful preview fields."""
        return replace(
            enriched,
            message_id=enriched.message_id or current.message_id,
            sender_id=enriched.sender_id or current.sender_id,
            text=enriched.text if enriched.text.strip() else current.text,
            raw_message=enriched.raw_message or current.raw_message,
            segments=enriched.segments or current.segments,
            resources=enriched.resources or current.resources,
            raw_data=enriched.raw_data or current.raw_data,
        )

    def _merge_resources(
        self,
        existing: tuple[ChatResource, ...],
        added: tuple[ChatResource, ...],
    ) -> tuple[ChatResource, ...]:
        if not added:
            return existing
        merged = list(existing)
        seen = {
            (
                resource.kind,
                resource.url,
                resource.file_id,
                resource.name,
                resource.source_segment,
            )
            for resource in existing
        }
        for resource in added:
            key = (
                resource.kind,
                resource.url,
                resource.file_id,
                resource.name,
                resource.source_segment,
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(resource)
        return tuple(merged)

    async def _enrich_forward(self, ev: ChatEvent) -> ChatEvent:
        if not ev.resources:
            return ev
        changed = False
        resources: list[ChatResource] = []
        for resource in ev.resources:
            if resource.kind != "forward":
                resources.append(resource)
                continue
            forward_id = (
                resource.file_id
                or _str_or_none(resource.raw_data.get("id"))
                or _str_or_none(resource.raw_data.get("resid"))
            )
            preview_messages = resource.raw_data.get("messages")
            source = resource.raw_data.get("source")
            if preview_messages and source != "onebot-json":
                resources.append(resource)
                continue
            if not forward_id:
                resources.append(resource)
                continue
            messages = await self.fetch_forward_message(forward_id)
            if not messages:
                resources.append(resource)
                continue
            raw_data = dict(resource.raw_data)
            raw_data["messages"] = messages
            raw_data["source"] = "onebot-forward-fetched"
            resources.append(replace(resource, raw_data=raw_data))
            changed = True
        if not changed:
            return ev
        return replace(ev, resources=tuple(resources))

    async def fetch_message(self, message_id: str) -> ChatReply | None:
        result = await self._call_action(
            "get_msg",
            {"message_id": int(message_id) if message_id.isdigit() else message_id},
            timeout=3.0,
        )
        if not isinstance(result, dict):
            return None
        return _reply_from_message_data(result, fallback_id=message_id)

    async def resolve_record_url(self, resource: ChatResource) -> str | None:
        result = await self._call_action(
            "get_record",
            {"file": resource.file_id or resource.url, "out_format": "wav"},
            timeout=5.0,
        )
        if not isinstance(result, dict):
            return None
        for key in ("url", "file", "path"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    async def fetch_forward_message(self, forward_id: str) -> list[dict[str, Any]]:
        result = await self._call_action(
            "get_forward_msg",
            {"message_id": forward_id, "id": forward_id},
            timeout=3.0,
        )
        return _forward_messages_from_any(result)

    def _remember_event(self, ev: ChatEvent) -> None:
        if not ev.id:
            return
        self._recent_messages[ev.id] = ChatReply(
            message_id=ev.id,
            sender_id=ev.sender_id,
            text=ev.text,
            raw_message=ev.raw_message,
            segments=ev.segments,
            resources=ev.resources,
            raw_data={"source": "onebot-recent-cache"},
        )
        self._recent_message_ids.append(ev.id)
        while len(self._recent_message_ids) > self._max_recent_messages:
            old = self._recent_message_ids.popleft()
            if old not in self._recent_message_ids:
                self._recent_messages.pop(old, None)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for task in list(self._dispatch_tasks):
            task.cancel()
        if self._dispatch_tasks:
            await asyncio.gather(*self._dispatch_tasks, return_exceptions=True)
        self._dispatch_tasks.clear()
        for fut in list(self._pending_actions.values()):
            if not fut.done():
                fut.cancel()
        self._pending_actions.clear()
        self._pending_action_connections.clear()
        for c in list(self._conns):
            await c.close()
        self._conns.clear()

    async def send(
        self,
        chat_id: str,
        is_group: bool,
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        await self._send_text_segments(
            chat_id,
            is_group,
            self._reply_segments(reply_to) + [{"type": "text", "data": {"text": text}}],
            echo,
        )

    async def send_at(
        self,
        chat_id: str,
        qq: str,
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        await self.send_ats(chat_id, (qq,), text, echo, reply_to=reply_to)

    async def send_ats(
        self,
        chat_id: str,
        qqs: tuple[str, ...],
        text: str,
        echo: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        segments: list[dict[str, Any]] = self._reply_segments(reply_to)
        for qq in qqs:
            qq_value: str | int = int(qq) if qq.isdigit() else qq
            segments.append({"type": "at", "data": {"qq": qq_value}})
        segments.append({"type": "text", "data": {"text": f" {text}"}})
        await self._send_text_segments(
            chat_id,
            True,
            segments,
            echo,
        )

    def _reply_segments(self, reply_to: str | None) -> list[dict[str, Any]]:
        if not reply_to:
            return []
        reply_id: str | int = int(reply_to) if reply_to.isdigit() else reply_to
        return [{"type": "reply", "data": {"id": reply_id}}]

    async def send_image(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        await self._send_text_segments(
            chat_id,
            is_group,
            [{"type": "image", "data": {"file": self._file_uri(path)}}],
            echo,
        )

    async def send_voice(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        await self._send_text_segments(
            chat_id,
            is_group,
            [{"type": "record", "data": {"file": self._file_uri(path)}}],
            echo,
        )

    async def send_file(
        self,
        chat_id: str,
        is_group: bool,
        path: Path,
        echo: str | None = None,
    ) -> None:
        action = "upload_group_file" if is_group else "upload_private_file"
        params: dict[str, Any] = {"file": self._file_uri(path), "name": path.name}
        if is_group:
            params["group_id"] = int(chat_id) if chat_id.isdigit() else chat_id
        else:
            params["user_id"] = int(chat_id) if chat_id.isdigit() else chat_id
        await self._send_action(action, params, echo)

    def _file_uri(self, path: Path) -> str:
        return path.expanduser().resolve(strict=False).as_uri()

    async def _send_text_segments(
        self,
        chat_id: str,
        is_group: bool,
        message: list[dict[str, Any]],
        echo: str | None = None,
    ) -> None:
        action = "send_group_msg" if is_group else "send_private_msg"
        params: dict[str, Any] = {"message": message}
        if is_group:
            params["group_id"] = int(chat_id) if chat_id.isdigit() else chat_id
        else:
            params["user_id"] = int(chat_id) if chat_id.isdigit() else chat_id
        await self._send_action(action, params, echo)

    async def _send_action(
        self,
        action: str,
        params: dict[str, Any],
        echo: str | None = None,
    ) -> None:
        if not self._conns:
            raise ConnectionError("OneBot gateway is not connected")
        conn = next(iter(self._conns))
        action_echo = echo or f"qq-agent-bridge-send-{time.time_ns()}"
        if action_echo in self._pending_actions:
            raise RuntimeError("OneBot action echo is already pending")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_actions[action_echo] = fut
        self._pending_action_connections[action_echo] = conn
        frame = {"action": action, "params": params, "echo": action_echo}
        try:
            await self._send_frame(conn, json.dumps(frame))
            response = await asyncio.wait_for(fut, timeout=self.SEND_ACTION_TIMEOUT_SECONDS)
        finally:
            self._pending_actions.pop(action_echo, None)
            self._pending_action_connections.pop(action_echo, None)
        if response.get("status") != "ok" or response.get("retcode") != 0:
            raise RuntimeError(f"OneBot action {action} failed")

    async def _call_action(
        self,
        action: str,
        params: dict[str, Any],
        timeout: float,
    ) -> Any:
        if not self._conns:
            logger.warning("no gateway conn, cannot call %s", action)
            return None
        echo = f"qq-agent-bridge-{time.time_ns()}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_actions[echo] = fut
        frame = {"action": action, "params": params, "echo": echo}
        conn = next(iter(self._conns))
        self._pending_action_connections[echo] = conn
        try:
            await self._send_frame(conn, json.dumps(frame))
            response = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("onebot action %s timed out", action)
            return None
        finally:
            self._pending_actions.pop(echo, None)
            self._pending_action_connections.pop(echo, None)
        if response.get("retcode") not in (0, None) or response.get("status") not in ("ok", "async", None):
            logger.warning("onebot action %s failed: %s", action, response)
            return None
        return response.get("data")

    def _complete_action_response(
        self,
        data: dict[str, Any],
        conn: ServerConnection | None = None,
    ) -> bool:
        echo = data.get("echo")
        if echo is None:
            return False
        action_echo = str(echo)
        selected = self._pending_action_connections.get(action_echo)
        if conn is not None and selected is not conn:
            return False
        fut = self._pending_actions.pop(action_echo, None)
        self._pending_action_connections.pop(action_echo, None)
        if not fut:
            return False
        if not fut.done():
            fut.set_result(data)
        return True

    def _remove_connection(self, conn: ServerConnection) -> None:
        self._conns.discard(conn)
        for echo, pending_conn in list(self._pending_action_connections.items()):
            if pending_conn is not conn:
                continue
            self._pending_action_connections.pop(echo, None)
            fut = self._pending_actions.pop(echo, None)
            if fut and not fut.done():
                fut.set_exception(ConnectionError("OneBot gateway disconnected"))

    async def _send_frame(self, conn: ServerConnection, data: str) -> None:
        try:
            await conn.send(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("onebot transport send failed error=%s", type(exc).__name__)
            raise
