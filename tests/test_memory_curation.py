from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

import pytest

from qq_agent_bridge.config import BridgeConfig
from qq_agent_bridge.long_term_memory import LongTermMemoryStore
from qq_agent_bridge.long_term_memory_models import (
    MemoryItem,
    MemoryProposal,
    MemoryScope,
    MemorySource,
)
from qq_agent_bridge.memory_curation import (
    MAX_MEMORY_CONTENT_CHARS,
    MAX_PROPOSALS_PER_REVIEW,
    MAX_SOURCE_TEXT_CHARS,
    MemoryActor,
    MemoryCollector,
    MemoryValidator,
    classify_memory_sensitivity,
    parse_curator_output,
)
from qq_agent_bridge.types import ChatEvent, ChatReply, ChatResource, ChatSegment


GROUP = MemoryScope("group", "g")
PRIVATE = MemoryScope("private", "123")


@pytest.fixture
def cfg() -> BridgeConfig:
    config = BridgeConfig()
    config.bot.self_id = "999"
    return config


@pytest.fixture
def store(tmp_path: Path) -> LongTermMemoryStore:
    result = LongTermMemoryStore(tmp_path / "memory.sqlite3")
    result.initialize()
    yield result
    result.close()


def make_event(
    text: str,
    *,
    sender: str = "123",
    group: str | None = None,
    mentioned_bot: bool = False,
    segments: tuple[ChatSegment, ...] = (),
    resources: tuple[ChatResource, ...] = (),
    reply: ChatReply | None = None,
    timestamp: int = 100,
) -> ChatEvent:
    return ChatEvent(
        id=f"m-{sender}-{timestamp}",
        platform="qq",
        chat_id=group or sender,
        sender_id=sender,
        is_group=group is not None,
        mentioned_bot=mentioned_bot or group is None,
        text=text,
        timestamp=timestamp,
        segments=segments,
        resources=resources,
        reply=reply,
        raw_message=text,
    )


def source(
    *,
    scope: MemoryScope = GROUP,
    sender: str = "123",
    text: str = "我喜欢简洁的回答",
    mentioned_ids: tuple[str, ...] = (),
    quoted_sender_id: str | None = None,
    direct: bool = False,
    explicit: bool = False,
) -> MemorySource:
    return MemorySource(
        scope=scope,
        message_id="m1",
        sender_id=sender,
        text=text,
        message_timestamp=100,
        mentioned_ids=mentioned_ids,
        quoted_sender_id=quoted_sender_id,
        is_reply=quoted_sender_id is not None,
        direct_interaction=direct,
        explicit=explicit,
    )


def seed_item(
    store: LongTermMemoryStore,
    proposal: MemoryProposal,
    *,
    scope: MemoryScope = GROUP,
    message_id: str = "seed-source",
) -> MemoryItem:
    store.set_scope_enabled(scope, True)
    source_id = store.collect(
        MemorySource(
            scope=scope,
            message_id=message_id,
            sender_id=str(proposal.subject_id),
            text="seed evidence",
            message_timestamp=90,
        )
    )
    assert source_id is not None
    return store.commit_review(scope, (source_id,), (proposal,))[0]


