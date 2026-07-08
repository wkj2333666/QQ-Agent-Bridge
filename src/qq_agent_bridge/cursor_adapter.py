"""CLI agent subprocess adapters."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import shutil
import stat
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .progress_directives import ProgressLineBuffer, strip_progress_directives
from .redactor import redact, strip_ansi

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str], Awaitable[None]]


class CursorAdapter:
    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        self.binary = self._resolve_binary(cfg.agent.binary)
        self.env_runner = self._resolve_optional_binary(cfg.agent.env_runner)
        self.bwrap = self._resolve_optional_binary(cfg.agent.bwrap_binary)

    def _resolve_binary(self, given: str) -> str:
        if not given:
            return shutil.which("cursor-agent") or "cursor-agent"
        if os.path.isabs(given):
            return given
        found = shutil.which(given) or shutil.which("cursor-agent")
        return found or given

    def _resolve_optional_binary(self, given: str) -> str:
        if not given:
            return ""
        if os.path.isabs(given):
            return given
        return shutil.which(given) or given

    def _build_cmd(
        self,
        prompt: str,
        workspace: str,
        mode: str,
        model: str | None,
        stream: bool = False,
    ) -> list[str]:
        cursor_cmd: list[str] = [self.binary, "-p", "--workspace", workspace]
        cursor_sandbox = "disabled" if self.cfg.agent.use_bwrap else "enabled"
        if model:
            cursor_cmd.extend(["--model", model])
        if stream:
            cursor_cmd.extend(["--output-format", "stream-json"])
        if mode == "ask":
            cursor_cmd.extend(["--mode", "ask", "--sandbox", cursor_sandbox])
        elif mode == "plan":
            cursor_cmd.extend(["--mode", "plan", "--sandbox", cursor_sandbox])
        elif mode == "task":
            cursor_cmd.extend(["--sandbox", cursor_sandbox])
            if self.cfg.agent.force_task_tools and self.cfg.agent.use_bwrap:
                cursor_cmd.append("--force")
        elif mode == "code":
            cursor_cmd.extend(["--trust", "--force", "--sandbox", cursor_sandbox])
        else:
            raise ValueError(f"unsupported cursor mode: {mode}")
        cursor_cmd.append(prompt)
        inner_cmd = cursor_cmd
        if self.env_runner and self.cfg.agent.env_name:
            inner_cmd = [self.env_runner, "run", "-n", self.cfg.agent.env_name, *cursor_cmd]
        if self.cfg.agent.use_bwrap:
            return self._build_bwrap_cmd(inner_cmd, workspace, mode)
        return inner_cmd

    def _build_bwrap_cmd(self, inner_cmd: list[str], workspace: str, mode: str) -> list[str]:
        home = Path.home()
        workspace_path = Path(workspace).expanduser().resolve(strict=False)
        sandbox_home = self._sandbox_home(workspace_path)
        cmd: list[str] = [self.bwrap]

        for src, dst in self._system_ro_binds():
            cmd.extend(["--ro-bind", src, dst])

        cmd.extend(
            [
                "--die-with-parent",
                "--symlink",
                "usr/bin",
                "/bin",
                "--symlink",
                "usr/sbin",
                "/sbin",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--share-net",
                "--unshare-user",
                "--unshare-ipc",
                "--dir",
                "/tmp",
                "--bind",
                str(sandbox_home),
                str(home),
            ]
        )
        workspace_flag = "--bind" if mode == "code" else "--ro-bind"
        cmd.extend([workspace_flag, str(workspace_path), str(workspace_path)])
        if mode == "task":
            for src, dst in self._task_rw_binds(workspace_path):
                cmd.extend(["--bind", src, dst])
        for src, dst in self._cursor_ro_binds(home):
            cmd.extend(["--ro-bind", src, dst])
        cmd.extend(
            [
                "--chdir",
                str(workspace_path),
                "--setenv",
                "HOME",
                str(home),
                "--setenv",
                "PATH",
                f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
                "--setenv",
                "MAMBA_ROOT_PREFIX",
                f"{home}/.local/share/mamba",
                *inner_cmd,
            ]
        )
        return cmd

    def _system_ro_binds(self) -> list[tuple[str, str]]:
        candidates = [
            "/usr",
            "/lib",
            "/lib64",
            "/etc/resolv.conf",
            "/etc/ssl",
            "/etc/alternatives",
        ]
        return [(path, path) for path in candidates if Path(path).exists()]

    def _cursor_ro_binds(self, home: Path) -> list[tuple[str, str]]:
        candidates = [
            home / ".local/bin",
            home / ".local/share/cursor-agent",
            home / ".local/share/mamba",
            home / ".mambarc",
            home / ".condarc",
        ]
        return [(str(path), str(path)) for path in candidates if path.exists()]

    def _sandbox_home(self, workspace: Path) -> Path:
        configured = Path(self.cfg.agent.sandbox_home).expanduser()
        if configured.is_absolute():
            return Path(os.path.abspath(configured))
        return Path(os.path.abspath(workspace / configured))

    def _prepare_bwrap(self, workspace: str, mode: str) -> str | None:
        if not self.cfg.agent.use_bwrap:
            if mode == "task" and self.cfg.agent.force_task_tools:
                return "[error] 助手沙箱未配置"
            return None
        workspace_path = Path(workspace).expanduser().resolve(strict=False)
        if not self._has_trusted_bwrap(workspace_path):
            return "[error] 助手沙箱未配置"
        sandbox_home = self._sandbox_home(workspace_path)
        try:
            self._ensure_private_sandbox_home(sandbox_home, workspace_path)
            if mode == "task":
                self._ensure_workspace_child_dir(self._task_outgoing_dir(workspace_path), workspace_path)
            self._seed_cursor_state(sandbox_home)
            self._seed_workspace_trust(sandbox_home, workspace_path)
        except OSError as exc:
            logger.warning("sandbox preparation failed: %s", type(exc).__name__)
            return "[error] 助手沙箱未配置"
        except ValueError:
            return "[error] 助手沙箱未配置"
        return None

    def _has_trusted_bwrap(self, workspace: Path) -> bool:
        if not self.bwrap:
            return False
        candidate = Path(self.bwrap)
        if not candidate.is_absolute():
            return False
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return False
        if resolved.name != "bwrap" or not os.access(resolved, os.X_OK):
            return False
        if (
            self._is_tmp_path(resolved)
            or self._is_relative_to(resolved, workspace)
            or self._is_relative_to(resolved, Path.home())
        ):
            return False
        st = resolved.stat()
        if st.st_mode & 0o022:
            return False
        self.bwrap = str(resolved)
        return True

    def _ensure_private_sandbox_home(self, sandbox_home: Path, workspace: Path) -> None:
        if not sandbox_home.is_absolute():
            raise ValueError("sandbox home must be absolute")
        if self._is_relative_to(sandbox_home, workspace) or not self._is_tmp_path(sandbox_home):
            raise ValueError("sandbox home must be a private tmp path outside workspace")
        self._ensure_private_dir_chain(sandbox_home)

    def _ensure_private_dir_chain(self, target: Path) -> None:
        target.relative_to(Path("/tmp"))
        current = Path("/tmp")
        for part in target.relative_to(Path("/tmp")).parts:
            current = current / part
            self._ensure_private_dir(current)

    def _ensure_private_child_dir(self, sandbox_home: Path, relative: Path) -> None:
        current = sandbox_home
        for part in relative.parts:
            if part in ("", "."):
                continue
            current = current / part
            self._ensure_private_dir(current)

    def _ensure_private_dir(self, path: Path) -> None:
        try:
            st = path.lstat()
        except FileNotFoundError:
            path.mkdir(mode=0o700)
            st = path.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ValueError("private path component is not a directory")
        if st.st_uid != os.getuid():
            raise ValueError("private path component is not owned by current user")
        if st.st_mode & 0o077:
            path.chmod(0o700)

    def _ensure_workspace_child_dir(self, target: Path, workspace: Path) -> None:
        try:
            target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("writable task path must stay inside workspace") from exc
        current = workspace
        for part in target.relative_to(workspace).parts:
            current = current / part
            try:
                st = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                st = current.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise ValueError("writable task path component is not a directory")

    def _is_tmp_path(self, path: Path) -> bool:
        try:
            path.relative_to(Path("/tmp"))
        except ValueError:
            return False
        return True

    def _seed_cursor_state(self, sandbox_home: Path) -> None:
        home = Path.home()
        for relative in (
            Path(".cursor/cli-config.json"),
            Path(".cursor/agent-cli-state.json"),
            Path(".config/cursor/auth.json"),
        ):
            source = home / relative
            if not source.exists():
                continue
            target = sandbox_home / relative
            self._ensure_private_child_dir(sandbox_home, relative.parent)
            self._write_private_file(target, source.read_bytes())

    def _seed_workspace_trust(self, sandbox_home: Path, workspace: Path) -> None:
        project_dir = sandbox_home / ".cursor/projects" / self._cursor_project_dir_name(workspace)
        project_relative = project_dir.relative_to(sandbox_home)
        self._ensure_private_child_dir(sandbox_home, project_relative)
        trusted_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        trust_file = project_dir / ".workspace-trusted"
        payload = (
            json.dumps(
                {"trustedAt": trusted_at, "workspacePath": str(workspace)},
                indent=2,
            )
            + "\n"
        )
        self._write_private_file(trust_file, payload.encode("utf-8"))

    def _cursor_project_dir_name(self, workspace: Path) -> str:
        return workspace.as_posix().strip("/").replace("/", "-") or "root"

    def _write_private_file(self, target: Path, payload: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(target, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        target.chmod(0o600)

    def _task_outgoing_dir(self, workspace: Path) -> Path:
        outgoing = (workspace / self.cfg.resources.root / "outgoing").resolve(strict=False)
        outgoing.relative_to(workspace)
        return outgoing

    def _task_rw_binds(self, workspace: Path) -> list[tuple[str, str]]:
        outgoing = self._task_outgoing_dir(workspace)
        return [(str(outgoing), str(outgoing))]

    def _is_relative_to(self, path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
        except ValueError:
            return False
        return True

    async def run(
        self,
        prompt: str,
        workspace: str | None = None,
        mode: str = "ask",
        model: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> str:
        ws = workspace or self.cfg.agent.default_workspace
        if not self.cfg.is_workspace_allowed(ws):
            return f"[denied] workspace {ws} not allowed"
        if self.cfg.agent.require_env:
            if (
                not self.env_runner
                or os.path.basename(self.env_runner) != "micromamba"
                or self.cfg.agent.env_name != "base"
            ):
                return "[error] 助手环境未配置"
        sandbox_error = self._prepare_bwrap(ws, mode)
        if sandbox_error:
            return sandbox_error

        env = {k: os.environ.get(k, "") for k in self.cfg.agent.env_allowlist if k in os.environ}
        env["PATH"] = os.environ.get("PATH", "")

        cmd = self._build_cmd(prompt, ws, mode, model, stream=progress is not None)

        logger.info("agent invoke: %s ...", " ".join(cmd[:4]))
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=ws,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            if progress:
                out, err = await asyncio.wait_for(
                    self._communicate_streaming(proc, progress),
                    timeout=self.cfg.agent.max_runtime_seconds,
                )
            else:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.cfg.agent.max_runtime_seconds
                )
                out = (stdout or b"").decode("utf-8", "replace")
                err = (stderr or b"").decode("utf-8", "replace")
            combined = (out + "\n" + err).strip()
            cleaned = strip_ansi(combined)
            cleaned, extra_progress = strip_progress_directives(cleaned)
            if progress:
                for item in extra_progress:
                    try:
                        await progress(item)
                    except Exception:  # noqa: BLE001 - progress should not fail the job
                        logger.exception("progress callback failed")
            if proc.returncode:
                logger.warning(
                    "agent process failed with exit code %s: %s",
                    proc.returncode,
                    redact(cleaned)[:1000],
                )
                return "[error] 助手执行失败"
            return cleaned[: self.cfg.agent.max_output_chars] or "[no output]"
        except asyncio.TimeoutError:
            await self._kill_process_group(proc)
            return "[error] 助手响应超时"
        except asyncio.CancelledError:
            await self._kill_process_group(proc)
            raise
        except FileNotFoundError:
            return "[error] 助手暂时不可用"
        except Exception as e:  # noqa: BLE001
            logger.exception("agent run error")
            return f"[error] 助手执行失败：{type(e).__name__}"

    async def _communicate_streaming(
        self,
        proc: asyncio.subprocess.Process,
        progress: ProgressCallback,
    ) -> tuple[str, str]:
        assert proc.stdout is not None
        stderr_task = asyncio.create_task(
            proc.stderr.read() if proc.stderr else asyncio.sleep(0, result=b"")
        )
        buffer = ProgressLineBuffer()
        output_parts: list[str] = []
        pending_assistant_message: str | None = None

        async def send_progress(text: str) -> None:
            try:
                await progress(text)
            except Exception:  # noqa: BLE001 - progress should not fail the job
                logger.exception("progress callback failed")

        async def flush_pending_assistant_message() -> None:
            nonlocal pending_assistant_message
            if not pending_assistant_message:
                return
            await send_progress(pending_assistant_message)
            pending_assistant_message = None

        async for line in self._stream_lines(proc.stdout):
            kind, text = self._stream_event_from_line(line)
            if kind == "progress":
                if text:
                    await send_progress(text)
                continue
            if kind == "message":
                clean_message, progress_lines = strip_progress_directives(text)
                for item in progress_lines:
                    await send_progress(item)
                if clean_message.strip():
                    await flush_pending_assistant_message()
                    pending_assistant_message = clean_message
                continue
            if text:
                await flush_pending_assistant_message()
            clean_lines, progress_lines = buffer.feed(text)
            output_parts.extend(clean_lines)
            for item in progress_lines:
                await send_progress(item)
        clean_lines, progress_lines = buffer.finish()
        output_parts.extend(clean_lines)
        for item in progress_lines:
            await send_progress(item)
        await proc.wait()
        stderr = await stderr_task
        delta_output = "\n".join(output_parts).strip()
        final_parts: list[str] = []
        if pending_assistant_message:
            if delta_output:
                await flush_pending_assistant_message()
                final_parts.append(delta_output)
            else:
                final_parts.append(pending_assistant_message)
        else:
            final_parts.append(delta_output)
        return "\n".join(part for part in final_parts if part).strip(), (stderr or b"").decode("utf-8", "replace")

    async def _stream_lines(self, stream: asyncio.StreamReader):
        pending = b""
        while True:
            chunk = await stream.read(32768)
            if not chunk:
                break
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                yield (line + b"\n").decode("utf-8", "replace")
        if pending:
            yield pending.decode("utf-8", "replace")

    def _stream_text_from_line(self, line: str) -> str:
        _kind, text = self._stream_event_from_line(line)
        return text

    def _stream_event_from_line(self, line: str) -> tuple[str, str]:
        stripped = line.strip()
        if not stripped:
            return "delta", line
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return "delta", line
        progress = self._stream_progress_from_payload(payload)
        if progress:
            return "progress", progress
        if not self._is_assistant_stream_payload(payload):
            return "", ""
        text = self._extract_stream_text(payload)
        return (self._stream_event_kind(payload), text) if text else ("", "")

    def _stream_progress_from_payload(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        if str(payload.get("type", "")).lower() != "tool_call":
            return ""
        subtype = str(payload.get("subtype", "")).lower()
        if subtype not in {"started", "completed"}:
            return ""
        description = self._extract_tool_description(payload)
        if not description:
            description = "调用工具"
        prefix = "正在执行" if subtype == "started" else "已完成"
        return f"{prefix}：{description}"

    def _extract_tool_description(self, payload: Any) -> str:
        candidates: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                description = value.get("description")
                if isinstance(description, str):
                    candidates.append(description)
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        tool_call = payload.get("tool_call") if isinstance(payload, dict) else None
        visit(tool_call)
        for candidate in candidates:
            cleaned = " ".join(candidate.split()).strip()
            if cleaned:
                return redact(cleaned)[:80]
        return ""

    def _stream_event_kind(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return "delta"
        markers = " ".join(
            str(payload.get(key, "")).lower() for key in ("type", "event", "role", "name")
        )
        if "assistant_message" in markers:
            return "message"
        if payload.get("role") == "assistant" and "message" in payload:
            return "message"
        return "delta"

    def _is_assistant_stream_payload(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        markers = " ".join(
            str(payload.get(key, "")).lower() for key in ("type", "event", "role", "name")
        )
        if any(item in markers for item in ("user", "system", "input", "prompt", "request")):
            return False
        if payload.get("role") == "assistant":
            return True
        return any(
            item in markers
            for item in (
                "assistant",
                "output",
                "response.output_text",
                "content_block_delta",
                "text_delta",
            )
        )

    def _extract_stream_text(self, payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        if isinstance(payload, list):
            return "".join(self._extract_stream_text(item) for item in payload)
        if not isinstance(payload, dict):
            return ""
        parts: list[str] = []
        for key in ("text", "delta", "content", "message", "output"):
            if key in payload:
                parts.append(self._extract_stream_text(payload[key]))
        return "".join(parts)

    async def _kill_process_group(self, proc: asyncio.subprocess.Process | None) -> None:
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass


class CustomCommandAdapter(CursorAdapter):
    """Run a user-configured command template for each QQ command mode."""

    def _build_cmd(
        self,
        prompt: str,
        workspace: str,
        mode: str,
        model: str | None,
        stream: bool = False,
    ) -> list[str]:
        template = self.cfg.agent.command.get(mode)
        if not template:
            raise ValueError(f"missing custom command template for {mode}")
        inner_cmd = [
            part.format(
                prompt=prompt,
                workspace=workspace,
                mode=mode,
                model=model or "",
                stream="true" if stream else "false",
            )
            for part in template
        ]
        if self.env_runner and self.cfg.agent.env_name:
            inner_cmd = [self.env_runner, "run", "-n", self.cfg.agent.env_name, *inner_cmd]
        if self.cfg.agent.use_bwrap:
            return self._build_bwrap_cmd(inner_cmd, workspace, mode)
        return inner_cmd

    def _stream_text_from_line(self, line: str) -> str:
        return line

    def _cursor_ro_binds(self, home: Path) -> list[tuple[str, str]]:
        return []

    def _seed_cursor_state(self, sandbox_home: Path) -> None:
        return

    def _seed_workspace_trust(self, sandbox_home: Path, workspace: Path) -> None:
        return
