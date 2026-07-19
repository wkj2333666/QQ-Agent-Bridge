from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import qq_agent_bridge.memory_commands as memory_commands_module
from qq_agent_bridge.config import BridgeConfig
from qq_agent_bridge.long_term_memory import LongTermMemoryRetriever, LongTermMemoryStore
from qq_agent_bridge.long_term_memory_models import (
    MemoryProposal,
    MemoryScope,
    MemorySource,
)
from qq_agent_bridge.memory_commands import MemoryCommandService, build_memory_command_interpreter
from qq_agent_bridge.memory_curation import MemoryActor, MemoryValidator
from qq_agent_bridge.storage_gate import StorageActivityGate
from qq_agent_bridge.types import ChatEvent


GROUP = MemoryScope("group", "g1")


def event(
    sender: str = "member",
    *,
    group: bool = True,
    chat_id: str | None = None,
    mid: str = "m1",
) -> ChatEvent:
    return ChatEvent(
        id=mid,
        platform="qq",
        chat_id=chat_id or ("g1" if group else sender),
        sender_id=sender,
        is_group=group,
        mentioned_bot=group,
        text="",
        timestamp=1_700_000_000,
    )


def config() -> BridgeConfig:
    cfg = BridgeConfig()
    cfg.owners = ["owner"]
    cfg.allowed_users = ["member", "other"]
    cfg.allowed_groups = ["g1"]
    cfg.long_term_memory.enabled = True
    return cfg


def store(tmp_path: Path) -> LongTermMemoryStore:
    result = LongTermMemoryStore(tmp_path / "memory.sqlite3")
    result.initialize()
    return result


def seed(
    db: LongTermMemoryStore,
    *,
    subject_kind: str,
    subject_id: str,
    content: str,
    status: str = "active",
    sensitivity: str = "normal",
) -> str:
    db.set_scope_enabled(GROUP, True)
    committed = db.commit_review(
        GROUP,
        (),
        (
            MemoryProposal.add(
                subject_kind=subject_kind,
                subject_id=subject_id,
                category="group_norm" if subject_kind == "group" else "preference",
                content=content,
                confidence=0.9,
                status=status,
                sensitivity=sensitivity,
                source_kind="explicit_request",
                explicit_memory=True,
                actor_class="test",
            ),
        ),
        trigger_class="explicit",
    )
    return committed[0].short_id


def run(coro):
    return asyncio.run(coro)


def test_status_is_deterministic_and_enablement_persists(tmp_path: Path) -> None:
    db = store(tmp_path)
    calls: list[str] = []

    async def interpreter(prompt: str) -> str:
        calls.append(prompt)
        return '{}'

    service = MemoryCommandService(config(), db, interpreter=interpreter)
    before = run(service.handle(event(), ""))
    denied = run(service.handle(event(), "enable"))
    enabled = run(service.handle(event("owner"), "enable"))

    assert "关闭" in before.text
    assert denied.text.startswith("[denied]")
    assert "已开启" in enabled.text
    assert db.is_scope_enabled(GROUP)
    assert calls == []


def test_private_user_can_manage_only_exact_private_scope(tmp_path: Path) -> None:
    db = store(tmp_path)
    service = MemoryCommandService(config(), db)
    ev = event("member", group=False)

    assert "已开启" in run(service.handle(ev, "enable")).text
    assert db.is_scope_enabled(MemoryScope("private", "member"))
    remembered = run(service.handle(ev, "remember 我喜欢简短回答"))
    assert "已记住" in remembered.text
    items = db.list_items(MemoryScope("private", "member"), subject_id="member")
    assert [item.content for item in items] == ["我喜欢简短回答"]
    assert db.list_items(GROUP) == ()


def test_forged_private_chat_id_cannot_select_another_users_scope(tmp_path: Path) -> None:
    db = store(tmp_path)
    service = MemoryCommandService(config(), db)
    forged = event("member", group=False, chat_id="other")

    assert "已开启" in run(service.handle(forged, "enable")).text
    assert db.is_scope_enabled(MemoryScope("private", "member"))
    assert not db.is_scope_enabled(MemoryScope("private", "other"))