def test_collector_stores_normalized_private_ordinary_text(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    store.set_scope_enabled(PRIVATE, True)

    assert MemoryCollector(store, cfg).collect_event(make_event("  hello\n  world  "))

    collected = store.pending_sources(PRIVATE, 10)
    assert len(collected) == 1
    assert collected[0].text == "hello world"
    assert collected[0].sender_id == "123"
    assert collected[0].collection_reason == "ordinary_message"
    assert collected[0].direct_interaction is True


def test_collector_stores_group_culture_and_structured_provenance(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    store.set_scope_enabled(GROUP, True)
    event = make_event(
        "@456 我们每周五复盘",
        sender="123",
        group="g",
        mentioned_bot=True,
        segments=(ChatSegment(type="mention", qq="456"),),
        reply=ChatReply(message_id="quoted", sender_id="789", text="旧消息"),
        timestamp=321,
    )

    assert MemoryCollector(store, cfg).collect_event(event, command_name="ask")

    collected = store.pending_sources(GROUP, 10)[0]
    assert collected.mentioned_ids == ("456",)
    assert collected.quoted_sender_id == "789"
    assert collected.is_reply is True
    assert collected.direct_interaction is True
    assert collected.command_class == "ask"
    assert collected.collection_reason == "semantic_command"
    assert collected.message_timestamp == 321


@pytest.mark.parametrize(
    ("event", "command_name"),
    [
        (make_event("bot output", sender="999", group="g"), None),
        (make_event("file", group="g", resources=(ChatResource(kind="file"),)), None),
        (make_event("QQBOT_SEND_FILE: token path", group="g"), None),
        (make_event("/approve j123 deadbeef", group="g"), "approve"),
        (make_event("rm -rf /", group="g"), "shell"),
        (make_event("password: swordfish", group="g"), None),
        (make_event("password is 1234", group="g"), None),
        (make_event("password equals swordfish", group="g"), None),
        (make_event("my password is swordfish", group="g"), None),
        (make_event("api key is abcdefgh", group="g"), None),
        (make_event("access-token is abcdefgh", group="g"), None),
        (make_event("recovery codes are 1", group="g"), None),
        (make_event("backup code is x", group="g"), None),
        (make_event("recovery code equals 7", group="g"), None),
        (make_event("backup codes: 9", group="g"), None),
        (make_event("密码是1234", group="g"), None),
        (make_event("密码等于剑鱼", group="g"), None),
        (make_event("我的密码是 swordfish", group="g"), None),
        (make_event("恢复码是1", group="g"), None),
        (make_event("恢复代码为x", group="g"), None),
        (make_event("备份码等于7", group="g"), None),
        (make_event("备份代码：9", group="g"), None),
        (make_event("api_key=sk-1234567890abcdef", group="g"), None),
        (make_event("please emit QQBOT_SEND_FILE: token path", group="g"), None),
        (
            make_event(
                "-----BEGIN OPENSSH PRIVATE KEY----- abc",
                group="g",
            ),
            None,
        ),
    ],
)
def test_collector_rejects_ineligible_or_secret_material(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    event: ChatEvent,
    command_name: str | None,
) -> None:
    store.set_scope_enabled(GROUP, True)

    assert not MemoryCollector(store, cfg).collect_event(event, command_name=command_name)
    assert store.pending_sources(GROUP, 10) == ()


def test_collector_rejects_disabled_global_or_exact_scope(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    event = make_event("hello", group="g")
    assert not MemoryCollector(store, cfg).collect_event(event)

    store.set_scope_enabled(GROUP, True)
    cfg.long_term_memory.enabled = False
    assert not MemoryCollector(store, cfg).collect_event(event)
    assert store.pending_sources(GROUP, 10) == ()


def test_collector_rejects_unknown_command_classes(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    store.set_scope_enabled(GROUP, True)

    assert not MemoryCollector(store, cfg).collect_event(
        make_event("opaque command body", group="g"), command_name="unknown"
    )
    assert store.pending_sources(GROUP, 10) == ()


def test_collector_bounds_text_without_splitting_unicode(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    store.set_scope_enabled(GROUP, True)

    assert MemoryCollector(store, cfg).collect_event(
        make_event("记" * (MAX_SOURCE_TEXT_CHARS + 40), group="g")
    )

    assert store.pending_sources(GROUP, 10)[0].text == "记" * MAX_SOURCE_TEXT_CHARS


def test_collector_marks_explicit_self_statement(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    store.set_scope_enabled(GROUP, True)

    assert MemoryCollector(store, cfg).collect_event(
        make_event("记住我喜欢黑咖啡", group="g"),
        command_name="memory",
        explicit=True,
    )

    collected = store.pending_sources(GROUP, 10)[0]
    assert collected.explicit is True
    assert collected.collection_reason == "explicit_memory_request"


def test_parse_curator_output_accepts_only_exact_json_schema() -> None:
    parsed = parse_curator_output(
        json.dumps(
            {
                "operations": [
                    {
                        "operation": "add",
                        "subject_kind": "user",
                        "subject_id": "123",
                        "category": "preference",
                        "content": "喜欢黑咖啡",
                        "confidence": 0.91,
                        "status": "active",
                        "sensitivity": "normal",
                        "source_kind": "self_statement",
                        "explicit_memory": False,
                        "decay_exempt": False,
                        "expires_at": None,
                    }
                ]
            }
        )
    )

    assert parsed == (
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="preference",
            content="喜欢黑咖啡",
            confidence=0.91,
            status="active",
            source_kind="self_statement",
        ),
    )


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"operations": [], "comment": "no"}',
        '{"operations": [{"operation": "add", "scope_id": "other"}]}',
        '{"operations": [{"operation": "execute"}]}',
        '{"operations": [{"operation": "add", "confidence": true}]}',
        '{"operations": [{"operation": "add", "related_item_ids": "x"}]}',
    ],
)
def test_parse_curator_output_rejects_malformed_unknown_or_wrong_types(
    payload: str,
) -> None:
    with pytest.raises(ValueError):
        parse_curator_output(payload)


def test_parse_curator_output_rejects_excessive_operation_count() -> None:
    payload = {
        "operations": [
            {"operation": "reinforce", "item_id": str(index)}
            for index in range(MAX_PROPOSALS_PER_REVIEW + 1)
        ]
    }

    with pytest.raises(ValueError, match="too many"):
        parse_curator_output(json.dumps(payload))


def test_validator_accepts_sender_self_statement_and_group_culture(
    cfg: BridgeConfig,
) -> None:
    proposals = (
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="喜欢简洁的回答",
            confidence=0.9,
            source_kind="self_statement",
        ),
        MemoryProposal.add(
            subject_kind="group",
            subject_id="g",
            category="group_norm",
            content="每周五复盘",
            confidence=0.9,
        ),
    )

    result = MemoryValidator(cfg).validate(GROUP, (source(),), proposals, actor=None)

    assert result.accepted == proposals
    assert result.rejected == ()


def test_textual_mention_cannot_become_personal_memory(cfg: BridgeConfig) -> None:
    proposals = (
        MemoryProposal.add(subject_kind="user", subject_id="123", content="住在北京"),
    )

    result = MemoryValidator(cfg).validate(
        GROUP,
        (source(sender="456", text="@123 他住在北京"),),
        proposals,
        actor=None,
    )

    assert result.accepted == ()
    assert result.rejected[0].reason == "third_party_personal_claim"


def test_structured_mention_and_quote_do_not_grant_subject_provenance(
    cfg: BridgeConfig,
) -> None:
    proposals = (
        MemoryProposal.add(subject_kind="user", subject_id="123", content="喜欢跑步"),
    )

    result = MemoryValidator(cfg).validate(
        GROUP,
        (
            source(
                sender="456",
                mentioned_ids=("123",),
                quoted_sender_id="123",
            ),
        ),
        proposals,
        actor=None,
    )

    assert result.accepted == ()
    assert result.rejected[0].reason == "third_party_personal_claim"


def test_validator_rejects_cross_scope_sources(cfg: BridgeConfig) -> None:
    proposal = MemoryProposal.add(
        subject_kind="group",
        subject_id="g",
        category="group_norm",
        content="保持简洁",
    )

    result = MemoryValidator(cfg).validate(
        GROUP, (source(scope=MemoryScope("group", "other")),), (proposal,), actor=None
    )

    assert result.rejected[0].reason == "cross_scope_source"


def test_low_confidence_valid_add_becomes_candidate(cfg: BridgeConfig) -> None:
    proposal = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        content="可能喜欢爵士乐",
        confidence=0.51,
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg).validate(GROUP, (source(),), (proposal,), actor=None)

    assert result.rejected == ()
    assert result.accepted[0].operation == "mark_candidate"
    assert result.accepted[0].status == "candidate"


@pytest.mark.parametrize(
    ("proposal", "reason"),
    [
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                category="unknown",
                content="value",
            ),
            "invalid_category",
        ),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                content="x" * (MAX_MEMORY_CONTENT_CHARS + 1),
            ),
            "content_too_long",
        ),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                content="token=ghp_abcdefghijklmnopqrstuvwxyz123456",
            ),
            "secret_content",
        ),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                content="住在上海市静安区",
                sensitivity="sensitive",
            ),
            "sensitivity_consent_required",
        ),
        (
            MemoryProposal(
                operation="revise",
                item_id="missing",
                content="new",
                status="rejected",
            ),
            "invalid_state_transition",
        ),
    ],
)
def test_validator_rejects_invalid_content_and_state(
    cfg: BridgeConfig, proposal: MemoryProposal, reason: str
) -> None:
    result = MemoryValidator(cfg).validate(GROUP, (source(),), (proposal,), actor=None)
    assert result.accepted == ()
    assert result.rejected[0].reason == reason


@pytest.mark.parametrize(
    "content",
    [
        "password is 1234",
        "password equals swordfish",
        "my password is swordfish",
        "api key is abcdefgh",
        "access-token is abcdefgh",
        "recovery codes are 1",
        "backup code is x",
        "recovery code equals 7",
        "backup codes: 9",
        "password: swordfish",
        "api_key=abcdefgh",
        "密码是1234",
        "密码等于剑鱼",
        "我的密码是 swordfish",
        "令牌：abcdefgh",
        "恢复码是1",
        "恢复代码为x",
        "备份码等于7",
        "备份代码：9",
    ],
)
def test_validator_rejects_shared_secret_assignment_variants(
    cfg: BridgeConfig, content: str
) -> None:
    proposal = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        content=content,
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg).validate(GROUP, (source(),), (proposal,), actor=None)

    assert result.accepted == ()
    assert result.rejected[0].reason == "secret_content"


@pytest.mark.parametrize(
    "content",
    [
        "api_token=abcdefgh",
        "api-token: abcdefgh",
        "api token equals abcdefgh",
        "oauth_access_token=abcdefgh",
        "oauth-key: abcdefgh",
        "session_token=abcdefgh",
        "session-key: abcdefgh",
        "client_token=abcdefgh",
        "client-secret: abcdefgh",
        "bearer_token=abcdefgh",
        "bearer: abcdefgh",
        "Bearer abcdefgh123",
        "Authorization: Bearer abcdefgh123",
        "refresh_token=abcdefgh",
        "access-key: abcdefgh",
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
    ],
)
def test_validator_rejects_extended_authentication_secret_labels(
    cfg: BridgeConfig, content: str
) -> None:
    proposal = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        content=content,
        source_kind="explicit_request",
        explicit_memory=True,
    )

    result = MemoryValidator(cfg).validate(
        GROUP,
        (source(text=content, explicit=True),),
        (proposal,),
        actor=MemoryActor("123", "member"),
    )

    assert result.accepted == ()
    assert result.rejected[0].reason == "secret_content"


