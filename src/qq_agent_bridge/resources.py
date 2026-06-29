"""Stage QQ message resources for the agent runtime to consume."""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp

from .config import BridgeConfig
from .types import ChatEvent, ChatResource

FetchFunc = Callable[[str, int], Awaitable[tuple[bytes, str]]]

MAX_FORWARD_TITLE_CHARS = 120
MAX_FORWARD_ITEM_CHARS = 500
MAX_FORWARD_CONTEXT_CHARS = 4000


@dataclass(frozen=True)
class PreparedResource:
    kind: str
    name: str | None = None
    local_path: str | None = None
    url: str | None = None
    duration_seconds: int | None = None
    text: str | None = None


class ResourceManager:
    """Download explicit QQ attachments into a workspace-local staging area."""

    def __init__(self, cfg: BridgeConfig, fetch: FetchFunc | None = None) -> None:
        self.cfg = cfg
        self.fetch = fetch or self._fetch_http

    async def prepare(self, ev: ChatEvent) -> tuple[PreparedResource, ...]:
        if not self.cfg.resources.enabled or not ev.resources:
            return ()
        workspace = Path(self.cfg.agent.default_workspace).expanduser().resolve(strict=False)
        if not self.cfg.is_workspace_allowed(str(workspace)):
            return ()
        root = self._resource_root(workspace)
        refs: list[PreparedResource] = []
        total_bytes = 0
        for idx, resource in enumerate(ev.resources[: max(0, self.cfg.resources.max_items)]):
            if resource.kind not in self.cfg.resources.allowed_kinds:
                continue
            if resource.kind == "forward":
                refs.append(
                    PreparedResource(
                        kind="forward",
                        name=resource.name or resource.file_id,
                        text=_format_forward_resource(resource),
                    )
                )
                continue
            if resource.kind == "url":
                if resource.url:
                    refs.append(
                        PreparedResource(kind="url", name=resource.name or resource.url, url=resource.url)
                    )
                continue
            if not resource.url:
                continue
            try:
                payload, content_type = await self.fetch(resource.url, self.cfg.resources.max_bytes)
            except Exception:  # noqa: BLE001 - resource passthrough should degrade softly
                continue
            total_bytes += len(payload)
            if total_bytes > self.cfg.resources.max_total_bytes:
                break
            event_dir = root / datetime.fromtimestamp(ev.timestamp).strftime("%Y-%m-%d") / self._safe_event_id(ev.id)
            event_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            name = self._generated_name(idx, payload, content_type, resource)
            target = (event_dir / name).resolve(strict=False)
            if root not in target.parents:
                continue
            target.write_bytes(payload)
            refs.append(
                PreparedResource(
                    kind=resource.kind,
                    name=resource.name,
                    local_path=target.relative_to(workspace).as_posix(),
                    duration_seconds=resource.duration_seconds,
                )
            )
        return tuple(refs)

    def _resource_root(self, workspace: Path) -> Path:
        root = (workspace / self.cfg.resources.root).resolve(strict=False)
        try:
            root.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("resource root must stay inside workspace") from exc
        return root

    def _safe_event_id(self, event_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", event_id).strip(".-")
        digest = hashlib.sha256(event_id.encode("utf-8", "replace")).hexdigest()[:8]
        return f"ev-{safe[:32] or 'message'}-{digest}"

    def _generated_name(
        self,
        idx: int,
        payload: bytes,
        content_type: str,
        resource: ChatResource,
    ) -> str:
        digest = hashlib.sha256(payload).hexdigest()[:12]
        ext = self._extension(content_type, resource)
        return f"{idx:02d}-{digest}{ext}"

    def _extension(self, content_type: str, resource: ChatResource) -> str:
        mime = (content_type or resource.mime_type or "").split(";", 1)[0].strip().lower()
        if mime == "image/jpeg":
            return ".jpg"
        guessed = mimetypes.guess_extension(mime) if mime else None
        if guessed:
            return guessed
        if resource.kind == "image":
            return ".img"
        if resource.kind == "audio":
            return ".audio"
        if resource.kind == "voice":
            return ".voice"
        if resource.kind == "video":
            return ".video"
        return ".bin"

    async def _fetch_http(self, url: str, limit: int) -> tuple[bytes, str]:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("resource too large")
                    chunks.append(chunk)
                return b"".join(chunks), content_type


def format_resource_context(resources: tuple[PreparedResource, ...]) -> str:
    lines: list[str] = []
    for res in resources:
        details = ""
        if res.kind == "voice":
            if res.duration_seconds is not None:
                details = f" duration={res.duration_seconds}s, QQ voice limit=60s"
            else:
                details = " QQ voice limit=60s"
        if res.text:
            lines.append(res.text)
        elif res.local_path:
            lines.append(f"- {res.kind}: {res.local_path}{details}")
        elif res.url:
            lines.append(f"- {res.kind}: {res.url}{details}")
    return "\n".join(lines)


def _format_forward_resource(resource: ChatResource) -> str:
    title = _clip(resource.name or resource.file_id or "聊天记录", MAX_FORWARD_TITLE_CHARS)
    lines = [f"- QQ批量转发：{title}"]
    messages = resource.raw_data.get("messages")
    if not isinstance(messages, list) or not messages:
        marker = resource.file_id or resource.raw_data.get("id") or resource.raw_data.get("resid")
        if marker:
            lines.append(f"  - 合并转发ID：{marker}")
        lines.append("  - 内容：当前事件只携带了转发摘要，未取得完整聊天记录")
        return "\n".join(lines)

    for item in messages[:20]:
        if not isinstance(item, dict):
            continue
        sender = _format_forward_sender(item)
        text = _clip(str(item.get("text") or "").strip(), MAX_FORWARD_ITEM_CHARS)
        resources = item.get("resources")
        if text:
            lines.append(f"  - {sender}: {text}" if sender else f"  - {text}")
        if isinstance(resources, list):
            for res in resources[:5]:
                if not isinstance(res, dict):
                    continue
                desc = _format_forward_nested_resource(res)
                if not desc:
                    continue
                lines.append(f"  - {sender}: {desc}" if sender else f"  - {desc}")
    return _clip("\n".join(lines), MAX_FORWARD_CONTEXT_CHARS)


def _format_forward_sender(item: dict[str, object]) -> str:
    sender_id = str(item.get("sender_id") or "").strip()
    sender_name = str(item.get("sender_name") or "").strip()
    if sender_name and sender_id:
        return f"{sender_name}({sender_id})"
    return sender_name or sender_id


def _format_forward_nested_resource(res: dict[str, object]) -> str:
    kind = str(res.get("kind") or "resource")
    name = str(res.get("name") or res.get("file_id") or res.get("url") or kind)
    url = str(res.get("url") or "")
    return _clip(f"[{kind}] {name} {url}".strip(), MAX_FORWARD_ITEM_CHARS)


def _clip(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