def test_global_disable_cannot_be_overridden_by_scope_command(tmp_path: Path) -> None:
    db = store(tmp_path)
    cfg = config()
    cfg.long_term_memory.enabled = False
    service = MemoryCommandService(cfg, db)

    result = run(service.handle(event("owner"), "enable"))

    assert result.text.startswith("[disabled]")
    assert not db.is_scope_enabled(GROUP)


def test_uninitialized_database_fails_closed_without_exception(tmp_path: Path) -> None:
    db = LongTermMemoryStore(tmp_path / "not-open.sqlite3")
    service = MemoryCommandService(config(), db)

    result = run(service.handle(event(), "remember keep this"))

    assert result.text == "[error] 长期记忆数据库当前不可用。"


def test_group_member_and_owner_lists_do_not_cross_subjects(tmp_path: Path) -> None:
    db = store(tmp_path)
    own_id = seed(db, subject_kind="user", subject_id="member", content="member fact")
    other_id = seed(db, subject_kind="user", subject_id="other", content="other fact")
    group_id = seed(db, subject_kind="group", subject_id="g1", content="group fact")
    service = MemoryCommandService(config(), db)

    member_list = run(service.handle(event(), "list"))
    owner_list = run(service.handle(event("owner"), "list"))

    assert own_id in member_list.text and "member fact" in member_list.text
    assert other_id not in member_list.text and "other fact" not in member_list.text
    assert group_id in owner_list.text and "group fact" in owner_list.text
    assert own_id not in owner_list.text and other_id not in owner_list.text


def test_page_index_is_bound_to_visible_list_snapshot(tmp_path: Path) -> None:
    db = store(tmp_path)
    own_id = seed(db, subject_kind="user", subject_id="member", content="visible")
    seed(db, subject_kind="user", subject_id="other", content="hidden")
    service = MemoryCommandService(config(), db)

    assert own_id in run(service.handle(event(), "list me 1")).text
    shown = run(service.handle(event(), "show 1"))
    attack = run(service.handle(event("other"), "show 1"))

    assert "visible" in shown.text
    assert "先使用 /memory list" in attack.text


def test_clear_token_expires_and_cannot_be_replayed(tmp_path: Path) -> None:
    db = store(tmp_path)
    seed(db, subject_kind="user", subject_id="member", content="erase me")
    now = [100.0]
    service = MemoryCommandService(config(), db, clock=lambda: now[0], confirmation_ttl=10)

    proposal = run(service.handle(event(), "clear me"))
    token = proposal.text.rsplit(" ", 1)[-1]
    now[0] = 111.0
    expired = run(service.handle(event(), f"clear me {token}"))
    assert "失效" in expired.text
    assert db.list_items(GROUP, subject_id="member")

    token = run(service.handle(event(), "clear me")).text.rsplit(" ", 1)[-1]
    cleared = run(service.handle(event(), f"clear me {token}"))
    replay = run(service.handle(event(), f"clear me {token}"))
    assert "已清除" in cleared.text
    assert "失效" in replay.text
    assert db.list_items(GROUP, subject_id="member") == ()


def test_owner_can_clear_member_without_browse_access(tmp_path: Path) -> None:
    db = store(tmp_path)
    hidden = seed(db, subject_kind="user", subject_id="other", content="private-in-group")
    service = MemoryCommandService(config(), db)

    assert hidden not in run(service.handle(event("owner"), "list")).text
    request = run(service.handle(event("owner"), "clear user other"))
    token = request.text.rsplit(" ", 1)[-1]
    result = run(service.handle(event("owner"), f"clear user other {token}"))

    assert "已清除" in result.text
    assert db.list_items(GROUP, subject_id="other") == ()