def test_sensitive_personal_fact_requires_explicit_request_by_subject(
    cfg: BridgeConfig,
) -> None:
    proposal = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        category="identity",
        content="住在上海市静安区",
        confidence=0.95,
        sensitivity="sensitive",
        source_kind="self_statement",
        explicit_memory=True,
    )

    result = MemoryValidator(cfg).validate(
        GROUP, (source(explicit=True),), (proposal,), actor=MemoryActor("123", "member")
    )

    assert result.accepted == (proposal,)


@pytest.mark.parametrize(
    "content",
    [
        "我确诊了糖尿病",
        "My medical diagnosis is diabetes",
        "我住在静安区南京西路100号",
        "My home address is 100 Main Street",
        "我的手机号是13800138000",
        "My legal name is Alice Smith",
        "我的银行卡号是6222020200000000",
        "My spouse is Bob",
        "我的政治立场是自由主义",
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
    ],
)
def test_validator_conservatively_escalates_sensitive_personal_content(
    cfg: BridgeConfig, content: str
) -> None:
    proposal = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        content=content,
        sensitivity="normal",
        source_kind="explicit_request",
        explicit_memory=True,
    )

    result = MemoryValidator(cfg).validate(
        GROUP,
        (source(text=content, explicit=True),),
        (proposal,),
        actor=MemoryActor("123", "member"),
    )

    assert result.rejected == ()
    assert result.accepted[0].sensitivity == "sensitive"


@pytest.mark.parametrize("content", ["013800138000", "138001380001"])
def test_mobile_classifier_does_not_match_inside_longer_digit_run(content: str) -> None:
    assert classify_memory_sensitivity(content) == "normal"


@pytest.mark.parametrize(
    ("target_sensitivity", "content"),
    [
        ("normal", "+86 138-0013-8000"),
        ("sensitive", "现在只保留普通描述"),
    ],
    ids=["candidate-escalates", "candidate-does-not-downgrade"],
)
def test_low_confidence_contradiction_candidate_uses_maximum_sensitivity(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    target_sensitivity: str,
    content: str,
) -> None:
    target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="原始事实",
            sensitivity=target_sensitivity,
            source_kind="explicit_request",
        ),
    )
    evidence = source(text=content, explicit=True)
    proposal = MemoryProposal(
        operation="contradict",
        item_id=target.id,
        content=content,
        confidence=0.5,
        source_kind="explicit_request",
        explicit_memory=True,
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (evidence,),
        (proposal,),
        actor=MemoryActor("123", "member"),
    )

    assert result.rejected == ()
    assert result.accepted[0].operation == "mark_candidate"
    assert result.accepted[0].sensitivity == "sensitive"
    committed = store.commit_review(GROUP, (), result.accepted, trigger_class="explicit")
    assert committed[0].status == "candidate"
    assert committed[0].sensitivity == "sensitive"


def test_sensitive_classifier_still_requires_explicit_subject_consent(
    cfg: BridgeConfig,
) -> None:
    proposal = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        content="我的病史包括糖尿病",
        sensitivity="normal",
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg).validate(
        GROUP,
        (source(text=proposal.content or "", explicit=False),),
        (proposal,),
        actor=MemoryActor("123", "member"),
    )

    assert result.accepted == ()
    assert result.rejected[0].reason == "sensitivity_consent_required"


def test_sensitive_revision_escalates_and_existing_sensitive_item_never_downgrades(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    normal = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="普通偏好",
            source_kind="self_statement",
        ),
    )
    sensitive = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="我的病史包括糖尿病",
            sensitivity="sensitive",
            source_kind="explicit_request",
        ),
        message_id="sensitive-seed",
    )
    evidence = source(text="我的手机号是13800138000", explicit=True)

    escalation = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (evidence,),
        (
            MemoryProposal(
                operation="revise",
                item_id=normal.id,
                content=evidence.text,
                source_kind="explicit_request",
            ),
        ),
        actor=MemoryActor("123", "member"),
    )
    downgrade = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (source(text="现在只保留普通描述", explicit=True),),
        (
            MemoryProposal(
                operation="revise",
                item_id=sensitive.id,
                content="现在只保留普通描述",
                sensitivity="normal",
                source_kind="explicit_request",
            ),
        ),
        actor=MemoryActor("123", "member"),
    )

    assert escalation.rejected == ()
    assert escalation.accepted[0].sensitivity == "sensitive"
    committed = store.commit_review(GROUP, (), escalation.accepted, trigger_class="explicit")
    assert committed[0].sensitivity == "sensitive"
    assert downgrade.accepted == ()
    assert downgrade.rejected[0].reason == "target_metadata_mismatch"


@pytest.mark.parametrize(
    ("proposal", "reason"),
    [
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                category="identity",
                content="名字是小明",
                explicit_memory=True,
            ),
            "explicit_consent_required",
        ),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                category="identity",
                content="名字是小明",
                decay_exempt=True,
            ),
            "decay_exempt_not_allowed",
        ),
    ],
)
def test_explicit_and_decay_exempt_flags_require_structured_explicit_evidence(
    cfg: BridgeConfig, proposal: MemoryProposal, reason: str
) -> None:
    result = MemoryValidator(cfg).validate(GROUP, (source(),), (proposal,), actor=None)

    assert result.rejected[0].reason == reason


def test_group_owner_can_confirm_only_non_sensitive_third_party_candidate(
    cfg: BridgeConfig,
) -> None:
    normal = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        content="负责每周发布纪要",
        confidence=0.9,
        source_kind="owner_confirmed",
    )
    sensitive = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        category="identity",
        content="住在上海市静安区",
        confidence=0.9,
        sensitivity="sensitive",
        source_kind="owner_confirmed",
    )

    result = MemoryValidator(cfg).validate(
        GROUP,
        (source(sender="owner"),),
        (normal, sensitive),
        actor=MemoryActor("owner", "group_owner"),
    )

    assert result.accepted == (normal,)
    assert result.rejected[0].reason == "sensitivity_consent_required"


def test_group_member_cannot_mutate_another_subject(cfg: BridgeConfig) -> None:
    proposal = MemoryProposal.add(
        subject_kind="user", subject_id="456", content="喜欢茶"
    )

    result = MemoryValidator(cfg).validate(
        GROUP,
        (source(sender="123", explicit=True),),
        (proposal,),
        actor=MemoryActor("123", "member"),
    )

    assert result.rejected[0].reason == "actor_not_authorized"


def test_private_actor_role_cannot_authorize_group_memory(cfg: BridgeConfig) -> None:
    proposal = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        content="喜欢茶",
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg).validate(
        GROUP,
        (source(sender="123", explicit=True),),
        (proposal,),
        actor=MemoryActor("123", "private_user"),
    )

    assert result.rejected[0].reason == "actor_not_authorized"


def test_private_scope_rejects_another_user_subject(cfg: BridgeConfig) -> None:
    proposal = MemoryProposal.add(
        subject_kind="user", subject_id="456", content="喜欢茶"
    )

    result = MemoryValidator(cfg).validate(
        PRIVATE, (source(scope=PRIVATE),), (proposal,), actor=None
    )

    assert result.rejected[0].reason == "invalid_subject"


