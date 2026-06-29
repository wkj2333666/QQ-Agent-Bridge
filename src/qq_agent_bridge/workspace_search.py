"""Bounded read-only search inside the configured workspace."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from .config import BridgeConfig
from .redactor import redact, strip_ansi


class WorkspaceSearch:
    """Search configured workspace with fixed rg arguments."""

    EXCLUDE_GLOBS: tuple[str, ...] = (
        "!.git/**",
        "!**/.git/**",
        "!.venv/**",
        "!**/.venv/**",
        "!__pycache__/**",
        "!**/__pycache__/**",
        "!node_modules/**",
        "!**/node_modules/**",
        "!runtime/napcat/data/**",
        "!downloads/qq-agent-bridge/**",
        "!config.yaml",
        "!.env",
        "!.env.*",
        "!*.pem",
        "!*.key",
        "!*.sqlite",
        "!*.db",
        "!**/*session*",
        "!**/*keystore*",
        "!**/*qr*",
    )

    SENSITIVE_NAMES: set[str] = {
        ".env",
        "config.yaml",
    }

    SENSITIVE_SUFFIXES: tuple[str, ...] = (
        ".pem",
        ".key",
        ".sqlite",
        ".db",
    )

    SENSITIVE_PARTS: tuple[str, ...] = (
        ".git",
        ".venv",
        "__pycache__",
        "node_modules",
        "session",
        "keystore",
        "qr",
    )

    def __init__(
        self,
        cfg: BridgeConfig,
        *,
        executable: str = "rg",
        timeout_seconds: float = 8.0,
        max_matches: int = 20,
        max_line_chars: int = 220,
    ) -> None:
        self.cfg = cfg
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.max_matches = max_matches
        self.max_line_chars = max_line_chars

    async def search(self, query: str) -> str:
        """Search for a literal query and return redacted snippets."""
        q = query.strip()
        if not q:
            return "用法：/search <关键词>"

        workspace = Path(self.cfg.agent.default_workspace).expanduser().resolve(strict=False)
        if not self.cfg.is_workspace_allowed(str(workspace)):
            return "[denied] search workspace not allowed"
        if not shutil.which(self.executable):
            return "[error] search tool unavailable"

        argv = self._build_rg_args(q)
        env = {"PATH": os.environ.get("PATH", "")}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workspace),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            try:
                proc.kill()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            return "[timeout] 搜索超时"
        except FileNotFoundError:
            return "[error] search tool unavailable"

        out = strip_ansi((stdout or b"").decode("utf-8", "replace"))
        err = strip_ansi((stderr or b"").decode("utf-8", "replace"))
        if proc.returncode not in (0, 1):
            detail = redact(err).strip()
            return f"[error] 搜索失败{': ' + detail[:120] if detail else ''}"
        return self._format_matches(q, out)

    def _build_rg_args(self, query: str) -> list[str]:
        args = [
            self.executable,
            "--fixed-strings",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--max-columns",
            str(self.max_line_chars),
            "--max-columns-preview",
        ]
        for glob in self.EXCLUDE_GLOBS:
            args.extend(["--glob", glob])
        args.extend(["--", query, "."])
        return args

    def _format_matches(self, query: str, output: str) -> str:
        lines: list[str] = []
        for raw in output.splitlines():
            if not raw.strip():
                continue
            normalized = self._normalize_match_line(raw)
            if self._is_sensitive_match_line(normalized):
                continue
            safe = redact(normalized, extra=[self.cfg.onebot.access_token])
            if len(safe) > self.max_line_chars:
                safe = safe[: self.max_line_chars - 1] + "…"
            lines.append(safe)
            if len(lines) >= self.max_matches:
                break
        if not lines:
            return f"没在当前项目里搜到：{query}"
        body = "\n".join(lines)
        limit = min(self.cfg.effective_max_chars(), 3000)
        return body[:limit]

    def _normalize_match_line(self, line: str) -> str:
        path, sep, rest = line.partition(":")
        if not sep:
            return line
        if path.startswith("./"):
            path = path[2:]
        line_no, sep2, snippet = rest.partition(":")
        if sep2:
            return f"{path}:{line_no}: {snippet.lstrip()}"
        return f"{path}: {rest}"

    def _is_sensitive_match_line(self, line: str) -> bool:
        path = line.split(":", 1)[0].replace("\\", "/")
        parts = [part for part in path.split("/") if part]
        if not parts:
            return False
        lowered = [part.lower() for part in parts]
        if lowered[-1] in self.SENSITIVE_NAMES or lowered[-1].startswith(".env"):
            return True
        if lowered[-1].endswith(self.SENSITIVE_SUFFIXES):
            return True
        return any(sensitive in part for part in lowered for sensitive in self.SENSITIVE_PARTS)
