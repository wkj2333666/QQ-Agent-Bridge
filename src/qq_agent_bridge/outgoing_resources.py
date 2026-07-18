"""Parse agent output directives for sending QQ resources."""
from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import stat
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import BridgeConfig

MAX_QQ_VOICE_SECONDS = 60
_MISSING_FILE_WARNING = "无法发送资源：文件不存在或不是普通文件"
_IMAGE_SUFFIXES = frozenset(
    {".apng", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)

_DIRECTIVE_RE = re.compile(
    r"^\s*QQBOT_SEND_(IMAGE|FILE|VOICE|AUDIO)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArtifactInspection:
    clean_text: str
    resources: tuple["OutgoingResource", ...]
    warnings: tuple[str, ...]
    attempted: int
    unresolved: int
    recovered: int


@dataclass(frozen=True)
class OutgoingResource:
    kind: str
    path: Path
    name: str
    duration_seconds: int | None = None
    source_path: Path | None = None
    size_bytes: int = 0


def collect_outgoing_resources(
    text: str,
    cfg: BridgeConfig,
    *,
    outbox_dir: Path | str | None = None,
    token: str | None = None,
    job_id: str = "job",
    expected_outbox: tuple[int, int] | None = None,
) -> tuple[str, tuple[OutgoingResource, ...], list[str]]:
    result = inspect_outgoing_resources(
        text,
        cfg,
        outbox_dir=outbox_dir,
        token=token,
        job_id=job_id,
        expected_outbox=expected_outbox,
    )
    return result.clean_text, result.resources, list(result.warnings)


def inspect_outgoing_resources(
    text: str,
    cfg: BridgeConfig,
    *,
    outbox_dir: Path | str | None = None,
    token: str | None = None,
    job_id: str = "job",
    expected_outbox: tuple[int, int] | None = None,
    discover_unique: bool = True,
) -> ArtifactInspection:
    workspace = Path(cfg.agent.default_workspace).expanduser().resolve(strict=False)
    workspace_allowed = cfg.is_workspace_allowed(str(workspace))
    outbox_path = Path(outbox_dir).expanduser() if outbox_dir else None
    resources: list[OutgoingResource] = []
    warnings: list[str] = []
    kept_lines: list[str] = []
    total_bytes = 0
    attempted = 0
    unresolved = 0
    recovered = 0
    seen_sources: set[tuple[str, str]] = set()

    def warn(message: str) -> None:
        nonlocal unresolved
        warnings.append(message)
        unresolved += 1

    def stage_resource(
        raw_path: str,
        kind: str,
        directive_kind: str,
        duration_seconds: int | None,
    ) -> OutgoingResource | None:
        nonlocal total_bytes
        resolved = _resolve_workspace_path(raw_path, workspace)
        if resolved is None:
            warn("已拒绝发送资源：路径不在工作区内")
            return None
        if outbox is None or not _is_relative_to(resolved, outbox):
            warn("已拒绝发送资源：路径不在本次任务输出目录内")
            return None
        source_key = kind, str(resolved)
        if source_key in seen_sources:
            return None
        seen_sources.add(source_key)
        if len(resources) >= max(0, cfg.resources.max_items):
            warn("无法发送资源：超过发送数量限制")
            return None
        try:
            source_stat = resolved.stat()
        except OSError:
            warn(_MISSING_FILE_WARNING)
            return None
        if not stat.S_ISREG(source_stat.st_mode):
            warn(_MISSING_FILE_WARNING)
            return None
        if source_stat.st_nlink != 1:
            warn("无法发送资源：文件不是本次任务生成的独立文件")
            return None
        size = source_stat.st_size
        if size > cfg.resources.max_bytes:
            warn("无法发送资源：文件超过大小限制")
            return None
        if total_bytes + size > cfg.resources.max_total_bytes:
            warn("无法发送资源：资源总大小超过限制")
            return None
        if directive_kind == "voice":
            actual_duration = _probe_audio_duration_seconds(resolved)
            if actual_duration is None:
                warn("无法发送QQ语音：无法验证实际时长")
                return None
            if actual_duration > MAX_QQ_VOICE_SECONDS:
                warn("无法发送QQ语音：实际时长超过60秒限制")
                return None
            duration_seconds = max(1, round(actual_duration))
        stable = _copy_for_sending(resolved, workspace, cfg, job_id, len(resources), source_stat)
        if stable is None:
            warn("无法发送资源：文件状态变化，已拒绝发送")
            return None
        resource = OutgoingResource(
            kind=kind,
            path=stable,
            name=stable.name,
            duration_seconds=duration_seconds,
            source_path=resolved,
            size_bytes=size,
        )
        resources.append(resource)
        total_bytes += size
        return resource

    outbox: Path | None = None

    for line in text.splitlines():
        match = _DIRECTIVE_RE.match(line)
        if not match:
            kept_lines.append(line)
            continue
        attempted += 1
        directive_kind = match.group(1).lower()
        kind = "file" if directive_kind == "audio" else directive_kind
        if not workspace_allowed:
            warn("已拒绝发送资源：工作区未授权")
            continue
        if outbox_path is None or not token:
            warn("已拒绝发送资源：当前任务未启用资源发送")
            continue
        outbox, outbox_warning = _validate_outbox(outbox_path, workspace, expected_outbox)
        if outbox_warning:
            warn(outbox_warning)
            continue
        parsed = _parse_token_path(match.group(2), token)
        if parsed is None:
            warn("已拒绝发送资源：令牌不匹配")
            continue
        raw_path, metadata = parsed
        duration_seconds = None
        if directive_kind == "voice":
            duration_seconds = _duration_seconds_from_metadata(metadata)
            if duration_seconds is None:
                warn("无法发送QQ语音：缺少时长元数据，需确认不超过60秒")
                continue
            if duration_seconds > MAX_QQ_VOICE_SECONDS:
                warn("无法发送QQ语音：时长超过60秒限制")
                continue
        recovered_path = _recover_glued_path(raw_path, workspace, outbox)
        if recovered_path is not None:
            raw_path = recovered_path
            recovered += 1
        stage_resource(raw_path, kind, directive_kind, duration_seconds)

    if (
        discover_unique
        and not resources
        and workspace_allowed
        and outbox_path is not None
        and token
        and (
            attempted == 0
            or (
                attempted == 1
                and unresolved == 1
                and warnings == [_MISSING_FILE_WARNING]
            )
        )
    ):
        outbox, outbox_warning = _validate_outbox(outbox_path, workspace, expected_outbox)
        if outbox_warning is None and outbox is not None:
            candidates = _eligible_top_level_files(outbox)
            if len(candidates) == 1:
                candidate = candidates[0]
                kind = "image" if candidate.suffix.lower() in _IMAGE_SUFFIXES else "file"
                if stage_resource(candidate.as_posix(), kind, kind, None) is not None:
                    if attempted == 1:
                        warnings.remove(_MISSING_FILE_WARNING)
                        unresolved -= 1
                    recovered += 1

    cleaned = "\n".join(line for line in kept_lines).strip()
    return ArtifactInspection(
        clean_text=cleaned,
        resources=tuple(resources),
        warnings=tuple(warnings),
        attempted=attempted,
        unresolved=unresolved,
        recovered=recovered,
    )


def _recover_glued_path(raw_path: str, workspace: Path, outbox: Path) -> str | None:
    matches: list[str] = []
    for candidate in _eligible_top_level_files(outbox):
        absolute = candidate.as_posix()
        relative = candidate.relative_to(workspace).as_posix()
        for shown in (absolute, relative):
            if raw_path.startswith(shown) and raw_path != shown:
                matches.append(shown)
    if not matches:
        return None
    longest = max(len(value) for value in matches)
    winners = sorted({value for value in matches if len(value) == longest})
    return winners[0] if len(winners) == 1 else None


def _eligible_top_level_files(outbox: Path) -> list[Path]:
    eligible: list[Path] = []
    try:
        candidates = outbox.iterdir()
    except OSError:
        return eligible
    for candidate in candidates:
        if candidate.name.startswith("."):
            continue
        try:
            candidate_stat = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISREG(candidate_stat.st_mode) and candidate_stat.st_nlink == 1:
            eligible.append(candidate)
    return eligible


def _parse_token_path(value: str, expected_token: str) -> tuple[str, list[str]] | None:
    try:
        parts = shlex.split(value.strip())
    except ValueError:
        return None
    if len(parts) < 2 or parts[0] != expected_token:
        return None
    return _strip_quotes(parts[1]), parts[2:]


def _duration_seconds_from_metadata(values: list[str]) -> int | None:
    for value in values:
        raw = value.strip()
        if "=" in raw:
            key, raw = raw.split("=", 1)
            if key not in {"duration", "duration_seconds", "seconds"}:
                continue
        try:
            duration = int(raw)
        except ValueError:
            return None
        return duration if duration > 0 else None
    return None


def _probe_audio_duration_seconds(path: Path) -> float | None:
    suffix = path.suffix.lower()
    if suffix == ".wav":
        try:
            with wave.open(str(path), "rb") as wav:
                framerate = wav.getframerate()
                if framerate <= 0:
                    return None
                return wav.getnframes() / float(framerate)
        except (OSError, EOFError, wave.Error):
            return None
    return None


def _validate_outbox(
    outbox_path: Path | None,
    workspace: Path,
    expected_outbox: tuple[int, int] | None,
) -> tuple[Path | None, str | None]:
    if outbox_path is None:
        return None, "已拒绝发送资源：当前任务未启用资源发送"
    try:
        current = outbox_path.lstat()
    except OSError:
        return None, "已拒绝发送资源：输出目录未授权"
    if expected_outbox and (current.st_dev, current.st_ino) != expected_outbox:
        return None, "已拒绝发送资源：输出目录状态变化"
    if not stat.S_ISDIR(current.st_mode):
        return None, "已拒绝发送资源：输出目录未授权"
    resolved = outbox_path.resolve(strict=False)
    if not _is_relative_to(resolved, workspace):
        return None, "已拒绝发送资源：输出目录未授权"
    return resolved, None


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1].strip()
    return text


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _resolve_workspace_path(raw_path: str, workspace: Path) -> Path | None:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return None
    return resolved


def _copy_for_sending(
    source: Path,
    workspace: Path,
    cfg: BridgeConfig,
    job_id: str,
    index: int,
    expected_stat: os.stat_result,
) -> Path | None:
    sending_dir = (workspace / cfg.resources.root / "sending" / _safe_segment(job_id)).resolve(
        strict=False
    )
    if not _is_relative_to(sending_dir, workspace):
        raise ValueError("sending directory must stay inside workspace")
    sending_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_name = _safe_filename(source.name)
    target = sending_dir / f"{index:02d}-{secrets.token_hex(4)}-{safe_name}"
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(source, flags)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as src:
        current = os.fstat(src.fileno())
        expected = (
            expected_stat.st_dev,
            expected_stat.st_ino,
            expected_stat.st_size,
            expected_stat.st_mode,
            expected_stat.st_nlink,
        )
        actual = (current.st_dev, current.st_ino, current.st_size, current.st_mode, current.st_nlink)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or actual != expected
        ):
            return None
        try:
            with target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            return target.resolve(strict=True)
        except OSError:
            return None


def _safe_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return safe[:64] or "job"


def _safe_filename(value: str) -> str:
    name = Path(value).name
    path = Path(name)
    suffix = path.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", path.suffix) else ""
    stem = name[: -len(suffix)] if suffix else name
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip(".-")
    safe_stem = safe_stem[: max(1, 96 - len(suffix))].strip(".-")
    return f"{safe_stem or 'resource'}{suffix}"[:96]