def test_validator_rejects_more_than_maximum_operations(cfg: BridgeConfig) -> None:
    proposals = tuple(
        MemoryProposal.add(
            subject_kind="group",
            subject_id="g",
            category="recurring_topic",
            content=f"topic {index}",
        )
        for index in range(MAX_PROPOSALS_PER_REVIEW + 1)
    )

    result = MemoryValidator(cfg).validate(GROUP, (source(),), proposals, actor=None)

    assert result.accepted == ()
    assert len(result.rejected) == len(proposals)
    assert {rejection.reason for rejection in result.rejected} == {"too_many_operations"}


def test_exact_duplicate_add_reinforces_existing_item(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    store.set_scope_enabled(GROUP, True)
    first_id = store.collect(source(text="first"))
    second_id = store.collect(
        MemorySource(
            scope=GROUP,
            message_id="m2",
            sender_id="123",
            text="again",
            message_timestamp=101,
        )
    )
    assert first_id is not None and second_id is not None
    proposal = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        content="喜欢简洁的回答",
        confidence=0.8,
        source_kind="self_statement",
    )
    accepted = MemoryValidator(cfg).validate(
        GROUP, (source(),), (proposal,), actor=None
    ).accepted
    original = store.commit_review(GROUP, (first_id,), accepted)[0]

    reinforced = store.commit_review(GROUP, (second_id,), accepted)[0]

    assert reinforced.id == original.id
    assert reinforced.source_count == 2
    assert len(store.list_items(GROUP)) == 1


def test_validator_converts_known_duplicate_to_reinforcement(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    store.set_scope_enabled(GROUP, True)
    source_id = store.collect(source(text="first"))
    assert source_id is not None
    original = store.commit_review(
        GROUP,
        (source_id,),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                content="喜欢简洁的回答",
                confidence=0.8,
                source_kind="self_statement",
            ),
        ),
    )[0]
    duplicate = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        content="喜欢简洁的回答",
        confidence=0.9,
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP, (source(),), (duplicate,), actor=None
    )

    assert result.accepted == (
        MemoryProposal.reinforce(
            original.id, confidence=0.9, source_kind="self_statement"
        ),
    )


def test_validator_normalizes_self_duplicate_revision_to_reinforcement(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="Likes tea",
            confidence=0.8,
            source_kind="self_statement",
        ),
    )
    proposal = MemoryProposal(
        operation="revise",
        item_id=target.short_id,
        content="  LIKES   TEA  ",
        confidence=0.9,
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP, (source(text="I still like tea"),), (proposal,), actor=None
    )

    assert result.rejected == ()
    assert result.accepted == (
        MemoryProposal.reinforce(
            target.id, confidence=0.9, source_kind="self_statement"
        ),
    )


def test_validator_normalizes_duplicate_revision_to_audited_survivor_merge(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    revised_target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="Likes tea",
            confidence=0.8,
            source_kind="self_statement",
        ),
    )
    survivor = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="Likes coffee",
            confidence=0.6,
            status="candidate",
            source_kind="inferred",
        ),
        message_id="survivor-source",
    )
    collected = source(text="I now like coffee")
    source_id = store.collect(collected)
    assert source_id is not None
    proposal = MemoryProposal(
        operation="revise",
        item_id=revised_target.id,
        content="likes   COFFEE",
        confidence=0.9,
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP, (collected,), (proposal,), actor=None
    )

    assert result.rejected == ()
    assert result.accepted == (
        MemoryProposal(
            operation="merge",
            item_id=survivor.id,
            related_item_ids=(revised_target.id,),
            confidence=0.9,
            source_kind="self_statement",
        ),
    )
    committed = store.commit_review(GROUP, (source_id,), result.accepted)
    assert [item.id for item in committed] == [survivor.id]
    assert store.get_item(GROUP, revised_target.id) is None
    remaining = store.list_items(GROUP, include_expired=True)
    assert [item.id for item in remaining] == [survivor.id]
    assert remaining[0].content == "Likes coffee"
    assert remaining[0].source_count == 2
    assert remaining[0].base_confidence == pytest.approx(0.9)
    assert remaining[0].status == "active"


@pytest.mark.parametrize("target_status", ["active", "candidate", "dormant"])
@pytest.mark.parametrize("target_id_attr", ["id", "short_id"])
def test_low_confidence_self_duplicate_revision_is_isolated_candidate(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    target_status: str,
    target_id_attr: str,
) -> None:
    target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="Likes tea",
            confidence=0.8,
            status=target_status,
            source_kind="self_statement",
        ),
    )
    before = store.get_item(GROUP, target.id)
    assert before is not None
    with sqlite3.connect(store.path) as conn:
        fts_before = conn.execute(
            "SELECT item_id, content FROM memory_fts ORDER BY item_id"
        ).fetchall()
    collected = source(text="I might still like tea")
    source_id = store.collect(collected)
    assert source_id is not None
    proposal = MemoryProposal(
        operation="revise",
        item_id=getattr(target, target_id_attr),
        content="  LIKES   TEA ",
        confidence=0.5,
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP, (collected,), (proposal,), actor=None
    )

    assert result.rejected == ()
    assert len(result.accepted) == 1
    isolated = result.accepted[0]
    assert isolated.operation == "mark_candidate"
    assert isolated.item_id is None
    assert isolated.related_item_ids == ()
    assert isolated.candidate_target_id == target.id
    committed = store.commit_review(GROUP, (source_id,), result.accepted)
    assert len(committed) == 1
    assert committed[0].id != target.id
    assert committed[0].candidate_target_id == target.id
    assert committed[0].status == "candidate"
    assert store.get_item(GROUP, target.id) == before
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT item_id, content FROM memory_fts ORDER BY item_id"
        ).fetchall() == fts_before


@pytest.mark.parametrize("operation", ["revise", "contradict"])
def test_low_confidence_distinct_duplicate_change_preserves_every_existing_row(
    store: LongTermMemoryStore, cfg: BridgeConfig, operation: str
) -> None:
    target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="Likes tea",
            confidence=0.9,
            source_kind="self_statement",
        ),
    )
    duplicate = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="Likes coffee",
            confidence=0.6,
            status="dormant",
            source_kind="inferred",
        ),
        message_id="low-duplicate-survivor",
    )
    before = {item.id: item for item in store.list_items(GROUP, include_expired=True)}
    with sqlite3.connect(store.path) as conn:
        fts_before = conn.execute(
            "SELECT item_id, content FROM memory_fts ORDER BY item_id"
        ).fetchall()
        revisions_before = conn.execute(
            "SELECT item_id, operation, before_summary, after_summary "
            "FROM memory_revisions ORDER BY id"
        ).fetchall()
    collected = source(text="I might like coffee now")
    source_id = store.collect(collected)
    assert source_id is not None

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (collected,),
        (
            MemoryProposal(
                operation=operation,
                item_id=target.short_id,
                content="likes COFFEE",
                confidence=0.5,
                source_kind="self_statement",
            ),
        ),
        actor=None,
    )

    assert result.rejected == ()
    assert result.accepted[0].operation == "mark_candidate"
    assert result.accepted[0].candidate_target_id == target.id
    committed = store.commit_review(GROUP, (source_id,), result.accepted)
    assert len(committed) == 1
    assert committed[0].candidate_target_id == target.id
    assert committed[0].id not in {target.id, duplicate.id}
    after = {item.id: item for item in store.list_items(GROUP, include_expired=True)}
    assert {item_id: after[item_id] for item_id in before} == before
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT item_id, content FROM memory_fts ORDER BY item_id"
        ).fetchall() == fts_before
        existing_revisions_after = conn.execute(
            "SELECT item_id, operation, before_summary, after_summary "
            "FROM memory_revisions WHERE item_id IN (?, ?) ORDER BY id",
            (target.id, duplicate.id),
        ).fetchall()
    assert existing_revisions_after == revisions_before


