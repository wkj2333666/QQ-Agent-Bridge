"""Stage QQ message resources for the agent runtime to consume."""
from __future__ import annotations

import asyncio
import hashlib
import logging

logger = logging.getLogger(__name__)
import ipaddress
import mimetypes
import os
import re
import socket
import stat
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp

from .animated_image import AnimatedImageExtractor, AnimationExtraction
from .config import BridgeConfig
from .types import ChatEvent, ChatResource, TranscriptStatus
from .whisper_runner import TranscriptionResult, WhisperRunner

FetchFunc = Callable[[str, int], Awaitable[tuple[bytes, str]]]
RecordUrlFunc = Callable[[ChatResource], Awaitable[str | None]]
ImageUrlFunc = Callable[[ChatResource], Awaitable[str | None]]
AnimationExtractFunc = Callable[[Path, Path], Awaitable[AnimationExtraction]]

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
    transcript: str | None = None
    transcript_status: TranscriptStatus | None = None
    transcript_language: str | None = None
    transcript_error: str | None = None
    animation_status: str | None = None
    animation_frame_count: int | None = None
    animation_duration_seconds: float | None = None
    animation_frame_paths: tuple[str, ...] = ()
    animation_frame_times: tuple[float, ...] = ()
    animation_error: str | None = None