def test_clear_token_is_actor_bound_and_attack_does_not_consume_it(tmp_path: Path) -> None:
    db = store(tmp_path)
    seed(db, subject_kind="user", subject_id="owner", content="owner fact")
    service = MemoryCommandService(config(), db)
    request = run(service.handle(event("owner"), "clear me"))
    token = request.text.rsplit(" ", 1)[-1]

    attack = run(service.handle(event("member"), f"clear me {token}"))
    success = run(service.handle(event("owner"), f"clear me {token}"))

    assert "失效" in attack.text
    assert "已清除" in success.text
    assert db.list_items(GROUP, subject_id="owner") == ()


def test_forget_never_defaults_or_guesses_ambiguous_reference(tmp_path: Path) -> None:
    db = store(tmp_path)
    seed(db, subject_kind="user", subject_id="member", content="准备考研")
    seed(db, subject_kind="user", subject_id="member", content="考研英语")

    async def interpreter(_prompt: str) -> str:
        return json.dumps({"intent": "forget", "references": ["考研"]}, ensure_ascii=False)

    acknowledgements: list[str] = []

    async def acknowledge(_ev: ChatEvent, text: str) -> None:
        acknowledgements.append(text)

    service = MemoryCommandService(
        config(), db, interpreter=interpreter, acknowledge=acknowledge
    )
    result = run(service.handle(event(), "忘掉考研那件事"))

    assert acknowledgements
    assert "需要" in result.text or "明确" in result.text
    assert len(db.list_items(GROUP, subject_id="member")) == 2