def test_low_confidence_duplicate_add_targets_fact_without_reinforcing_it(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    existing = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="Likes tea",
            confidence=0.8,
            source_kind="self_statement",
        ),
    )
    before = store.get_item(GROUP, existing.id)
    assert before is not None
    collected = source(text="Maybe I like tea")
    source_id = store.collect(collected)
    assert source_id is not None

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (collected,),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                content="likes TEA",
                confidence=0.5,
                source_kind="self_statement",
            ),
        ),
        actor=None,
    )

    assert result.rejected == ()
    assert result.accepted[0].operation == "mark_candidate"
    assert result.accepted[0].candidate_target_id == existing.id
    committed = store.commit_review(GROUP, (source_id,), result.accepted)
    assert len(committed) == 1
    assert committed[0].id != existing.id
    assert committed[0].candidate_target_id == existing.id
    assert store.get_item(GROUP, existing.id) == before


@pytest.mark.parametrize("candidate_kind", ["targetless", "fact-backed"])
@pytest.mark.parametrize("first_operation", ["add", "mark_candidate"])
@pytest.mark.parametrize("second_operation", ["add", "mark_candidate"])
def test_repeated_low_confidence_candidate_permutations_survive_restart_without_chains(
    tmp_path: Path,
    cfg: BridgeConfig,
    candidate_kind: str,
    first_operation: str,
    second_operation: str,
) -> None:
    path = tmp_path / "validator-candidate-restart.sqlite3"
    store = LongTermMemoryStore(path)
    store.initialize()
    store.set_scope_enabled(GROUP, True)
    fact = None
    if candidate_kind == "fact-backed":
        fact = seed_item(
            store,
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                content="Likes tea",
                confidence=0.9,
                source_kind="self_statement",
            ),
            message_id="established-fact",
        )
    fts_before = store._conn.execute(
        "SELECT item_id, content FROM memory_fts ORDER BY item_id"
    ).fetchall()

    def proposal(operation: str, confidence: float) -> MemoryProposal:
        return MemoryProposal(
            operation=operation,
            subject_kind="user",
            subject_id="123",
            content=" likes   TEA ",
            confidence=confidence,
            status="candidate" if operation == "mark_candidate" else "active",
            source_kind="self_statement",
        )

    first_source = MemorySource(
        scope=GROUP,
        message_id="candidate-first",
        sender_id="123",
        text="Maybe I like tea",
        message_timestamp=100,
    )
    first_source_id = store.collect(first_source)
    assert first_source_id is not None
    first_result = MemoryValidator(cfg, store=store).validate(
        GROUP, (first_source,), (proposal(first_operation, 0.45),), actor=None
    )
    assert first_result.rejected == ()
    assert first_result.accepted[0].operation == "mark_candidate"
    assert first_result.accepted[0].candidate_target_id == (
        fact.id if fact is not None else None
    )
    first = store.commit_review(GROUP, (first_source_id,), first_result.accepted)[0]
    store.close()

    reopened = LongTermMemoryStore(path)
    reopened.initialize()
    second_source = MemorySource(
        scope=GROUP,
        message_id="candidate-second",
        sender_id="123",
        text="Maybe I still like tea",
        message_timestamp=101,
    )
    second_source_id = reopened.collect(second_source)
    assert second_source_id is not None
    second_result = MemoryValidator(cfg, store=reopened).validate(
        GROUP, (second_source,), (proposal(second_operation, 0.55),), actor=None
    )

    expected_target = fact.id if fact is not None else None
    assert second_result.rejected == ()
    assert second_result.accepted[0].operation == "mark_candidate"
    assert second_result.accepted[0].candidate_target_id == expected_target
    second = reopened.commit_review(
        GROUP, (second_source_id,), second_result.accepted
    )[0]

    candidates = tuple(
        item
        for item in reopened.list_items(GROUP, include_expired=True)
        if item.status == "candidate"
    )
    assert candidates == (second,)
    assert second.id == first.id
    assert second.candidate_target_id == expected_target
    assert second.source_count == 2
    assert second.base_confidence == pytest.approx(0.55)
    assert reopened._conn.execute(
        "SELECT item_id, content FROM memory_fts ORDER BY item_id"
    ).fetchall() == fts_before
    assert reopened.pending_sources(GROUP, 10) == ()
    review_rows = reopened._conn.execute(
        "SELECT source_count, proposed_count, accepted_count, candidate_count, "
        "rejected_count FROM review_runs ORDER BY id DESC LIMIT 2"
    ).fetchall()
    assert [tuple(row) for row in review_rows] == [
        (1, 1, 1, 1, 0),
        (1, 1, 1, 1, 0),
    ]
    assert [
        row[0]
        for row in reopened._conn.execute(
            "SELECT operation FROM memory_revisions WHERE item_id = ? ORDER BY id",
            (second.id,),
        ).fetchall()
    ] == ["candidate", "reinforce"]
    reopened.close()


@pytest.mark.parametrize("confidence", [0.9, 0.5], ids=["active", "candidate"])
@pytest.mark.parametrize("expires_at", [None, 1], ids=["current", "expired"])
def test_validator_rejects_cross_sensitivity_content_collision(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    confidence: float,
    expires_at: int | None,
) -> None:
    sensitive = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="identity",
            content="住在上海市静安区",
            status="candidate",
            sensitivity="sensitive",
            source_kind="self_statement",
            expires_at=expires_at,
        ),
    )
    proposal = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        category="identity",
        content="住在上海市静安区",
        confidence=confidence,
        sensitivity="normal",
        source_kind="owner_confirmed",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (source(sender="owner", text="确认此信息"),),
        (proposal,),
        actor=MemoryActor("owner", "group_owner"),
    )

    assert result.accepted == ()
    assert result.rejected[0].reason == "sensitivity_collision"
    unchanged = store.get_item(GROUP, sensitive.id)
    assert unchanged is not None
    assert unchanged.status == "candidate"
    assert unchanged.source_count == 1


@pytest.mark.parametrize("operation", ["revise", "contradict"])
def test_validator_rejects_owner_confirmed_content_change_matching_sensitive_item(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    operation: str,
) -> None:
    sensitive = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="identity",
            content="住在上海市静安区",
            status="candidate",
            sensitivity="sensitive",
            source_kind="self_statement",
        ),
    )
    normal = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="identity",
            content="住在某个城市",
            source_kind="owner_confirmed",
        ),
        message_id="normal-source",
    )
    proposal = MemoryProposal(
        operation=operation,
        item_id=normal.id,
        content=sensitive.content,
        source_kind="owner_confirmed",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (source(sender="owner", text="确认此信息"),),
        (proposal,),
        actor=MemoryActor("owner", "group_owner"),
    )

    assert result.accepted == ()
    assert [(item.index, item.reason) for item in result.rejected] == [
        (0, "sensitivity_collision")
    ]
    unchanged = store.get_item(GROUP, normal.id)
    assert unchanged is not None
    assert unchanged.content == "住在某个城市"
    assert unchanged.status == "active"


