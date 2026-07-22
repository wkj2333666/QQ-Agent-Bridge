"""CLI agent subprocess adapters."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import shutil
import stat
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .agent_trace import AgentTrace
from .progress_directives import ProgressLineBuffer, strip_progress_directives
from .redactor import redact, strip_ansi

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str], Awaitable[None]]
_HARDENED_HOME = Path("/home/curator")
_HARDENED_CURSOR_RUNTIME = Path("/opt/qq-agent-curator/cursor")
_MAX_CURSOR_AUTH_BYTES = 1024 * 1024
_HARDENED_CLI_CONFIG = {
    "permissions": {
        "allow": [],
        "deny": ["Shell(*)", "Write(*)"],
    }
}
_HARDENED_AGENT_STATE = {
    "autoReview": False,
    "plugins": {},
    "toolApprovals": {},
}
_HARDENED_MCP_CONFIG = {"mcpServers": {}}

_OUTGOING_DIRECTIVE_RE = re.compile(
    r"^\s*QQBOT_SEND_(?:IMAGE|FILE|VOICE|AUDIO)\s*:\s*.+?\s*$",
    re.IGNORECASE,
)


def _split_outgoing_directives(text: str) -> tuple[str, tuple[str, ...]]:
    kept: list[str] = []
    directives: list[str] = []
    for line in text.splitlines():
        if _OUTGOING_DIRECTIVE_RE.match(line):
            directives.append(line.strip())
        else:
            kept.append(line)
    return "\n".join(kept).strip(), tuple(directives)


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

    @staticmethod
    def _is_usage_limit_error(text: str) -> bool:
        normalized = " ".join(text.lower().split())
        return any(
            phrase in normalized
            for phrase in (
                "you've hit your usage limit",
                "you have hit your usage limit",
                "you're out of usage",
                "you are out of usage",
                "usage limit",
                "spend limit",
                "monthly cycle",
                "switch to auto",
            )
        )

    @staticmethod
    def _is_storage_exhaustion_error(text: str) -> bool:
        normalized = " ".join(text.lower().split())
        return "enospc" in normalized or "no space left on device" in normalized

    def _process_error_class(self, text: str) -> str:
        if self._is_usage_limit_error(text):
            return "usage_limit"
        if self._is_storage_exhaustion_error(text):
            return "storage_exhaustion"
        return "process_exit"

    def _build_cmd(
        self,
        prompt: str,
        workspace: str,
        mode: str,
        model: str | None,
        stream: bool = False,
    ) -> list[str]:
        if self.cfg.agent.hardened_read_only and mode != "ask":
            raise ValueError("hardened read-only agent only supports ask mode")
        cursor_cmd: list[str] = [self.binary, "-p", "--workspace", workspace]
        # bwrap already provides a full container sandbox, so disable
        # cursor-agent's own sandbox (which requires AppArmor).
        # Without bwrap, let cursor-agent manage its own sandbox.
        cursor_sandbox = "disabled" if self.cfg.agent.use_bwrap else "enabled"
        if model:
            cursor_cmd.extend(["--model", model])
        if stream:
            cursor_cmd.extend(["--output-format", "stream-json"])
        # Hardened read-only mode has trust baked into the hardened runtime config.
        if not self.cfg.agent.hardened_read_only:
            cursor_cmd.append("--trust")
        if mode == "ask":
            cursor_cmd.extend(["--mode", "ask", "--sandbox", cursor_sandbox])
        elif mode == "plan":
            cursor_cmd.extend(["--mode", "plan", "--sandbox", cursor_sandbox])
        elif mode == "task":
            cursor_cmd.extend(["--sandbox", cursor_sandbox])
            if self.cfg.agent.force_task_tools and self.cfg.agent.use_bwrap:
                cursor_cmd.append("--force")
        elif mode == "code":
            cursor_cmd.extend(["--force", "--sandbox", cursor_sandbox])
        else:
            raise ValueError(f"unsupported cursor mode: {mode}")
        cursor_cmd.append(prompt)
        inner_cmd = cursor_cmd
        if (
            not self.cfg.agent.hardened_read_only
            and self.env_runner
            and self.cfg.agent.env_name
        ):
            inner_cmd = [self.env_runner, "run", "-n", self.cfg.agent.env_name, *cursor_cmd]
        if self.cfg.agent.use_bwrap:
            return self._build_bwrap_cmd(inner_cmd, workspace, mode)
        return inner_cmd

    def _build_bwrap_cmd(self, inner_cmd: list[str], workspace: str, mode: str) -> list[str]:
        home = Path.home()
        exposed_home = _HARDENED_HOME if self.cfg.agent.hardened_read_only else home
        workspace_path = Path(workspace).expanduser().resolve(strict=False)
        exposed_workspace = (
            Path("/workspace") if self.cfg.agent.hardened_read_only else workspace_path
        )
        hardened_runtime: tuple[Path, Path] | None = None
        if self.cfg.agent.hardened_read_only:
            runtime_root, runtime_binary = self._hardened_cursor_runtime(workspace_path)
            hardened_binary = _HARDENED_CURSOR_RUNTIME / runtime_binary.relative_to(
                runtime_root
            )
            hardened_runtime = (runtime_root, hardened_binary)
            inner_cmd = self._rewrite_hardened_paths(
                inner_cmd,
                workspace_path,
                exposed_workspace,
                hardened_binary,
            )
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
                "--share-net" if self.cfg.agent.share_network else "--unshare-net",
                "--unshare-user",
                "--unshare-ipc",
                "--dir",
                "/tmp",
                "--bind",
                str(sandbox_home),
                str(exposed_home),
            ]
        )
        if hardened_runtime is not None:
            runtime_root, _runtime_binary = hardened_runtime
            cmd.extend(
                [
                    "--dir",
                    "/opt",
                    "--dir",
                    "/opt/qq-agent-curator",
                    "--ro-bind",
                    str(runtime_root),
                    str(_HARDENED_CURSOR_RUNTIME),
                ]
            )
        workspace_flag = "--bind" if mode == "code" else "--ro-bind"
        cmd.extend([workspace_flag, str(workspace_path), str(exposed_workspace)])
        if mode == "task":
            for src, dst in self._task_rw_binds(workspace_path):
                cmd.extend(["--bind", src, dst])
        if not self.cfg.agent.hardened_read_only:
            for src, dst in self._cursor_ro_binds(home, exposed_home):
                cmd.extend(["--ro-bind", src, dst])
        path = (
            "/usr/local/bin:/usr/bin:/bin"
            if self.cfg.agent.hardened_read_only
            else f"{exposed_home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
        )
        cmd.extend(
            [
                "--chdir",
                str(exposed_workspace),
                "--setenv",
                "HOME",
                str(exposed_home),
                "--setenv",
                "PATH",
                path,
            ]
        )
        if not self.cfg.agent.hardened_read_only:
            cmd.extend(
                [
                    "--setenv",
                    "MAMBA_ROOT_PREFIX",
                    f"{exposed_home}/.local/share/mamba",
                ]
            )
        cmd.extend(inner_cmd)
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

    def _cursor_ro_binds(self, home: Path, exposed_home: Path) -> list[tuple[str, str]]:
        candidates = [
            home / ".local/bin",
            home / ".local/share/cursor-agent",
            home / ".local/share/mamba",
            home / ".mambarc",
            home / ".condarc",
        ]
        return [
            (str(path), str(exposed_home / path.relative_to(home)))
            for path in candidates
            if path.exists()
        ]

    def _rewrite_hardened_paths(
        self,
        inner_cmd: list[str],
        workspace: Path,
        exposed_workspace: Path,
        hardened_binary: Path,
    ) -> list[str]:
        rewritten = [
            str(hardened_binary) if part == self.binary else part for part in inner_cmd
        ]
        try:
            workspace_index = rewritten.index("--workspace") + 1
        except ValueError:
            return rewritten
        if workspace_index < len(rewritten) and rewritten[workspace_index] == str(workspace):
            rewritten[workspace_index] = str(exposed_workspace)
        return rewritten

    def _hardened_cursor_runtime(self, workspace: Path) -> tuple[Path, Path]:
        locator = Path(self.binary).expanduser()
        if not locator.is_absolute():
            raise ValueError("hardened cursor runtime is not trusted")
        self._validate_runtime_path(locator.parent, ownership=False)
        binary = locator.resolve(strict=True)
        runtime_root = binary.parent
        required = (binary, runtime_root / "node", runtime_root / "index.js")
        if self._is_tmp_path(runtime_root) or self._is_relative_to(runtime_root, workspace):
            raise ValueError("hardened cursor runtime is not trusted")
        self._validate_runtime_path(runtime_root, ownership=True)
        for artifact in required:
            artifact_stat = self._runtime_lstat(artifact)
            if (
                stat.S_ISLNK(artifact_stat.st_mode)
                or not stat.S_ISREG(artifact_stat.st_mode)
                or artifact_stat.st_uid not in {0, os.getuid()}
                or artifact_stat.st_mode & 0o022
            ):
                raise ValueError("hardened cursor runtime is not trusted")
        if not os.access(binary, os.X_OK) or not os.access(runtime_root / "node", os.X_OK):
            raise ValueError("hardened cursor runtime is unavailable")
        return runtime_root, binary

    def _validate_runtime_path(self, path: Path, *, ownership: bool) -> None:
        if not path.is_absolute():
            raise ValueError("hardened cursor runtime is not trusted")
        uid_mapped_prefixes = self._uid_mapped_system_prefixes(path) if ownership else set()
        current = Path(path.anchor)
        components = (current,)
        for part in path.parts[1:]:
            current /= part
            components += (current,)
        for component in components:
            metadata = self._runtime_lstat(component)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("hardened cursor runtime is not trusted")
            if ownership and metadata.st_mode & 0o022:
                raise ValueError("hardened cursor runtime is not trusted")
            if (
                ownership
                and metadata.st_uid not in {0, os.getuid()}
                and (
                    component not in uid_mapped_prefixes
                    or not self._runtime_is_read_only_mount(component)
                )
            ):
                raise ValueError("hardened cursor runtime is not trusted")

    def _uid_mapped_system_prefixes(self, runtime_root: Path) -> set[Path]:
        try:
            import pwd

            home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
        except (ImportError, KeyError, OSError):
            return set()
        if not self._is_relative_to(runtime_root, home):
            return set()
        current = Path(home.anchor)
        prefixes = {current}
        for part in home.parts[1:]:
            current /= part
            if current == home:
                break
            prefixes.add(current)
        return prefixes

    def _runtime_is_read_only_mount(self, path: Path) -> bool:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)

    def _runtime_lstat(self, path: Path) -> os.stat_result:
        return path.lstat()

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
            logger.warning(
                "bwrap not trusted: bwrap=%s workspace=%s",
                self.bwrap,
                workspace_path,
            )
            return "[error] 助手沙箱未配置"
        sandbox_home = self._sandbox_home(workspace_path)
        try:
            self._ensure_private_sandbox_home(sandbox_home, workspace_path)
            if mode == "task":
                self._ensure_workspace_child_dir(self._task_outgoing_dir(workspace_path), workspace_path)
            if self.cfg.agent.hardened_read_only:
                self._hardened_cursor_runtime(workspace_path)
                self._prepare_hardened_cursor_state(sandbox_home)
                # The hardened workspace is remapped to /workspace inside
                # the sandbox.  Seed trust for that path so cursor-agent
                # runs without prompting.
                self._seed_workspace_trust(sandbox_home, Path("/workspace"))
            else:
                self._seed_cursor_state(sandbox_home)
                self._seed_workspace_trust(sandbox_home, workspace_path)
        except OSError:
            logger.warning("sandbox preparation failed with OSError")
            return "[error] 助手沙箱未配置"
        except ValueError:
            logger.warning("sandbox preparation failed with ValueError")
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
        if self._is_relative_to(sandbox_home, workspace):
            raise ValueError("sandbox home must stay outside workspace")
        if self._is_tmp_path(sandbox_home):
            if sandbox_home == Path("/tmp"):
                raise ValueError("sandbox home must not expose the shared tmp root")
            self._ensure_private_dir_chain(sandbox_home)
            return
        home = Path.home().resolve(strict=False)
        persistent_root = home / ".local/state/qq-agent-bridge"
        if sandbox_home == persistent_root or not self._is_relative_to(
            sandbox_home, persistent_root
        ):
            raise ValueError("sandbox home must use the dedicated application state root")
        self._ensure_private_user_dir_chain(sandbox_home, home)

    def _ensure_private_dir_chain(self, target: Path) -> None:
        target.relative_to(Path("/tmp"))
        current = Path("/tmp")
        for part in target.relative_to(Path("/tmp")).parts:
            current = current / part
            self._ensure_private_dir(current)

    def _ensure_private_user_dir_chain(self, target: Path, home: Path) -> None:
        target.relative_to(home)
        self._validate_private_parent(home)
        current = home
        for part in target.relative_to(home).parts:
            current = current / part
            try:
                st = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                st = current.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise ValueError("private path component is not a directory")
            if st.st_uid != os.getuid() or st.st_mode & 0o022:
                raise ValueError("private path component is not safely owned")
        target.chmod(0o700)

    def _validate_private_parent(self, path: Path) -> None:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ValueError("private path parent is not a directory")
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise ValueError("private path parent is not safely owned")

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

    def _prepare_hardened_cursor_state(self, sandbox_home: Path) -> None:
        auth_payload = self._read_cursor_auth()
        self._reset_private_home(sandbox_home)
        files = (
            (Path(".cursor/cli-config.json"), self._json_bytes(_HARDENED_CLI_CONFIG)),
            (Path(".cursor/agent-cli-state.json"), self._json_bytes(_HARDENED_AGENT_STATE)),
            (Path(".cursor/mcp.json"), self._json_bytes(_HARDENED_MCP_CONFIG)),
            (Path(".config/cursor/auth.json"), auth_payload),
        )
        for relative, payload in files:
            self._ensure_private_child_dir(sandbox_home, relative.parent)
            self._write_private_file(sandbox_home / relative, payload)

    def _read_cursor_auth(self) -> bytes:
        home = Path.home()
        relative = Path(".config/cursor/auth.json")
        current = home
        for part in relative.parts[:-1]:
            current = current / part
            st = current.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise ValueError("cursor authentication path is unsafe")
            if st.st_uid != os.getuid():
                raise ValueError("cursor authentication path is not owned")
        source = home / relative
        source_stat = source.lstat()
        if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("cursor authentication artifact is unsafe")
        if source_stat.st_uid != os.getuid() or source_stat.st_size > _MAX_CURSOR_AUTH_BYTES:
            raise ValueError("cursor authentication artifact is unavailable")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(source, flags)
        try:
            opened_stat = os.fstat(fd)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_uid != os.getuid()
                or opened_stat.st_ino != source_stat.st_ino
                or opened_stat.st_dev != source_stat.st_dev
            ):
                raise ValueError("cursor authentication artifact changed")
            chunks: list[bytes] = []
            remaining = _MAX_CURSOR_AUTH_BYTES + 1
            while remaining > 0:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(fd)
        if not payload or len(payload) > _MAX_CURSOR_AUTH_BYTES:
            raise ValueError("cursor authentication artifact is unavailable")
        return payload

    def _reset_private_home(self, sandbox_home: Path) -> None:
        for child in sandbox_home.iterdir():
            metadata = child.lstat()
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                shutil.rmtree(child)
            else:
                child.unlink()

    def _json_bytes(self, payload: dict[str, Any]) -> bytes:
        return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")

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
        trace_id: str | None = None,
        redact_extra: tuple[str, ...] | None = None,
    ) -> str:
        ws = workspace or self.cfg.agent.default_workspace
        if not self.cfg.is_workspace_allowed(ws):
            return f"[denied] workspace {ws} not allowed"
        if self.cfg.agent.require_env:
            if not self.env_runner:
                return "[error] 助手环境未配置 (no env_runner)"
            if os.path.basename(self.env_runner) != "micromamba":
                return f"[error] 助手环境未配置 (env_runner={self.env_runner})"
            if self.cfg.agent.env_name != "base":
                return f"[error] 助手环境未配置 (env_name={self.cfg.agent.env_name})"
        sandbox_error = self._prepare_bwrap(ws, mode)
        if sandbox_error:
            logger.warning("cursor adapter sandbox error: %s", sandbox_error)
            return sandbox_error

        env = {k: os.environ.get(k, "") for k in self.cfg.agent.env_allowlist if k in os.environ}
        env["PATH"] = os.environ.get("PATH", "")

        trace = AgentTrace(self.cfg, trace_id, mode, model, ws, redact_extra=redact_extra)
        if trace.path is not None:
            logger.info("agent trace: %s", trace.path)
        proc: asyncio.subprocess.Process | None = None
        try:
            cmd = self._build_cmd(prompt, ws, mode, model, stream=progress is not None)

            logger.info("agent invoke: executable=%s mode=%s", cmd[0], mode)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=ws,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            trace.record("lifecycle", "spawn")
            if progress:
                out, err = await asyncio.wait_for(
                    self._communicate_streaming(proc, progress, trace=trace),
                    timeout=self.cfg.agent.max_runtime_seconds,
                )
            else:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.cfg.agent.max_runtime_seconds
                )
                out = (stdout or b"").decode("utf-8", "replace")
                err = (stderr or b"").decode("utf-8", "replace")
                trace.record_stdout_text(out)
                trace.record_stderr_text(err)
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
                error_class = self._process_error_class(cleaned)
                if self.cfg.agent.log_subprocess_output:
                    logger.warning(
                        "agent process failed with exit code %s: %s",
                        proc.returncode,
                        redact(cleaned, extra=redact_extra)[:1000],
                    )
                else:
                    logger.warning(
                        "agent process failed: error_class=%s exit_code=%s",
                        error_class,
                        proc.returncode,
                    )
                if (
                    model
                    and str(model).strip().lower() != "auto"
                    and self._is_usage_limit_error(cleaned)
                ):
                    if self.cfg.agent.log_subprocess_output:
                        logger.warning(
                            "model %s hit its usage limit; retrying once with Auto",
                            model,
                        )
                    else:
                        logger.warning("agent retry: error_class=usage_limit")
                    if progress:
                        try:
                            await progress("指定模型额度已用尽，正在切换 Auto 重试。")
                        except Exception:  # noqa: BLE001 - fallback must not fail the job
                            logger.exception("usage-limit fallback progress failed")
                    fallback_trace_id = f"{trace_id}-auto-fallback" if trace_id else None
                    return await self.run(
                        prompt,
                        ws,
                        mode,
                        model="auto",
                        progress=progress,
                        trace_id=fallback_trace_id,
                        redact_extra=redact_extra,
                    )
                if self._is_storage_exhaustion_error(cleaned):
                    return "[error] 助手存储空间不足，请联系管理员清理运行缓存"
                return "[error] 助手执行失败"
            return cleaned[: self.cfg.agent.max_output_chars] or "[no output]"
        except asyncio.TimeoutError:
            trace.record("lifecycle", "timeout")
            await self._kill_process_group(proc)
            return "[error] 助手响应超时"
        except asyncio.CancelledError:
            trace.record("lifecycle", "cancelled")
            await self._kill_process_group(proc)
            raise
        except FileNotFoundError:
            trace.record("lifecycle", "error", summary="process-not-found")
            return "[error] 助手暂时不可用"
        except Exception as e:  # noqa: BLE001
            trace.record("lifecycle", "error", summary=type(e).__name__)
            if self.cfg.agent.log_subprocess_output:
                logger.exception("agent run error")
            else:
                logger.warning("agent run error: error_class=%s", type(e).__name__)
            return f"[error] 助手执行失败：{type(e).__name__}"
        finally:
            trace.record(
                "lifecycle",
                "exit",
                returncode=proc.returncode if proc is not None else None,
            )
            trace.close()

    async def _communicate_streaming(
        self,
        proc: asyncio.subprocess.Process,
        progress: ProgressCallback,
        trace: AgentTrace | None = None,
    ) -> tuple[str, str]:
        assert proc.stdout is not None
        stderr_task = asyncio.create_task(
            proc.stderr.read() if proc.stderr else asyncio.sleep(0, result=b"")
        )
        buffer = ProgressLineBuffer()
        output_parts: list[str] = []
        outgoing_directives: list[str] = []
        seen_outgoing_directives: set[str] = set()
        pending_assistant_message: str | None = None

        def retain_outgoing_directives(text: str) -> str:
            clean, directives = _split_outgoing_directives(text)
            for directive in directives:
                if directive not in seen_outgoing_directives:
                    outgoing_directives.append(directive)
                    seen_outgoing_directives.add(directive)
            return clean

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
            if trace:
                trace.record_stdout_line(line)
            kind, text = self._stream_event_from_line(line)
            if kind == "progress":
                await flush_pending_assistant_message()
                if text:
                    await send_progress(text)
                continue
            if kind == "message":
                clean_message, progress_lines = strip_progress_directives(text)
                clean_message = retain_outgoing_directives(clean_message)
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
        if trace:
            trace.record_stderr_text((stderr or b"").decode("utf-8", "replace"))
        delta_output = retain_outgoing_directives("\n".join(output_parts).strip())
        final_parts: list[str] = []
        if pending_assistant_message:
            if delta_output:
                await flush_pending_assistant_message()
                final_parts.append(delta_output)
            else:
                final_parts.append(pending_assistant_message)
        else:
            final_parts.append(delta_output)
        final_parts.extend(outgoing_directives)
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
        return self._user_visible_tool_progress(description, completed=subtype == "completed")

    def _user_visible_tool_progress(self, description: str, *, completed: bool) -> str:
        text = description.lower()
        # Inspection verbs — these are about checking what exists, not
        # acting on media, so they must not trigger voice/video/image labels.
        _INSPECT = frozenset({
            "probe", "check", "list", "find", "inspect", "locate",
            "verify", "explore", "count", "view", "look",
        })
        is_inspect = any(w in text for w in _INSPECT)
        # Transcribe / subtitle — keep first, most specific
        if any(item in text for item in ("transcrib", "subtitle", "caption", "字幕", "转写")):
            return "语音转写完成。" if completed else "正在转写语音…"
        # Voice / audio — check before video; skip for pure inspection
        if not is_inspect and any(
            item in text
            for item in ("tts", "voice", "speech", "audio", "语音", "人声", "音频")
        ):
            return "语音处理完成。" if completed else "正在处理语音…"
        # Video — bilibili / video keywords only, no "download" here
        if not is_inspect and any(
            item in text for item in ("bilibili", "b站", "video", "视频")
        ):
            return "视频内容获取完成。" if completed else "正在获取视频内容…"
        # Read / reference — before image so "查看图片规范" means reading, not image work
        if any(item in text for item in ("read", "reference", "skill", "规范", "说明", "查看", "读取")):
            return "相关说明已看完。" if completed else "正在查看相关说明…"
        # Image — skip for pure inspection
        if not is_inspect and any(
            item in text
            for item in ("image", "picture", "draw", "paint", "图片", "图像", "绘图", "画")
        ):
            return "图片处理完成。" if completed else "正在处理图片…"
        # Download — generic, after voice/video so specific matches win.
        # Skip pure inspection (e.g. "check download status" is not downloading).
        if not is_inspect and any(item in text for item in ("download", "下载")):
            return "下载完成。" if completed else "正在下载…"
        # Search — applies to search, fetch, web
        if any(item in text for item in ("search", "web", "browse", "fetch", "搜索", "联网", "网页", "资料")):
            return "资料查找完成。" if completed else "正在查找资料…"
        # File — only for actual file operations, not "find files" / "check file"
        if not is_inspect and any(
            item in text
            for item in ("file", "write", "save", "create", "文件", "保存", "创建", "写入")
        ):
            return "文件处理完成。" if completed else "正在处理文件…"
        return ""

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
        event_type = str(payload.get("type", "")).lower()
        if event_type == "assistant":
            return "message"
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

    def _cursor_ro_binds(self, home: Path, exposed_home: Path) -> list[tuple[str, str]]:
        return []

    def _seed_cursor_state(self, sandbox_home: Path) -> None:
        return

    def _seed_workspace_trust(self, sandbox_home: Path, workspace: Path) -> None:
        return
