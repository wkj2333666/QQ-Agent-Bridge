"""Optional local CLI availability smoke tests."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess

import pytest


_CLI_SMOKE_ENV = "QQ_AGENT_BRIDGE_CLI_SMOKE"


def _smoke_command(name: str, default: list[str]) -> list[str]:
    override = os.environ.get(f"QQ_AGENT_BRIDGE_SMOKE_{name.upper()}_CMD", "").strip()
    return shlex.split(override) if override else default


@pytest.mark.parametrize(
    ("name", "default_cmd"),
    [
        ("cursor", ["cursor-agent", "--version"]),
        ("codex", ["codex", "--version"]),
        ("claude", ["claude", "--version"]),
    ],
)
def test_cli_binary_can_start_when_smoke_enabled(name: str, default_cmd: list[str]) -> None:
    if os.environ.get(_CLI_SMOKE_ENV) != "1":
        pytest.skip(f"set {_CLI_SMOKE_ENV}=1 to run local CLI smoke tests")

    cmd = _smoke_command(name, default_cmd)
    if not cmd or not shutil.which(cmd[0]):
        pytest.skip(f"{name} CLI not found: {cmd[0] if cmd else '<empty>'}")

    result = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, (result.stdout + "\n" + result.stderr).strip()