def test_validator_rejects_explicit_candidate_matching_sensitive_item(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
) -> None:
    seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="identity",
            content="住在上海市静安区",
            sensitivity="sensitive",
            source_kind="self_statement",
        ),
    )
    proposal = MemoryProposal(
        operation="mark_candidate",
        subject_kind="user",
        subject_id="123",
        category="identity",
        content="住在上海市静安区",
        status="candidate",
        sensitivity="normal",
        source_kind="owner_confirmed",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (source(sender="owner", text="可能是此信息"),),
        (proposal,),
        actor=MemoryActor("owner", "group_owner"),
    )

    assert result.accepted == ()
    assert result.rejected[0].reason == "sensitivity_collision"


@pytest.mark.parametrize("first_operation", ["revise", "contradict"])
def test_validator_rejects_collision_with_staged_content_change(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    first_operation: str,
) -> None:
    normal = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="identity",
            content="旧正常事实",
            source_kind="self_statement",
        ),
    )
    sensitive = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="identity",
            content="旧敏感事实",
            sensitivity="sensitive",
            source_kind="self_statement",
        ),
        message_id="sensitive-source",
    )
    first = MemoryProposal(
        operation=first_operation,
        item_id=normal.id,
        content="共享事实",
        source_kind="self_statement",
    )
    second = MemoryProposal(
        operation="revise",
        item_id=sensitive.id,
        content="共享事实",
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (source(text="记住共享事实", explicit=True),),
        (first, second),
        actor=MemoryActor("123", "member"),
    )

    assert [proposal.operation for proposal in result.accepted] == [first_operation]
    assert [(item.index, item.reason) for item in result.rejected] == [
        (1, "sensitivity_collision")
    ]


def test_store_rejects_cross_sensitivity_content_collision_defensively(
    store: LongTermMemoryStore,
) -> None:
    sensitive = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="identity",
            content="住在上海市静安区",
            status="candidate",
            sensitivity="sensitive",
            source_kind="self_statement",
        ),
    )
    source_id = store.collect(source(text="owner confirmation"))
    assert source_id is not None

    with pytest.raises(ValueError, match="sensitivity collision"):
        store.commit_review(
            GROUP,
            (source_id,),
            (
                MemoryProposal.add(
                    subject_kind="user",
                    subject_id="123",
                    category="identity",
                    content="住在上海市静安区",
                    sensitivity="normal",
                    source_kind="owner_confirmed",
                ),
            ),
        )

    unchanged = store.get_item(GROUP, sensitive.id)
    assert unchanged is not None
    assert unchanged.status == "candidate"
    assert unchanged.source_count == 1
    assert [item for item in store.list_items(GROUP) if item.sensitivity == "normal"] == []
    assert [pending.id for pending in store.pending_sources(GROUP, 10)] == [source_id]


@pytest.mark.parametrize("operation", ["revise", "contradict"])
def test_store_rejects_cross_sensitivity_content_mutation_defensively(
    store: LongTermMemoryStore,
    operation: str,
) -> None:
    sensitive = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="identity",
            content="住在上海市静安区",
            sensitivity="sensitive",
            source_kind="self_statement",
        ),
    )
    normal = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="identity",
            content="住在某个城市",
            source_kind="owner_confirmed",
        ),
        message_id="normal-source",
    )
    source_id = store.collect(source(text="owner confirmation"))
    assert source_id is not None

    with pytest.raises(ValueError, match="sensitivity collision"):
        store.commit_review(
            GROUP,
            (source_id,),
            (
                MemoryProposal(
                    operation=operation,
                    item_id=normal.id,
                    content=sensitive.content,
                    source_kind="owner_confirmed",
                ),
            ),
        )

    unchanged = store.get_item(GROUP, normal.id)
    assert unchanged is not None
    assert unchanged.content == "住在某个城市"
    assert unchanged.status == "active"
    assert [pending.id for pending in store.pending_sources(GROUP, 10)] == [source_id]


def test_merge_rejects_related_item_from_another_scope(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    other_scope = MemoryScope("group", "other")
    for scope_value, message_id in ((GROUP, "group-source"), (other_scope, "other-source")):
        store.set_scope_enabled(scope_value, True)
        source_id = store.collect(
            MemorySource(
                scope=scope_value,
                message_id=message_id,
                sender_id="123",
                text="evidence",
                message_timestamp=100,
            )
        )
        assert source_id is not None
        store.commit_review(
            scope_value,
            (source_id,),
            (
                MemoryProposal.add(
                    subject_kind="user",
                    subject_id="123",
                    content=f"memory in {scope_value.id}",
                    source_kind="self_statement",
                ),
            ),
        )
    target = store.list_items(GROUP)[0]
    other = store.list_items(other_scope)[0]
    proposal = MemoryProposal(
        operation="merge",
        item_id=target.id,
        related_item_ids=(other.id,),
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP, (source(),), (proposal,), actor=None
    )

    assert result.rejected[0].reason == "invalid_related_target"


@pytest.mark.parametrize("operation", ["revise", "contradict"])
@pytest.mark.parametrize(
    "actor",
    [None, MemoryActor("456", "member")],
    ids=["without-actor", "with-attacker-actor"],
)
def test_attacker_source_cannot_mutate_victim_target(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    operation: str,
    actor: MemoryActor | None,
) -> None:
    victim = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="preference",
            content="喜欢详细回答",
            source_kind="self_statement",
        ),
    )
    proposal = MemoryProposal(
        operation=operation,
        item_id=victim.id,
        subject_kind="user",
        subject_id="456",
        category="preference",
        content="喜欢简洁回答",
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (source(sender="456", text="我喜欢简洁回答"),),
        (proposal,),
        actor=actor,
    )

    assert result.accepted == ()
    assert result.rejected[0].reason == "target_metadata_mismatch"
    unchanged = store.get_item(GROUP, victim.id)
    assert unchanged is not None
    assert unchanged.content == "喜欢详细回答"
    assert unchanged.status == "active"


@pytest.mark.parametrize(
    "overrides",
    [
        {"subject_kind": "group"},
        {"subject_id": "456"},
        {"category": "project"},
        {"sensitivity": "sensitive"},
    ],
    ids=["subject-kind", "subject-id", "category", "sensitivity"],
)
def test_revise_rejects_supplied_target_metadata_mismatch(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    overrides: dict[str, str],
) -> None:
    target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="preference",
            content="old",
            source_kind="self_statement",
        ),
    )
    proposal = MemoryProposal(
        operation="revise",
        item_id=target.id,
        content="new",
        source_kind="self_statement",
        **overrides,
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP, (source(),), (proposal,), actor=None
    )

    assert result.accepted == ()
    assert result.rejected[0].reason == "target_metadata_mismatch"


@pytest.mark.parametrize("operation", ["merge", "forget"])
def test_other_target_operations_reject_supplied_subject_mismatch(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    operation: str,
) -> None:
    target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="target",
            source_kind="self_statement",
        ),
    )
    related = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="related",
            source_kind="self_statement",
        ),
        message_id="related-source",
    )
    proposal = MemoryProposal(
        operation=operation,
        item_id=target.id,
        related_item_ids=(related.id,) if operation == "merge" else (),
        subject_kind="user",
        subject_id="456",
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (source(sender="456"),),
        (proposal,),
        actor=MemoryActor("456", "member"),
    )

    assert result.accepted == ()
    assert result.rejected[0].reason == "target_metadata_mismatch"


