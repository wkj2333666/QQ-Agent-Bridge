"""Global test safeguards for local deployment state."""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import qq_agent_bridge.config_block_store as config_block_store  # noqa: E402


LOCAL_CONFIG = (ROOT / "config.yaml").resolve(strict=False)


@pytest.fixture(autouse=True)
def reject_repository_local_config_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must persist config changes only under their own tmp_path."""
    original = config_block_store._write_text_atomic

    def guarded_write(path: Path, text: str) -> Any:
        if Path(path).resolve(strict=False) == LOCAL_CONFIG:
            raise AssertionError("test attempted to modify repository-local config.yaml")
        return original(path, text)

    monkeypatch.setattr(config_block_store, "_write_text_atomic", guarded_write)