class ResourceManager:
    """Download explicit QQ attachments into a workspace-local staging area."""

    def __init__(
        self,
        cfg: BridgeConfig,
        fetch: FetchFunc | None = None,
        record_url: RecordUrlFunc | None = None,
        image_url: ImageUrlFunc | None = None,
        transcriber: WhisperRunner | None = None,
        animation_extractor: AnimationExtractFunc | None = None,
    ) -> None:
        self.cfg = cfg
        self.fetch = fetch or self._fetch_http
        self.record_url = record_url
        self.image_url = image_url
        self.transcriber = transcriber
        self.animation_extractor = animation_extractor or AnimatedImageExtractor(cfg.resources).extract

    async def prepare(self, ev: ChatEvent) -> tuple[PreparedResource, ...]:
        if not self.cfg.resources.enabled:
            return ()
        if not ev.resources:
            logger.info("resource_prepare_empty chat_id=%s text=%s", ev.chat_id, ev.text[:80])
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
            if resource.kind == "voice":
                prepared, consumed = await self._prepare_voice(
                    resource, idx, root, workspace, ev, total_bytes
                )
                total_bytes += consumed
                if total_bytes > self.cfg.resources.max_total_bytes:
                    break
                refs.append(prepared)
                continue
            if resource.kind == "image":
                prepared, consumed = await self._prepare_image(
                    resource, idx, root, workspace, ev, total_bytes
                )
                total_bytes += consumed
                if total_bytes > self.cfg.resources.max_total_bytes:
                    break
                refs.append(prepared)
                continue
            if not resource.url:
                logger.info(
                    "resource_skip_no_url chat_id=%s kind=%s file_id=%s",
                    ev.chat_id, resource.kind, resource.file_id or "",
                )
                continue
            try:
                payload, content_type = await self.fetch(resource.url, self.cfg.resources.max_bytes)
            except Exception:  # noqa: BLE001 - resource passthrough should degrade softly
                logger.info(
                    "resource_download_failed chat_id=%s kind=%s url=%s",
                    ev.chat_id, resource.kind, (resource.url or "")[:120],
                )
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
            prepared = PreparedResource(
                kind=resource.kind,
                name=resource.name,
                local_path=target.relative_to(workspace).as_posix(),
                duration_seconds=resource.duration_seconds,
            )
            if self._is_animation_candidate(resource, content_type, target, payload):
                prepared = await self._with_animation(prepared, target, workspace)
            refs.append(prepared)
        return tuple(refs)

    async def _with_animation(
        self,
        prepared: PreparedResource,
        source: Path,
        workspace: Path,
    ) -> PreparedResource:
        if not self.cfg.resources.animation_enabled:
            return prepared
        try:
            result = await self.animation_extractor(source, workspace)
        except Exception:  # noqa: BLE001 - original image remains usable
            return replace(
                prepared,
                animation_status="unavailable",
                animation_error="animation extraction failed",
            )
        if result.status == "static":
            return prepared
        return replace(
            prepared,
            animation_status=result.status,
            animation_frame_count=result.source_frame_count,
            animation_duration_seconds=result.duration_seconds,
            animation_frame_paths=result.frame_paths,
            animation_frame_times=result.frame_times,
            animation_error=result.error,
        )

    @staticmethod
    def _is_animation_candidate(
        resource: ChatResource,
        content_type: str,
        target: Path,
        payload: bytes,
    ) -> bool:
        if resource.kind not in {"image", "file"}:
            return False
        mime = (content_type or resource.mime_type or "").split(";", 1)[0].strip().lower()
        if mime in {"image/gif", "image/apng", "image/webp"}:
            return True
        names = (resource.name or "", resource.url or "", target.name)
        if any(
            urlsplit(value).path.lower().endswith((".gif", ".apng", ".webp"))
            for value in names
            if value
        ):
            return True
        return ResourceManager._is_apng_payload(payload)

    @staticmethod
    def _is_apng_payload(payload: bytes) -> bool:
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        offset = 8
        while offset + 12 <= len(payload):
            length = int.from_bytes(payload[offset : offset + 4], "big")
            chunk_type = payload[offset + 4 : offset + 8]
            if chunk_type == b"acTL":
                return True
            if chunk_type in {b"IDAT", b"IEND"}:
                return False
            offset += 12 + length
        return False

    def cleanup_prepared(self, resources: tuple[PreparedResource, ...]) -> None:
        try:
            workspace = Path(self.cfg.agent.default_workspace).expanduser().resolve(strict=False)
            root = self._resource_root(workspace)
        except (OSError, ValueError):
            return
        parents: set[Path] = set()
        for resource in resources:
            for relative in resource.animation_frame_paths:
                path = (workspace / relative).resolve(strict=False)
                try:
                    path.relative_to(root)
                except ValueError:
                    continue
                try:
                    if path.is_file() and not path.is_symlink():
                        path.unlink()
                        parents.add(path.parent)
                except OSError:
                    continue
        for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
            try:
                parent.rmdir()
            except OSError:
                pass

    async def _prepare_voice(
        self,
        resource: ChatResource,
        idx: int,
        root: Path,
        workspace: Path,
        ev: ChatEvent,
        total_bytes: int,
    ) -> tuple[PreparedResource, int]:
        source_url: str | None = None
        converted = False
        conversion_attempted = False
        if self.record_url and self._voice_needs_conversion(resource):
            conversion_attempted = True
            try:
                source_url = await self.record_url(resource)
            except Exception:  # noqa: BLE001 - one converter failure must not discard the event
                source_url = None
            converted = bool(source_url)
        if (
            source_url is None
            and resource.url
            and not conversion_attempted
            and not self._is_silk(resource)
        ):
            source_url = resource.url
        if not source_url:
            return self._unavailable_voice(resource, "QQ voice conversion unavailable"), 0

        if self._is_http_url(source_url):
            try:
                payload, content_type = await self.fetch(source_url, self.cfg.resources.max_bytes)
            except Exception:  # noqa: BLE001 - media staging is an optional enrichment
                return self._unavailable_voice(resource, "QQ voice download unavailable"), 0
        else:
            try:
                payload, content_type = self._read_local_record(source_url, total_bytes, resource)
            except ValueError as exc:
                return self._unavailable_voice(resource, str(exc)), 0
        if total_bytes + len(payload) > self.cfg.resources.max_total_bytes:
            return self._unavailable_voice(resource, "QQ voice download limit exceeded"), len(payload)
        if converted and not self._is_valid_wav_payload(payload):
            return self._unavailable_voice(resource, "QQ voice conversion returned invalid WAV"), len(payload)

        event_dir = root / datetime.fromtimestamp(ev.timestamp).strftime("%Y-%m-%d") / self._safe_event_id(ev.id)
        try:
            event_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            return self._unavailable_voice(resource, "QQ voice staging unavailable"), len(payload)
        name = self._voice_name(idx, payload, content_type, resource, converted)
        target = (event_dir / name).resolve(strict=False)
        if root not in target.parents:
            return self._unavailable_voice(resource, "QQ voice staging unavailable"), len(payload)
        try:
            target.write_bytes(payload)
        except OSError:
            return self._unavailable_voice(resource, "QQ voice staging unavailable"), len(payload)

        local_path = target.relative_to(workspace).as_posix()
        prepared = PreparedResource(
            kind="voice",
            name=resource.name,
            local_path=local_path,
            duration_seconds=resource.duration_seconds,
        )
        if not self.transcriber:
            return prepared, len(payload)
        result = await self._transcribe_voice(target)
        return self._with_transcript(prepared, result), len(payload)

    def _voice_needs_conversion(self, resource: ChatResource) -> bool:
        return bool(resource.file_id) or self._is_silk(resource)

    @staticmethod
    def _is_http_url(value: str) -> bool:
        return urlsplit(value).scheme.lower() in {"http", "https"}

    def _read_local_record(
        self, source: str, total_bytes: int, resource: ChatResource
    ) -> tuple[bytes, str]:
        source_path = Path(source).expanduser()
        try:
            path = source_path.resolve(strict=False)
        except OSError as exc:
            raise ValueError("QQ voice local file unavailable") from exc
        if (
            source_path.is_symlink()
            or not path.is_file()
            or not self._is_trusted_local_media_path(path)
        ):
            raise ValueError("QQ voice local file unavailable")
        mime_type = mimetypes.guess_type(path.name)[0]
        if not mime_type or not mime_type.startswith("audio/"):
            raise ValueError("QQ voice local file unavailable")
        remaining = self.cfg.resources.max_total_bytes - total_bytes
        limit = min(self.cfg.resources.max_bytes, remaining)
        if limit <= 0:
            raise ValueError("QQ voice download limit exceeded")
        fd = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(source_path, flags)
            descriptor_stat = os.fstat(fd)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                raise ValueError("QQ voice local file unavailable")
            descriptor_path = Path(os.path.realpath(f"/proc/self/fd/{fd}"))
            if not self._is_trusted_local_media_path(descriptor_path):
                raise ValueError("QQ voice local file unavailable")
            if descriptor_stat.st_size > limit:
                raise ValueError("QQ voice download limit exceeded")
            with os.fdopen(fd, "rb") as file:
                fd = -1
                payload = file.read(limit + 1)
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError("QQ voice local file unavailable") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if len(payload) > limit:
            raise ValueError("QQ voice download limit exceeded")
        if not self._is_valid_wav_payload(payload):
            raise ValueError("QQ voice local file unavailable")
        return payload, mime_type

    @staticmethod
    def _is_valid_wav_payload(payload: bytes) -> bool:
        try:
            with wave.open(BytesIO(payload), "rb"):
                return True
        except (EOFError, wave.Error):
            return False

    def _is_trusted_local_media_path(self, path: Path) -> bool:
        roots = self.cfg.resources.local_media_roots
        if not isinstance(roots, list):
            return False
        for root in roots:
            if not isinstance(root, str) or not root.strip():
                continue
            try:
                path.relative_to(Path(root).expanduser().resolve(strict=False))
            except (OSError, ValueError):
                continue
            return True
        return False

    @staticmethod
    def _is_silk(resource: ChatResource) -> bool:
        values = (resource.url, resource.file_id, resource.name, resource.mime_type)
        return any(value and "silk" in value.lower() for value in values)

    def _voice_name(
        self,
        idx: int,
        payload: bytes,
        content_type: str,
        resource: ChatResource,
        converted: bool,
    ) -> str:
        if converted:
            digest = hashlib.sha256(payload).hexdigest()[:12]
            return f"{idx:02d}-{digest}.wav"
        return self._generated_name(idx, payload, content_type, resource)

    async def _prepare_image(
        self,
        resource: ChatResource,
        idx: int,
        root: Path,
        workspace: Path,
        ev: ChatEvent,
        total_bytes: int,
    ) -> tuple[PreparedResource, int]:
        source_url: str | None = resource.url
        if self.image_url:
            try:
                resolved = await self.image_url(resource)
            except Exception:
                resolved = None
            if resolved:
                source_url = resolved
        if not source_url:
            return self._unavailable_resource(resource, "image", "QQ image URL unavailable"), 0

        if self._is_http_url(source_url):
            try:
                payload, content_type = await self.fetch(source_url, self.cfg.resources.max_bytes)
            except Exception:
                return self._unavailable_resource(resource, "image", "QQ image download unavailable"), 0
        else:
            try:
                payload, content_type = self._read_local_record(source_url, total_bytes, resource)
            except ValueError as exc:
                return self._unavailable_resource(resource, "image", str(exc)), 0

        if total_bytes + len(payload) > self.cfg.resources.max_total_bytes:
            return self._unavailable_resource(resource, "image", "QQ image download limit exceeded"), len(payload)

        event_dir = root / datetime.fromtimestamp(ev.timestamp).strftime("%Y-%m-%d") / self._safe_event_id(ev.id)
        try:
            event_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            return self._unavailable_resource(resource, "image", "image staging unavailable"), len(payload)

        name = self._generated_name(idx, payload, content_type, resource)
        target = (event_dir / name).resolve(strict=False)
        if root not in target.parents:
            return self._unavailable_resource(resource, "image", "image staging unavailable"), len(payload)
        target.write_bytes(payload)

        prepared = PreparedResource(
            kind="image",
            name=resource.name,
            local_path=target.relative_to(workspace).as_posix(),
        )
        if self._is_animation_candidate(resource, content_type, target, payload):
            prepared = await self._with_animation(prepared, target, workspace)
        return prepared, len(payload)

    @staticmethod
    def _unavailable_voice(resource: ChatResource, error: str) -> PreparedResource:
        return PreparedResource(
            kind="voice",
            name=resource.name or resource.file_id,
            duration_seconds=resource.duration_seconds,
            transcript_status="unavailable",
            transcript_error=error,
        )

    @staticmethod
    def _unavailable_resource(
        resource: ChatResource, kind: str, error: str
    ) -> PreparedResource:
        return PreparedResource(
            kind=kind,
            name=resource.name or resource.file_id,
        )

    async def _transcribe_voice(self, path: Path) -> TranscriptionResult:
        assert self.transcriber is not None
        try:
            return await self.transcriber.transcribe(path)
        except Exception:  # noqa: BLE001 - transcription must degrade softly per resource
            return TranscriptionResult(None, "failed", None, None)

    @staticmethod
    def _with_transcript(
        resource: PreparedResource, result: TranscriptionResult
    ) -> PreparedResource:
        text = (result.text or "").strip()
        if result.status == "ok" and text:
            return replace(
                resource,
                transcript=text,
                transcript_status="verified",
                transcript_language=result.language,
            )
        if result.status == "timeout":
            error = "Whisper timeout"
        elif result.status == "failed":
            error = "Whisper failed"
        else:
            error = (result.error or "Whisper unavailable").strip()[:500]
        return replace(
            resource,
            transcript_status="unavailable",
            transcript_error=error,
        )

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
        for value in (resource.name or "", resource.url or ""):
            suffix = Path(urlsplit(value).path).suffix.lower()
            if suffix in {".gif", ".apng", ".webp"}:
                return suffix
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
        current_url = url
        async with aiohttp.ClientSession() as session:
            for _ in range(4):
                await self._validate_http_target(current_url)
                async with session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if 300 <= resp.status < 400:
                        location = resp.headers.get("Location")
                        if not location:
                            raise ValueError("redirect without location")
                        current_url = urljoin(current_url, location)
                        continue
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
        raise ValueError("too many redirects")

    async def _validate_http_target(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("unsupported resource URL")
        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise ValueError("invalid resource URL") from exc
        try:
            infos = await asyncio.get_running_loop().run_in_executor(
                None,
                socket.getaddrinfo,
                parsed.hostname,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
            addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
        except OSError as exc:
            raise ValueError("resource host unavailable") from exc
        if not addresses or any(
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
            for address in addresses
        ):
            raise ValueError("private network resource target")


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
            _append_animation_context(lines, res)
            _append_transcript_context(lines, res)
        elif res.kind == "voice":
            name = res.name or "unavailable voice"
            lines.append(f"- voice: {name}{details}")
            _append_transcript_context(lines, res)
        elif res.url:
            lines.append(f"- {res.kind}: {res.url}{details}")
    return "\n".join(lines)


def _append_animation_context(lines: list[str], resource: PreparedResource) -> None:
    if resource.animation_status == "ready":
        count = resource.animation_frame_count or "unknown"
        duration = (
            f"{resource.animation_duration_seconds:.3f}s"
            if resource.animation_duration_seconds is not None
            else "unknown"
        )
        lines.append(
            f"  animated image: source_frames={count}, duration={duration}, "
            f"sampled_frames={len(resource.animation_frame_paths)}"
        )
        for index, path in enumerate(resource.animation_frame_paths):
            if index < len(resource.animation_frame_times):
                timestamp = resource.animation_frame_times[index]
                lines.append(f"  sampled frame {index + 1} (t={timestamp:.3f}s): {path}")
            else:
                lines.append(f"  sampled frame {index + 1}: {path}")
        lines.append("  动图理解必须结合这些采样帧；首帧不能代表完整动图。")
    elif resource.animation_status == "unavailable":
        error = resource.animation_error or "animation extraction unavailable"
        lines.append(f"  dynamic evidence unavailable ({error}); 不得把首帧当作完整动图。")


def _append_transcript_context(lines: list[str], resource: PreparedResource) -> None:
    if resource.transcript_status == "verified" and resource.transcript:
        language = resource.transcript_language or "unknown"
        lines.append(
            f"  transcript (verified by local Whisper, language={language}): {resource.transcript}"
        )
    elif resource.transcript_status == "unavailable":
        error = resource.transcript_error or "Whisper unavailable"
        lines.append(f"  transcript: unavailable ({error})")


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