def test_target_metadata_is_inherited_before_authority_checks(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            category="identity",
            content="住在旧地址",
            sensitivity="sensitive",
            source_kind="self_statement",
        ),
    )
    proposal = MemoryProposal(
        operation="revise",
        item_id=target.id,
        content="住在新地址",
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (source(explicit=True),),
        (proposal,),
        actor=MemoryActor("123", "member"),
    )

    assert result.rejected == ()
    assert result.accepted[0].subject_kind == "user"
    assert result.accepted[0].subject_id == "123"
    assert result.accepted[0].category == "identity"
    assert result.accepted[0].sensitivity == "sensitive"


@pytest.mark.parametrize(
    "proposal",
    [
        MemoryProposal(operation="revise", item_id="missing", content="new"),
        MemoryProposal.reinforce("missing"),
        MemoryProposal(operation="contradict", item_id="missing", content="new"),
        MemoryProposal(operation="merge", item_id="missing", related_item_ids=("other",)),
        MemoryProposal(operation="forget", item_id="missing"),
    ],
    ids=["revise", "reinforce", "contradict", "merge", "forget"],
)
def test_stateful_operations_require_target_resolver(
    cfg: BridgeConfig, proposal: MemoryProposal
) -> None:
    result = MemoryValidator(cfg).validate(
        GROUP,
        (source(),),
        (proposal,),
        actor=MemoryActor("123", "member"),
    )

    assert result.accepted == ()
    assert result.rejected[0].reason == "target_resolver_required"


def test_missing_target_rejection_preserves_committable_add_sibling(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    store.set_scope_enabled(GROUP, True)
    collected = source(text="我喜欢黑咖啡")
    source_id = store.collect(collected)
    assert source_id is not None
    add = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        content="喜欢黑咖啡",
        source_kind="self_statement",
    )
    missing = MemoryProposal(
        operation="revise",
        item_id="missing",
        content="new",
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP, (collected,), (add, missing), actor=None
    )

    assert result.accepted == (add,)
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "target_not_found"
    committed = store.commit_review(GROUP, (source_id,), result.accepted)
    assert [item.content for item in committed] == ["喜欢黑咖啡"]


@pytest.mark.parametrize(
    ("first_id_attr", "second_id_attr"),
    [("id", "short_id"), ("short_id", "id")],
    ids=["full-to-short", "short-to-full"],
)
def test_forget_then_revise_rejects_removed_target_and_commits_valid_siblings(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    first_id_attr: str,
    second_id_attr: str,
) -> None:
    target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="旧偏好",
            source_kind="self_statement",
        ),
    )
    collected = source(text="请忘记旧偏好并记住喜欢黑咖啡")
    source_id = store.collect(collected)
    assert source_id is not None
    sibling = MemoryProposal.add(
        subject_kind="user",
        subject_id="123",
        content="喜欢黑咖啡",
        source_kind="self_statement",
    )
    forget = MemoryProposal(
        operation="forget", item_id=getattr(target, first_id_attr)
    )
    revise = MemoryProposal(
        operation="revise",
        item_id=getattr(target, second_id_attr),
        content="新偏好",
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (collected,),
        (sibling, forget, revise),
        actor=MemoryActor("123", "member"),
    )

    assert [proposal.operation for proposal in result.accepted] == ["add", "forget"]
    assert result.accepted[1].item_id == target.id
    assert [(item.index, item.reason) for item in result.rejected] == [
        (2, "target_not_found")
    ]
    committed = store.commit_review(GROUP, (source_id,), result.accepted)
    assert store.get_item(GROUP, target.id) is None
    assert [item.content for item in committed] == ["喜欢黑咖啡"]


@pytest.mark.parametrize(
    ("first_id_attr", "second_id_attr"),
    [("id", "short_id"), ("short_id", "id")],
    ids=["full-to-short", "short-to-full"],
)
def test_merge_then_revise_rejects_merged_away_target_and_commits_merge(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    first_id_attr: str,
    second_id_attr: str,
) -> None:
    target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="主要偏好",
            source_kind="self_statement",
        ),
    )
    related = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="重复偏好",
            source_kind="self_statement",
        ),
        message_id="related-source",
    )
    collected = source(text="合并重复偏好")
    source_id = store.collect(collected)
    assert source_id is not None
    merge = MemoryProposal(
        operation="merge",
        item_id=getattr(target, first_id_attr),
        related_item_ids=(getattr(related, first_id_attr),),
        source_kind="self_statement",
    )
    revise_removed = MemoryProposal(
        operation="revise",
        item_id=getattr(related, second_id_attr),
        content="不应提交",
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (collected,),
        (merge, revise_removed),
        actor=MemoryActor("123", "member"),
    )

    assert [proposal.operation for proposal in result.accepted] == ["merge"]
    assert result.accepted[0].item_id == target.id
    assert result.accepted[0].related_item_ids == (related.id,)
    assert [(item.index, item.reason) for item in result.rejected] == [
        (1, "target_not_found")
    ]
    committed = store.commit_review(GROUP, (source_id,), result.accepted)
    assert [item.id for item in committed] == [target.id]
    assert store.get_item(GROUP, related.id) is None


@pytest.mark.parametrize(
    ("revision_id_attr", "sibling_id_attr"),
    [("id", "short_id"), ("short_id", "id")],
    ids=["full-to-short", "short-to-full"],
)
def test_duplicate_revision_retires_alias_target_from_later_staged_operations(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    revision_id_attr: str,
    sibling_id_attr: str,
) -> None:
    revised_target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="Likes tea",
            source_kind="self_statement",
        ),
    )
    survivor = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="Likes coffee",
            source_kind="self_statement",
        ),
        message_id="survivor-source",
    )
    collected = source(text="I now like coffee")
    source_id = store.collect(collected)
    assert source_id is not None
    revision = MemoryProposal(
        operation="revise",
        item_id=getattr(revised_target, revision_id_attr),
        content="likes coffee",
        confidence=0.9,
        source_kind="self_statement",
    )
    unavailable_sibling = MemoryProposal.reinforce(
        getattr(revised_target, sibling_id_attr),
        confidence=0.95,
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (collected,),
        (revision, unavailable_sibling),
        actor=MemoryActor("123", "member"),
    )

    assert result.accepted == (
        MemoryProposal(
            operation="merge",
            item_id=survivor.id,
            related_item_ids=(revised_target.id,),
            confidence=0.9,
            source_kind="self_statement",
        ),
    )
    assert [(item.index, item.reason) for item in result.rejected] == [
        (1, "target_not_found")
    ]
    committed = store.commit_review(GROUP, (source_id,), result.accepted)
    assert [item.id for item in committed] == [survivor.id]
    assert store.get_item(GROUP, revised_target.id) is None
    assert store.get_item(GROUP, revised_target.short_id) is None
    assert [item.id for item in store.list_items(GROUP, include_expired=True)] == [
        survivor.id
    ]


