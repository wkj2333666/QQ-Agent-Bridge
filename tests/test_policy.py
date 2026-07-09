"""Policy and parser tests."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_agent_bridge.config import BridgeConfig  # type: ignore
from qq_agent_bridge.policy import Job, Policy  # type: ignore
from qq_agent_bridge.types import ChatEvent  # type: ignore


async def fake_runner(job: Job) -> str:
    return f"ran {job.cmd} {job.args}"


def make_ev(
    text: str,
    sender: str = "1000000001",
    group: str | None = None,
    mentioned: bool = True,
    mid: str = "m1",
) -> ChatEvent:
    return ChatEvent(
        id=mid,
        platform="qq",
        chat_id=group or sender,
        sender_id=sender,
        is_group=bool(group),
        mentioned_bot=mentioned,
        text=text,
        timestamp=1,
    )


def test_parse_and_allow() -> None:
    cfg = BridgeConfig(
        owners=["1000000001"],
        allowed_users=["1000000001"],
        allowed_groups=["123"],
        workspaces={"/opt/workspaces": True},
        commands={"ask": True, "code": False},
    )
    pol = Policy(cfg, fake_runner)
    ev = make_ev("/ask hello")
    p = pol.parse(ev.text)
    assert p is not None and p.name == "ask"
    ok, _ = pol.allow(ev, "ask")
    assert ok

    evg = make_ev("@bot /ask hi", group="123", mid="m2")
    pg = pol.parse(evg.text)
    assert pg is not None
    okg, _ = pol.allow(evg, "ask")
    assert okg


def test_parse_group_mention_command() -> None:
    cfg = BridgeConfig()
    pol = Policy(cfg, fake_runner)
    parsed = pol.parse("@1000000001 /ask hello")
    assert parsed is not None
    assert parsed.name == "ask"
    assert parsed.args == "hello"


def test_parse_bare_mention_defaults_to_ask() -> None:
    cfg = BridgeConfig()
    pol = Policy(cfg, fake_runner)
    parsed = pol.parse("@1000000001 你好", default_command="ask")
    assert parsed is not None
    assert parsed.name == "ask"
    assert parsed.args == "你好"


def test_parse_private_plain_text_defaults_to_ask() -> None:
    cfg = BridgeConfig()
    pol = Policy(cfg, fake_runner)
    parsed = pol.parse("你好", default_command="ask")
    assert parsed is not None
    assert parsed.name == "ask"
    assert parsed.args == "你好"


def test_parse_reset_command() -> None:
    cfg = BridgeConfig()
    pol = Policy(cfg, fake_runner)
    parsed = pol.parse("@1000000001 /reset")
    assert parsed is not None
    assert parsed.name == "reset"
    assert parsed.args == ""


def test_parse_reload_command() -> None:
    cfg = BridgeConfig()
    pol = Policy(cfg, fake_runner)
    parsed = pol.parse("@1000000001 /reload")
    assert parsed is not None
    assert parsed.name == "reload"
    assert parsed.args == ""


def test_parse_profile_command() -> None:
    cfg = BridgeConfig()
    pol = Policy(cfg, fake_runner)
    parsed = pol.parse("@1000000001 /profile set 你是项目管家")
    assert parsed is not None
    assert parsed.name == "profile"
    assert parsed.args == "set 你是项目管家"


def test_non_owner_can_only_use_read_only_commands() -> None:
    cfg = BridgeConfig(
        owners=["owner"],
        allowed_users=["reader"],
        allowed_groups=["123"],
        commands={
            "ask": True,
            "plan": True,
            "search": True,
            "task": True,
            "status": True,
            "help": True,
            "reset": True,
            "code": True,
            "shell": True,
            "approve": True,
            "stop": True,
            "reload": True,
            "profile": True,
        },
        workspaces={"/tmp": True},
    )
    pol = Policy(cfg, fake_runner)

    for idx, cmd in enumerate(("ask", "plan", "search", "task", "status", "help", "profile")):
        ok, reason = pol.allow(make_ev(f"/{cmd} x", sender="reader", mid=f"ro-{idx}"), cmd)
        assert ok, reason

    for idx, cmd in enumerate(("code", "shell", "approve", "stop", "reset", "reload")):
        ok, reason = pol.allow(make_ev(f"/{cmd} x", sender="reader", mid=f"rw-{idx}"), cmd)
        assert not ok
        assert reason == "owner-only"


def test_allowed_group_member_can_use_read_only_without_user_allowlist() -> None:
    cfg = BridgeConfig(
        owners=["owner"],
        allowed_users=[],
        allowed_groups=["123"],
        commands={
            "ask": True,
            "plan": True,
            "search": True,
            "task": True,
            "status": True,
            "help": True,
            "profile": True,
        },
    )
    pol = Policy(cfg, fake_runner)

    for idx, cmd in enumerate(("ask", "plan", "search", "task", "status", "help", "profile")):
        ev = make_ev(f"/{cmd} hi", sender="group-member", group="123", mid=f"gm-ro-{idx}")
        ok, reason = pol.allow(ev, cmd)
        assert ok, reason


def test_allowed_group_member_cannot_use_owner_only_commands() -> None:
    cfg = BridgeConfig(
        owners=["owner"],
        allowed_users=[],
        allowed_groups=["123"],
        commands={
            "code": True,
            "shell": True,
            "approve": True,
            "stop": True,
            "reset": True,
            "reload": True,
        },
        workspaces={"/tmp": True},
    )
    pol = Policy(cfg, fake_runner)

    for idx, cmd in enumerate(("code", "shell", "approve", "stop", "reset", "reload")):
        ev = make_ev(f"/{cmd} hi", sender="group-member", group="123", mid=f"gm-rw-{idx}")
        ok, reason = pol.allow(ev, cmd)
        assert not ok
        assert reason == "owner-only"


def test_private_sender_still_needs_user_allowlist() -> None:
    cfg = BridgeConfig(
        owners=["owner"],
        allowed_users=[],
        allowed_groups=["123"],
        commands={"ask": True},
    )
    pol = Policy(cfg, fake_runner)
    ok, reason = pol.allow(make_ev("/ask hi", sender="stranger"), "ask")
    assert not ok
    assert reason == "user-denied"


def test_owner_can_start_code_confirmation_flow() -> None:
    cfg = BridgeConfig(
        owners=["owner"],
        allowed_users=[],
        commands={"code": True},
        dangerous_requires_confirm=True,
        workspaces={"/tmp": True},
    )
    cfg.agent.default_workspace = "/tmp"
    pol = Policy(cfg, fake_runner)
    ev = make_ev("/code touch file", sender="owner")
    parsed = pol.parse(ev.text)
    assert parsed is not None
    ok, reason = pol.allow(ev, parsed.name)
    assert ok, reason
    jid, nonce = pol.start_job(ev, parsed)
    assert jid in pol.jobs
    assert nonce is not None


def test_deny() -> None:
    cfg = BridgeConfig()
    pol = Policy(cfg, fake_runner)
    ev = make_ev("hi")
    p = pol.parse(ev.text) or pol.parse("/ask hi")
    assert p is not None
    ok, reason = pol.allow(ev, p.name)
    assert not ok
    assert "user" in reason or "denied" in reason


def test_nonce_flow() -> None:
    cfg = BridgeConfig(
        owners=["1000000001"],
        allowed_users=["1000000001"],
        commands={"code": True},
        dangerous_requires_confirm=True,
        workspaces={"/tmp": True},
    )
    pol = Policy(cfg, fake_runner)
    ev = make_ev("/code do it")
    p = pol.parse(ev.text)
    assert p
    jid, nonce = pol.start_job(ev, p)
    assert nonce is not None
    # approve
    async def go() -> None:
        res = await pol.approve(jid, nonce or "", "1000000001")
        assert res == jid

    asyncio.run(go())


def test_non_owner_cannot_approve_even_if_original_sender() -> None:
    cfg = BridgeConfig(
        owners=["owner"],
        allowed_users=["reader"],
        commands={"code": True},
        dangerous_requires_confirm=True,
        workspaces={"/tmp": True},
    )
    cfg.agent.default_workspace = "/tmp"
    pol = Policy(cfg, fake_runner)
    ev = make_ev("/code do it", sender="reader")
    parsed = pol.parse(ev.text)
    assert parsed is not None
    jid, nonce = pol.start_job(ev, parsed)
    assert nonce is not None

    async def go() -> None:
        assert await pol.approve(jid, nonce, "reader") is None

    asyncio.run(go())


def test_workspace_allowlist_does_not_allow_prefix_collision() -> None:
    cfg = BridgeConfig(workspaces={"/opt/workspaces": True})
    assert cfg.is_workspace_allowed("/opt/workspaces")
    assert cfg.is_workspace_allowed("/opt/workspaces/qq-agent-bridge")
    assert not cfg.is_workspace_allowed("/opt/workspaces_evil")


def test_status_reports_running_and_queued_jobs_when_limited() -> None:
    async def go() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_runner(job: Job) -> str:
            started.set()
            await release.wait()
            return f"ran {job.cmd} {job.args}"

        cfg = BridgeConfig(
            owners=["1000000001"],
            allowed_users=["1000000001"],
            commands={"ask": True},
        )
        cfg.agent.max_concurrent_jobs = 1
        pol = Policy(cfg, blocking_runner)

        try:
            ev1 = make_ev("/ask one", mid="q1")
            ev2 = make_ev("/ask two", mid="q2")
            p1 = pol.parse(ev1.text)
            p2 = pol.parse(ev2.text)
            assert p1 is not None
            assert p2 is not None
            jid1, _ = pol.start_job(ev1, p1)
            jid2, _ = pol.start_job(ev2, p2)
            pol.start_job_task(pol.jobs[jid1])
            pol.start_job_task(pol.jobs[jid2])

            await asyncio.wait_for(started.wait(), timeout=0.2)
            await asyncio.sleep(0)

            status = pol.get_status()
            assert "running:" in status
            assert "queued:" in status
            assert jid1 in status
            assert jid2 in status
        finally:
            release.set()
            tasks = [job.task for job in pol.jobs.values() if job.task]
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(go())


def test_status_lists_readable_indices_and_summaries() -> None:
    cfg = BridgeConfig(
        owners=["1000000001"],
        allowed_users=["1000000001"],
        commands={"task": True},
    )
    pol = Policy(cfg, fake_runner)

    for idx, text in enumerate(("/task old job", "/task newest job")):
        ev = make_ev(text, mid=f"status-index-{idx}")
        parsed = pol.parse(ev.text)
        assert parsed is not None
        jid, _ = pol.start_job(ev, parsed)
        pol.jobs[jid].state = "running" if idx == 0 else "queued"

    status = pol.get_status()
    assert "0." in status
    assert "1." in status
    assert "running" in status
    assert "queued" in status
    assert "1000000001" in status
    assert "old job" in status
    assert "newest job" in status


def test_status_resolves_positive_and_negative_indices() -> None:
    cfg = BridgeConfig(
        owners=["1000000001"],
        allowed_users=["1000000001"],
        commands={"task": True},
    )
    pol = Policy(cfg, fake_runner)

    for idx, text in enumerate(("/task first summary", "/task latest summary")):
        ev = make_ev(text, mid=f"status-ref-{idx}")
        parsed = pol.parse(ev.text)
        assert parsed is not None
        pol.start_job(ev, parsed)

    assert "first summary" in pol.get_status("0")
    assert "latest summary" in pol.get_status("-1")
    assert "unknown job" in pol.get_status("3")


def test_status_hides_finished_jobs_and_indexes_only_active_jobs() -> None:
    cfg = BridgeConfig(
        owners=["1000000001"],
        allowed_users=["1000000001"],
        commands={"task": True},
    )
    pol = Policy(cfg, fake_runner)

    for idx, text in enumerate(("/task finished summary", "/task running summary")):
        ev = make_ev(text, mid=f"status-active-{idx}")
        parsed = pol.parse(ev.text)
        assert parsed is not None
        jid, _ = pol.start_job(ev, parsed)
        pol.jobs[jid].state = "done" if idx == 0 else "running"

    status = pol.get_status()

    assert "finished summary" not in status
    assert "running summary" in status
    assert "0." in status
    assert "1." not in status
    assert "running summary" in pol.get_status("0")
    assert "unknown job" in pol.get_status("-2")


def test_cancel_by_ref_defaults_to_latest_job() -> None:
    cfg = BridgeConfig(
        owners=["owner"],
        commands={"code": True},
        dangerous_requires_confirm=True,
        workspaces={"/tmp": True},
    )
    cfg.agent.default_workspace = "/tmp"
    pol = Policy(cfg, fake_runner)

    jids: list[str] = []
    for idx in range(2):
        ev = make_ev(f"/code edit {idx}", sender="owner", mid=f"cancel-ref-{idx}")
        parsed = pol.parse(ev.text)
        assert parsed is not None
        jid, _ = pol.start_job(ev, parsed)
        jids.append(jid)

    ok, jid, job, reason = pol.cancel_by_ref("", "owner")

    assert ok, reason
    assert jid == jids[-1]
    assert job is pol.jobs[jids[-1]]
    assert pol.jobs[jids[-1]].state == "cancelled"
    assert pol.jobs[jids[0]].state == "waiting_approval"


def test_multiple_jobs_can_run_concurrently() -> None:
    async def go() -> None:
        active = 0
        both_running = asyncio.Event()
        release = asyncio.Event()

        async def blocking_runner(job: Job) -> str:
            nonlocal active
            active += 1
            if active == 2:
                both_running.set()
            await release.wait()
            active -= 1
            return f"ran {job.cmd} {job.args}"

        cfg = BridgeConfig(
            owners=["1000000001"],
            allowed_users=["1000000001"],
            commands={"ask": True},
        )
        cfg.agent.max_concurrent_jobs = 2
        pol = Policy(cfg, blocking_runner)

        try:
            for idx in range(2):
                ev = make_ev(f"/ask {idx}", mid=f"c{idx}")
                parsed = pol.parse(ev.text)
                assert parsed is not None
                jid, _ = pol.start_job(ev, parsed)
                pol.start_job_task(pol.jobs[jid])

            await asyncio.wait_for(both_running.wait(), timeout=0.2)
        finally:
            release.set()
            tasks = [job.task for job in pol.jobs.values() if job.task]
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(go())


def test_cleanup_prunes_finished_jobs_and_seen_messages() -> None:
    async def go() -> None:
        cfg = BridgeConfig(
            owners=["1000000001"],
            allowed_users=["1000000001"],
            commands={"ask": True},
            max_finished_jobs=1,
            max_seen_messages=2,
        )
        pol = Policy(cfg, fake_runner)

        for idx in range(3):
            ev = make_ev(f"/ask {idx}", mid=f"done-{idx}")
            parsed = pol.parse(ev.text)
            assert parsed is not None
            ok, reason = pol.allow(ev, parsed.name)
            assert ok, reason
            jid, _ = pol.start_job(ev, parsed)
            pol.start_job_task(pol.jobs[jid])

        tasks = [job.task for job in pol.jobs.values() if job.task]
        await asyncio.gather(*tasks)
        await pol.cleanup()

        assert len(pol.jobs) == 1
        assert len(pol.seen) == 2
        assert "done-2" in pol.seen

    asyncio.run(go())


def test_cleanup_prunes_excess_waiting_approval_jobs() -> None:
    cfg = BridgeConfig(
        owners=["owner"],
        commands={"code": True},
        dangerous_requires_confirm=True,
        max_finished_jobs=1,
        workspaces={"/tmp": True},
    )
    cfg.agent.default_workspace = "/tmp"
    pol = Policy(cfg, fake_runner)

    for idx in range(3):
        ev = make_ev(f"/code {idx}", sender="owner", mid=f"wait-{idx}")
        parsed = pol.parse(ev.text)
        assert parsed is not None
        pol.start_job(ev, parsed)

    asyncio.run(pol.cleanup())

    waiting = [job for job in pol.jobs.values() if job.state == "waiting_approval"]
    assert len(waiting) == 1
    assert waiting[0].args == "2"