def test_natural_language_acknowledges_before_exactly_one_agent_pass(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.set_scope_enabled(GROUP, True)
    order: list[str] = []

    async def acknowledge(_ev: ChatEvent, _text: str) -> None:
        order.append("ack")

    async def interpreter(prompt: str) -> str:
        assert order == ["ack"]
        assert "other fact" not in prompt
        order.append("agent")
        return json.dumps(
            {"intent": "remember", "content": "我偏好短回复"},
            ensure_ascii=False,
        )

    seed(db, subject_kind="user", subject_id="other", content="other fact")
    service = MemoryCommandService(
        config(), db, interpreter=interpreter, acknowledge=acknowledge
    )
    result = run(service.handle(event(), "以后记得我喜欢短回复"))

    assert order == ["ack", "agent"]
    assert "已记住" in result.text


def test_malformed_or_overbroad_interpreter_output_changes_nothing(tmp_path: Path) -> None:
    db = store(tmp_path)
    seed(db, subject_kind="user", subject_id="member", content="one")
    outputs = iter(
        (
            "not json",
            json.dumps({"intent": "forget", "references": [str(i) for i in range(6)]}),
        )
    )

    async def interpreter(_prompt: str) -> str:
        return next(outputs)

    service = MemoryCommandService(config(), db, interpreter=interpreter)
    assert "说得更明确" in run(service.handle(event(), "第一条自然语言")).text
    assert "最多" in run(service.handle(event(mid="m2"), "第二条自然语言")).text
    assert len(db.list_items(GROUP, subject_id="member")) == 1


def test_model_output_is_reauthorized_after_interpretation(tmp_path: Path) -> None:
    db = store(tmp_path)
    hidden = seed(db, subject_kind="user", subject_id="other", content="hidden")

    async def interpreter(_prompt: str) -> str:
        return json.dumps({"intent": "forget", "references": [hidden]})

    service = MemoryCommandService(config(), db, interpreter=interpreter)
    result = run(service.handle(event(), "删掉那条"))

    assert result.text.startswith("[denied]") or "无权" in result.text
    assert db.get_item(GROUP, hidden) is not None


def test_natural_language_review_returns_background_request(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.set_scope_enabled(GROUP, True)

    async def interpreter(_prompt: str) -> str:
        return '{"intent":"review"}'

    service = MemoryCommandService(config(), db, interpreter=interpreter)
    result = run(service.handle(event("owner"), "整理一下最近聊天"))

    assert result.review_request is not None
    assert result.review_request.scope == GROUP
    assert "已安排" in result.text


@pytest.mark.parametrize("group", [True, False], ids=["owner-group", "private-user"])
@pytest.mark.parametrize(
    ("intent", "initial", "expected"),
    [
        ("status", True, True),
        ("enable", False, True),
        ("disable", True, False),
    ],
)
def test_natural_state_intent_executes_only_selected_handler(
    tmp_path: Path,
    group: bool,
    intent: str,
    initial: bool,
    expected: bool,
) -> None:
    db = store(tmp_path)
    ev = event("owner" if group else "member", group=group)
    scope = GROUP if group else MemoryScope("private", "member")
    db.set_scope_enabled(scope, initial)

    async def interpreter(_prompt: str) -> str:
        return json.dumps({"intent": intent})

    service = MemoryCommandService(config(), db, interpreter=interpreter)
    run(service.handle(ev, "用自然语言管理长期记忆"))

    assert db.is_scope_enabled(scope) is expected


@pytest.mark.parametrize(
    "output",
    [
        '{"intent":"remember","content":123}',
        '{"intent":"remember","content":true}',
        '{"intent":"remember","content":"x","target":"me"}',
        '{"intent":"status","content":"irrelevant"}',
        '{"intent":"list","page":true}',
        '{"intent":"list","page":1.0}',
        '{"intent":"list","target":123}',
        '{"intent":"list","target":[]}',
        '{"intent":"forget"}',
        '{"intent":"forget","references":[true]}',
        '{"intent":"confirm","reference":123}',
        '{"intent":"correct","references":["abc"]}',
        '{"intent":"correct","reference":"abc","content":false}',
        '{"intent":"clear","target":"user"}',
        '{"intent":"clear","target":"user","subject_id":123}',
        '{"intent":"clear","target":[]}',
        '{"intent":"clear","target":"me","subject_id":"other"}',
        '{"intent":"review","unknown":"x"}',
        '{"intent":"status","intent":"disable"}',
        '{"intent":"remember","content":"first","content":"second"}',
        '{"intent":"show","reference":"first","reference":"second"}',
        '{"intent":"list","target":"me","target":"group"}',
    ],
)
def test_intent_specific_schema_rejects_malformed_output_without_mutation(
    tmp_path: Path, output: str
) -> None:
    db = store(tmp_path)
    db.set_scope_enabled(GROUP, True)

    async def interpreter(_prompt: str) -> str:
        return output

    service = MemoryCommandService(config(), db, interpreter=interpreter)
    before = db.status(GROUP)
    result = run(service.handle(event(), "自然语言请求"))

    assert "没能可靠理解" in result.text
    assert db.status(GROUP) == before
    assert db.list_items(GROUP) == ()


@pytest.mark.parametrize(
    "credential",
    [
        "api_token=secretvalue123",
        "api-token: secretvalue123",
        "api token = secretvalue123",
        "oauth_access_token=secretvalue123",
        "session_key=secretvalue123",
        "client-secret=secretvalue123",
        "bearer_token=secretvalue123",
        "refresh_token=secretvalue123",
        "access-key=secretvalue123",
        "OPENAI_API_KEY=opaquevalue123456",
        "AWS_SECRET_ACCESS_KEY=opaquevalue654321",
        "GITHUB_TOKEN=opaquevalue123456",
        "ACME_OAUTH_ACCESS_TOKEN=opaquevalue123456",
        "VENDOR_SESSION_TOKEN=opaquevalue123456",
        "SERVICE_CLIENT_SECRET=opaquevalue123456",
        "PROVIDER_REFRESH_TOKEN=opaquevalue123456",
        "DATABASE_PASSWORD is opaquevalue123456",
        "REDIS_PASSWD are opaquevalue123456",
        "SESSION_COOKIE equals opaquevalue123456",
        "_OPENAI_API_KEY=opaquevalue123456",
        "_PASSWORD=opaquevalue123456",
        "__SERVICE_PRIVATE_KEY: opaquevalue123456",
        "APP_RECOVERY_CODES are opaquevalue123456",
        "APP_BACKUP_CODE equals opaquevalue123456",
        "MY_APP_REFRESH_TOKEN is opaquevalue123456",
        "private key: opaquevalue123456",
        "private-key equals opaquevalue123456",
        "secret key: opaquevalue123456",
        "secret-key equals opaquevalue123456",
        "SECRET_KEY=opaquevalue123456",
        "DJANGO_SECRET_KEY=opaquevalue123456",
        "JWT_SECRET_KEY is opaquevalue123456",
        "passphrase: alpha beta gamma",
        "key-passphrase equals alpha beta gamma",
        "WALLET_PASSPHRASE=alpha beta gamma",
        "SSH_KEY_PASSPHRASE is alpha beta gamma",
        "SSH_PRIVATE_KEY_PASSPHRASE=alpha beta gamma",
        "私钥：opaquevalue123456",
        "recovery phrase is alpha beta gamma",
        "recovery-key: opaquevalue123456",
        "backup phrase are alpha beta gamma",
        "backup code equals opaquevalue123456",
        "seed phrase is alpha beta gamma",
        "seed-key: opaquevalue123456",
        "seed code equals opaquevalue123456",
        "mnemonic: alpha beta gamma",
        "mnemonic phrase equals alpha beta gamma",
        "mnemonic words are alpha beta gamma",
        "recovery words: alpha beta gamma",
        "backup-words equals alpha beta gamma",
        "seed words is alpha beta gamma",
        "_SEED_PHRASE=alpha beta gamma",
        "__MNEMONIC_PHRASE is alpha beta gamma",
        "WALLET_MNEMONIC_WORDS=alpha beta gamma",
        "APP_RECOVERY_WORDS are alpha beta gamma",
        "_BACKUP_WORDS: alpha beta gamma",
        "__SEED_WORDS equals alpha beta gamma",
        "助记词是 甲乙丙丁",
        "恢复短语：甲乙丙丁",
        "种子短语等于 甲乙丙丁",
    ],
)
def test_remember_and_correct_reject_extended_credential_labels(
    tmp_path: Path, credential: str
) -> None:
    db = store(tmp_path)
    db.set_scope_enabled(GROUP, True)
    item_id = seed(db, subject_kind="user", subject_id="member", content="safe value")
    service = MemoryCommandService(config(), db)

    remembered = run(service.handle(event(), f"remember {credential}"))
    corrected = run(service.handle(event(), f"correct {item_id} {credential}"))

    assert "不能" in remembered.text
    assert "不能" in corrected.text
    items = db.list_items(GROUP, subject_id="member")
    assert [item.content for item in items] == ["safe value"]


def test_forget_scrubs_credential_fixture_from_items_fts_and_revisions(tmp_path: Path) -> None:
    db = store(tmp_path)
    credential = "api_token=secretvalue123"
    item_id = seed(db, subject_kind="user", subject_id="member", content=credential)
    service = MemoryCommandService(config(), db)

    assert "已忘记" in run(service.handle(event(), f"forget {item_id}")).text

    assert db.get_item(GROUP, item_id) is None
    assert db._conn.execute(  # noqa: SLF001 - assert storage scrubbing, not API behavior
        "SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH 'secretvalue123'"
    ).fetchone()[0] == 0
    revisions = db._conn.execute(  # noqa: SLF001
        "SELECT before_summary, after_summary FROM memory_revisions"
    ).fetchall()
    assert all(credential not in str(value) for row in revisions for value in row)


@pytest.mark.parametrize(
    "content",
    [
        "我确诊了糖尿病",
        "My medical diagnosis is diabetes",
        "我住在静安区南京西路100号",
        "My home address is 100 Main Street",
        "我的手机号是13800138000",
        "My email is alice@example.com",
        "我的身份证号是110101199001011234",
        "My passport number is X12345678",
        "我的银行卡号是6222020200000000",
        "My annual salary is 100000 dollars",
        "我的伴侣是小王",
        "I am bisexual and married",
        "我的政治立场是自由主义",
        "My political affiliation is independent",
        "我的宗教信仰是佛教",
        "My religion is Buddhism",
        "13800138000",
        "110101199001011234",
        "6222020200000000",
        "+8613800138000",
        "138-0013-8000",
        "138 0013 8000",
        "110101-19900101-1234",
        "6222 0202 0000 0000",
        "6222-0202-0000-0000",
        "+86 (138) 0013-8000",
        "138.0013.8000",
        "(138) 0013 8000",
        "+86-(138)-0013.8000",
        "138–0013–8000",
        "138—0013—8000",
        "138‐0013‑8000",
        "138‒0013−8000",
        "138―0013﹘8000",
        "138﹣0013－8000",
        "138．0013．8000",
        "＋86（138）0013－8000",
        "＋86　（138）　0013－8000",
        "＋86 (138) 0013-8000",
    ],
)
def test_remember_and_correct_conservatively_escalate_sensitive_content(
    tmp_path: Path, content: str
) -> None:
    db = store(tmp_path)
    db.set_scope_enabled(GROUP, True)
    item_id = seed(db, subject_kind="user", subject_id="member", content="普通偏好")
    service = MemoryCommandService(config(), db)

    remembered = run(service.handle(event(), f"remember {content}"))
    corrected = run(service.handle(event(), f"correct {item_id} {content}"))

    assert "已记住" in remembered.text
    assert "已更正" in corrected.text
    items = db.list_items(GROUP, subject_id="member")
    assert items
    assert all(item.sensitivity == "sensitive" for item in items)


def test_correcting_sensitive_item_to_benign_text_does_not_downgrade(tmp_path: Path) -> None:
    db = store(tmp_path)
    item_id = seed(
        db,
        subject_kind="user",
        subject_id="member",
        content="我的手机号是13800138000",
        sensitivity="sensitive",
    )
    service = MemoryCommandService(config(), db)

    result = run(service.handle(event(), f"correct {item_id} 以后通过应用联系我"))

    assert "已更正" in result.text
    item = db.get_item(GROUP, item_id)
    assert item is not None
    assert item.sensitivity == "sensitive"


@pytest.mark.parametrize(
    "content",
    [
        "+86 138 0013 8000",
        "+86 (138) 0013-8000",
        "138.0013.8000",
        "138–0013—8000",
        "＋86　（138）　0013－8000",
        "110101-19900101-1234",
        "6222-0202-0000-0000",
    ],
    ids=[
        "formatted-mobile",
        "parenthesized-mobile",
        "dotted-mobile",
        "unicode-dash-mobile",
        "fullwidth-mobile",
        "formatted-id",
        "formatted-card",
    ],
)
def test_formatted_identifier_remains_sensitive_after_benign_correction(
    tmp_path: Path, content: str
) -> None:
    db = store(tmp_path)
    db.set_scope_enabled(GROUP, True)
    service = MemoryCommandService(config(), db)

    assert "已记住" in run(service.handle(event(), f"remember {content}")).text
    item = db.list_items(GROUP, subject_id="member")[0]
    assert item.sensitivity == "sensitive"

    assert "已更正" in run(
        service.handle(event(mid="m2"), f"correct {item.id} 改用应用内联系")
    ).text
    corrected = db.get_item(GROUP, item.id)
    assert corrected is not None
    assert corrected.sensitivity == "sensitive"


def test_low_confidence_sensitive_contradiction_confirmation_stays_private(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    db.set_scope_enabled(GROUP, True)
    target_id = seed(
        db,
        subject_kind="user",
        subject_id="member",
        content="普通联系方式",
    )
    cfg = config()
    content = "+86 138-0013-8000"
    source = MemorySource(
        scope=GROUP,
        message_id="low-sensitive",
        sender_id="member",
        text=content,
        message_timestamp=1_700_000_001,
        explicit=True,
    )
    validation = MemoryValidator(cfg, store=db).validate(
        GROUP,
        (source,),
        (
            MemoryProposal(
                operation="contradict",
                item_id=target_id,
                content=content,
                confidence=0.5,
                source_kind="explicit_request",
                explicit_memory=True,
            ),
        ),
        actor=MemoryActor("member", "member"),
    )
    assert validation.rejected == ()
    assert validation.accepted[0].operation == "mark_candidate"
    assert validation.accepted[0].sensitivity == "sensitive"

    committed = db.commit_review(GROUP, (), validation.accepted, trigger_class="explicit")
    candidate = committed[0]
    assert candidate.status == "candidate"
    assert candidate.sensitivity == "sensitive"

    service = MemoryCommandService(cfg, db)
    assert "已确认" in run(service.handle(event(mid="confirm"), f"confirm {candidate.id}")).text
    confirmed = db.get_item(GROUP, candidate.id)
    assert confirmed is not None
    assert confirmed.status == "active"
    assert confirmed.sensitivity == "sensitive"
    retrieved = LongTermMemoryRetriever(db, cfg.long_term_memory).retrieve(
        GROUP, "member", (), None, content
    )
    assert content not in retrieved


def test_candidate_confirmation_respects_subject_and_sensitivity(tmp_path: Path) -> None:
    db = store(tmp_path)
    own = seed(
        db,
        subject_kind="user",
        subject_id="member",
        content="own candidate",
        status="candidate",
        sensitivity="sensitive",
    )
    other_normal = seed(
        db,
        subject_kind="user",
        subject_id="other",
        content="other candidate",
        status="candidate",
    )
    other_sensitive = seed(
        db,
        subject_kind="user",
        subject_id="other",
        content="other sensitive candidate",
        status="candidate",
        sensitivity="sensitive",
    )
    service = MemoryCommandService(config(), db)

    assert "已确认" in run(service.handle(event(), f"confirm {own}")).text
    assert "已确认" in run(service.handle(event("owner"), f"confirm {other_normal}")).text
    denied = run(service.handle(event("owner"), f"confirm {other_sensitive}"))

    assert denied.text.startswith("[denied]")
    assert db.get_item(GROUP, own).status == "active"  # type: ignore[union-attr]
    assert db.get_item(GROUP, other_normal).status == "active"  # type: ignore[union-attr]
    assert db.get_item(GROUP, other_sensitive).status == "candidate"  # type: ignore[union-attr]


def test_interpreter_exception_makes_no_change(tmp_path: Path) -> None:
    db = store(tmp_path)
    seed(db, subject_kind="user", subject_id="member", content="keep")

    async def interpreter(_prompt: str) -> str:
        raise RuntimeError("raw model failure")

    service = MemoryCommandService(config(), db, interpreter=interpreter)
    result = run(service.handle(event(), "删掉记忆"))

    assert "说得更明确" in result.text
    assert "raw model failure" not in result.text
    assert len(db.list_items(GROUP, subject_id="member")) == 1


def test_production_interpreter_forces_restricted_ask_auto(monkeypatch) -> None:
    cfg = config()
    restricted = SimpleNamespace(
        cfg=SimpleNamespace(agent=SimpleNamespace(default_workspace="/isolated"))
    )
    calls: list[tuple[object, str, str, str, str | None]] = []

    def fake_builder(*_args, **_kwargs):
        return restricted

    async def fake_run_agent(agent, prompt, workspace, mode, *, model=None, **_kwargs):
        calls.append((agent, prompt, workspace, mode, model))
        return '{"intent":"status"}'

    monkeypatch.setattr(memory_commands_module, "build_restricted_agent_adapter", fake_builder)
    monkeypatch.setattr(memory_commands_module, "run_agent", fake_run_agent)
    interpreter = build_memory_command_interpreter(cfg, StorageActivityGate(), "/ignored")

    assert run(interpreter("prompt")) == '{"intent":"status"}'
    assert calls == [(restricted, "prompt", "/isolated", "ask", "auto")]