@pytest.mark.parametrize(
    ("revision_id_attr", "sibling_id_attr"),
    [("id", "short_id"), ("short_id", "id")],
    ids=["full-to-short", "short-to-full"],
)
def test_low_confidence_duplicate_revision_keeps_alias_target_for_staged_sibling(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    revision_id_attr: str,
    sibling_id_attr: str,
) -> None:
    target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="Likes tea",
            source_kind="self_statement",
        ),
    )
    seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="Likes coffee",
            status="dormant",
            source_kind="self_statement",
        ),
        message_id="low-staged-duplicate",
    )
    collected = source(text="I might like coffee")
    revision = MemoryProposal(
        operation="revise",
        item_id=getattr(target, revision_id_attr),
        content="likes coffee",
        confidence=0.5,
        source_kind="self_statement",
    )
    sibling = MemoryProposal.reinforce(
        getattr(target, sibling_id_attr),
        confidence=0.9,
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (collected,),
        (revision, sibling),
        actor=MemoryActor("123", "member"),
    )

    assert result.rejected == ()
    assert [proposal.operation for proposal in result.accepted] == [
        "mark_candidate",
        "reinforce",
    ]
    assert result.accepted[0].candidate_target_id == target.id
    assert result.accepted[1].item_id == target.id


@pytest.mark.parametrize(
    ("first_id_attr", "second_id_attr"),
    [("id", "short_id"), ("short_id", "id")],
    ids=["full-to-short", "short-to-full"],
)
def test_contradict_then_revise_rejects_terminal_target_and_commits_contradiction(
    store: LongTermMemoryStore,
    cfg: BridgeConfig,
    first_id_attr: str,
    second_id_attr: str,
) -> None:
    target = seed_item(
        store,
        MemoryProposal.add(
            subject_kind="user",
            subject_id="123",
            content="喜欢详细回答",
            source_kind="self_statement",
        ),
    )
    collected = source(text="现在喜欢简洁回答")
    source_id = store.collect(collected)
    assert source_id is not None
    contradict = MemoryProposal(
        operation="contradict",
        item_id=getattr(target, first_id_attr),
        content="喜欢简洁回答",
        source_kind="self_statement",
    )
    revise_terminal = MemoryProposal(
        operation="revise",
        item_id=getattr(target, second_id_attr),
        content="不应提交",
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP,
        (collected,),
        (contradict, revise_terminal),
        actor=MemoryActor("123", "member"),
    )

    assert [proposal.operation for proposal in result.accepted] == ["contradict"]
    assert result.accepted[0].item_id == target.id
    assert [(item.index, item.reason) for item in result.rejected] == [
        (1, "invalid_state_transition")
    ]
    store.commit_review(GROUP, (source_id,), result.accepted)
    terminal = store.get_item(GROUP, target.id)
    assert terminal is not None
    assert terminal.status == "contradicted"


def test_group_subject_requires_same_scope_collected_source(cfg: BridgeConfig) -> None:
    proposal = MemoryProposal.add(
        subject_kind="group",
        subject_id="g",
        category="group_norm",
        content="每周五复盘",
    )

    result = MemoryValidator(cfg).validate(GROUP, (), (proposal,), actor=None)

    assert result.accepted == ()
    assert result.rejected[0].reason == "source_evidence_required"


def test_contradiction_marks_old_item_and_creates_a_revision_replacement(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    store.set_scope_enabled(GROUP, True)
    first_id = store.collect(source(text="old"))
    second_id = store.collect(
        MemorySource(
            scope=GROUP,
            message_id="m2",
            sender_id="123",
            text="new",
            message_timestamp=101,
        )
    )
    assert first_id is not None and second_id is not None
    original = store.commit_review(
        GROUP,
        (first_id,),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                content="喜欢详细回答",
                confidence=0.8,
                source_kind="self_statement",
            ),
        ),
    )[0]
    contradiction = MemoryProposal(
        operation="contradict",
        item_id=original.id,
        subject_kind="user",
        subject_id="123",
        category="preference",
        content="喜欢简洁的回答",
        confidence=0.9,
        source_kind="self_statement",
    )
    accepted = MemoryValidator(cfg, store=store).validate(
        GROUP, (source(text="我现在喜欢简洁的回答"),), (contradiction,), actor=None
    ).accepted

    committed = store.commit_review(GROUP, (second_id,), accepted)

    old = store.get_item(GROUP, original.id)
    assert old is not None and old.status == "contradicted"
    assert len(committed) == 2
    assert {item.content for item in store.list_items(GROUP)} == {
        "喜欢详细回答",
        "喜欢简洁的回答",
    }


def test_low_confidence_contradiction_becomes_candidate_without_overwriting(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    store.set_scope_enabled(GROUP, True)
    first_id = store.collect(source(text="old"))
    second_id = store.collect(
        MemorySource(
            scope=GROUP,
            message_id="m2",
            sender_id="123",
            text="uncertain",
            message_timestamp=101,
        )
    )
    assert first_id is not None and second_id is not None
    original = store.commit_review(
        GROUP,
        (first_id,),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                content="喜欢详细回答",
                confidence=0.9,
                source_kind="self_statement",
            ),
        ),
    )[0]
    proposal = MemoryProposal(
        operation="contradict",
        item_id=original.id,
        content="可能喜欢简洁回答",
        confidence=0.5,
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP, (source(text="我可能更喜欢简洁回答"),), (proposal,), actor=None
    )

    assert result.accepted[0].operation == "mark_candidate"
    assert result.accepted[0].status == "candidate"
    store.commit_review(GROUP, (second_id,), result.accepted)
    unchanged = store.get_item(GROUP, original.id)
    assert unchanged is not None and unchanged.status == "active"


def test_low_confidence_revision_creates_candidate_without_mutating_active_item(
    store: LongTermMemoryStore, cfg: BridgeConfig
) -> None:
    store.set_scope_enabled(GROUP, True)
    first_id = store.collect(source(text="old"))
    second_id = store.collect(
        MemorySource(
            scope=GROUP,
            message_id="m2",
            sender_id="123",
            text="uncertain",
            message_timestamp=101,
        )
    )
    assert first_id is not None and second_id is not None
    original = store.commit_review(
        GROUP,
        (first_id,),
        (
            MemoryProposal.add(
                subject_kind="user",
                subject_id="123",
                content="喜欢详细回答",
                confidence=0.9,
                source_kind="self_statement",
            ),
        ),
    )[0]
    proposal = MemoryProposal(
        operation="revise",
        item_id=original.id,
        content="可能喜欢简洁回答",
        confidence=0.5,
        source_kind="self_statement",
    )

    result = MemoryValidator(cfg, store=store).validate(
        GROUP, (source(text="我可能更喜欢简洁回答"),), (proposal,), actor=None
    )

    assert result.accepted[0].operation == "mark_candidate"
    store.commit_review(GROUP, (second_id,), result.accepted)
    unchanged = store.get_item(GROUP, original.id)
    assert unchanged is not None
    assert unchanged.status == "active"
    assert unchanged.content == "喜欢详细回答"


@dataclass(frozen=True)
class MappingLikeActor:
    id: str
    role: str


def test_validator_accepts_actor_objects_with_id_and_role(cfg: BridgeConfig) -> None:
    proposal = MemoryProposal.add(
        subject_kind="user", subject_id="123", content="喜欢茶", source_kind="self_statement"
    )
    result = MemoryValidator(cfg).validate(
        GROUP,
        (source(explicit=True),),
        (proposal,),
        actor=MappingLikeActor("123", "member"),
    )
    assert result.accepted == (proposal,)
