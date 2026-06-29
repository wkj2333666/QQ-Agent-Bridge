"""Dry run: simulate events without network or real cursor.

Usage:
  . .venv/bin/activate
  python dry_run.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.policy import Policy  # type: ignore
from qq_agent_bridge.redactor import redact  # type: ignore
from qq_agent_bridge.types import ChatEvent  # type: ignore


async def fake_cursor(cmd: str, args: str, ev: ChatEvent) -> str:
    return f"[fake-cursor {cmd}] workspace ok, prompt was: {args[:80]!r}"


async def main() -> None:
    cfg = BridgeConfig(
        owners=["1000000001"],
        allowed_users=["1000000001"],
        allowed_groups=["999999"],
        workspaces={"/opt/workspaces": True},
        commands={"ask": True, "plan": True, "code": True, "status": True},
        dangerous_requires_confirm=False,
    )
    cfg.agent.max_concurrent_jobs = 2
    pol = Policy(cfg, fake_cursor)

    async def handle(ev: ChatEvent) -> None:
        default_command = "ask" if (not ev.is_group or ev.mentioned_bot) else None
        p = pol.parse(ev.text, default_command=default_command)
        if not p:
            return
        ok, r = pol.allow(ev, p.name)
        print(f"allow {ev.text!r} -> {ok} {r}")
        if not ok:
            return
        jid, nonce = pol.start_job(ev, p)
        print(f"started {jid} nonce={nonce}")
        job = pol.jobs[jid]
        if job.task:
            res = await job.task
            print("result:", redact(res)[:200])

    # private ask
    await handle(
        ChatEvent("m1", "qq", "1000000001", "1000000001", False, True, "/ask what is 1+1", 1)
    )
    # private bare text defaults to ask
    await handle(
        ChatEvent("m1b", "qq", "1000000001", "1000000001", False, True, "what is 2+2", 1)
    )
    # group ask
    await handle(
        ChatEvent("m2", "qq", "999999", "1000000001", True, True, "@123456 /ask explain cursor cli", 2)
    )
    # group bare mention defaults to ask
    await handle(
        ChatEvent("m3", "qq", "999999", "1000000001", True, True, "@123456 explain plan mode", 3)
    )
    print("status:", pol.get_status())
    print("dry run OK")


if __name__ == "__main__":
    asyncio.run(main())
