"""Eligibility, parsing, and deterministic validation for memory curation."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from typing import Mapping, Sequence

from .config import BridgeConfig, LongTermMemoryConfig
from .long_term_memory import LongTermMemoryStore
from .long_term_memory_models import (
    ALLOWED_CATEGORIES,
    ALLOWED_OPERATIONS,
    ALLOWED_STATUSES,
    MemoryItem,
    MemoryProposal,
    MemoryScope,
    MemorySource,
    memory_identity_key,
)
from .types import ChatEvent


MAX_SOURCE_TEXT_CHARS = 2_000
MAX_MEMORY_CONTENT_CHARS = 500
MAX_PROPOSALS_PER_REVIEW = 20
ACTIVE_CONFIDENCE_THRESHOLD = 0.70

ALLOWED_SENSITIVITIES = frozenset({"normal", "sensitive", "secret"})
ALLOWED_SOURCE_KINDS = frozenset(
    {
        "inferred",
        "self_statement",
        "direct_interaction",
        "explicit_request",
        "owner_confirmed",
    }
)
STATEFUL_OPERATIONS = ALLOWED_OPERATIONS - {"add", "mark_candidate"}
TARGET_METADATA_FIELDS = ("subject_kind", "subject_id", "category", "sensitivity")

_SEMANTIC_COMMANDS = frozenset({"ask", "plan", "task"})
_DANGEROUS_COMMAND_RE = re.compile(
    r"^\s*/(?:approve|code|mode|permission|profile|reload|reset|schedule|shell|stop)\b",
    re.IGNORECASE,
)
_APPROVAL_NONCE_RE = re.compile(
    r"(?:^|\s)/?approve\s+\S+\s+[0-9a-f]{6,}(?:\s|$)", re.IGNORECASE
)
_INTERNAL_DIRECTIVE_RE = re.compile(
    r"(?:QQBOT_(?:SEND|PROGRESS)|::(?:code-comment|git-|created-thread)|"
    r"<system\b|资源发送令牌\s*[：:])",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk|ghp|gho|github_pat)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(?:api[\s_-]*key|access[\s_-]*token|auth[\s_-]*token|token|"
        r"password|passwd|secret|cookie)\b\s*(?:is\b|equals\b|[=:：])\s*\S"
    ),
    re.compile(r"(?:密码|口令|令牌)\s*(?:是|为|等于|[:：=])\s*\S"),
    re.compile(r"(?i)\b(?:recovery|backup)\s+codes?\s*[=:：]\s*\S+"),
)

_PROPOSAL_FIELDS = frozenset(
    {
        "operation",
        "item_id",
        "related_item_ids",
        "subject_kind",
        "subject_id",
        "category",
        "content",
        "confidence",
        "status",
        "sensitivity",
        "source_kind",
        "explicit_memory",
        "decay_exempt",
        "expires_at",
    }
)
_STRING_FIELDS = frozenset(
    {
        "operation",
        "item_id",
        "subject_kind",
        "subject_id",
        "category",
        "content",
        "status",
        "sensitivity",
        "source_kind",
    }
)


@dataclass(frozen=True)
class MemoryActor:
    id: str
    role: str


@dataclass(frozen=True)
class RejectedProposal:
    proposal: MemoryProposal
    reason: str
    index: int


@dataclass(frozen=True)
class ValidationResult:
    accepted: tuple[MemoryProposal, ...]
    rejected: tuple[RejectedProposal, ...]

    @property
    def candidates(self) -> tuple[MemoryProposal, ...]:
        return tuple(
            proposal
            for proposal in self.accepted
            if proposal.operation == "mark_candidate" or proposal.status == "candidate"
        )


class MemoryCollector:
    """Collect bounded user-authored evidence without invoking an Agent."""

    def __init__(self, store: LongTermMemoryStore, cfg: BridgeConfig | LongTermMemoryConfig):
        self.store = store
        self.cfg = cfg
        self.memory_cfg = _memory_config(cfg)
        bot = getattr(cfg, "bot", None)
        self.bot_id = str(getattr(bot, "self_id", "") or "")

    def collect_event(
        self,
        ev: ChatEvent,
        command_name: str | None = None,
        explicit: bool = False,
    ) -> bool:
        scope = MemoryScope("group" if ev.is_group else "private", ev.chat_id)
        command = str(command_name).strip().lower() if command_name else None
        if not self.memory_cfg.enabled or not self.store.is_scope_enabled(scope):
            return False
        if self.bot_id and str(ev.sender_id) == self.bot_id:
            return False
        if ev.resources or any(segment.resource is not None for segment in ev.segments):
            return False
        if command is not None and command not in _SEMANTIC_COMMANDS and not (
            command == "memory" and explicit
        ):
            return False

        text = _normalize_text(ev.text)
        if not text:
            return False
        if (
            _DANGEROUS_COMMAND_RE.search(text)
            or _APPROVAL_NONCE_RE.search(text)
            or _INTERNAL_DIRECTIVE_RE.search(text)
            or _contains_secret(text)
        ):
            return False

        mentions = tuple(
            dict.fromkeys(
                str(segment.qq)
                for segment in ev.segments
                if segment.type in {"mention", "at"} and segment.qq
            )
        )
        quoted_sender = (
            str(ev.reply.sender_id)
            if ev.reply is not None and ev.reply.sender_id is not None
            else None
        )
        direct = bool(
            not ev.is_group
            or ev.mentioned_bot
            or (self.bot_id and quoted_sender == self.bot_id)
        )
        reason = (
            "explicit_memory_request"
            if explicit
            else ("semantic_command" if command in _SEMANTIC_COMMANDS else "group_culture")
            if ev.is_group
            else "ordinary_message"
        )
        if not ev.is_group and command in _SEMANTIC_COMMANDS:
            reason = "semantic_command"

        source = MemorySource(
            scope=scope,
            message_id=str(ev.id),
            sender_id=str(ev.sender_id),
            text=text[:MAX_SOURCE_TEXT_CHARS],
            message_timestamp=int(ev.timestamp),
            mentioned_ids=mentions,
            quoted_sender_id=quoted_sender,
            is_reply=ev.reply is not None,
            direct_interaction=direct,
            command_class=command,
            collection_reason=reason,
            explicit=bool(explicit),
        )
        return self.store.collect(source) is not None


def parse_curator_output(text: str) -> tuple[MemoryProposal, ...]:
    """Parse the curator's exact JSON envelope without coercing field types."""
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("curator output is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"operations"}:
        raise ValueError("curator output must contain only operations")
    operations = payload["operations"]
    if not isinstance(operations, list):
        raise ValueError("curator operations must be a list")
    if len(operations) > MAX_PROPOSALS_PER_REVIEW:
        raise ValueError("curator output has too many operations")
    return tuple(_parse_proposal(value) for value in operations)


class MemoryValidator:
    """Apply deterministic scope, provenance, consent, and transition rules."""

    def __init__(
        self,
        cfg: BridgeConfig | LongTermMemoryConfig,
        *,
        store: LongTermMemoryStore | None = None,
    ) -> None:
        self.cfg = _memory_config(cfg)
        self.store = store

    def validate(
        self,
        scope: MemoryScope,
        sources: Sequence[MemorySource],
        proposals: Sequence[MemoryProposal],
        actor: object | None,
    ) -> ValidationResult:
        source_tuple = tuple(sources)
        proposal_tuple = tuple(proposals)
        if len(proposal_tuple) > MAX_PROPOSALS_PER_REVIEW:
            return self._reject_all(proposal_tuple, "too_many_operations")
        if not self.cfg.enabled or (
            self.store is not None and not self.store.is_scope_enabled(scope)
        ):
            return self._reject_all(proposal_tuple, "scope_disabled")
        if any(source.scope != scope for source in source_tuple):
            return self._reject_all(proposal_tuple, "cross_scope_source")

        normalized_actor = _normalize_actor(actor)
        accepted: list[MemoryProposal] = []
        rejected: list[RejectedProposal] = []
        staged_items: dict[str, MemoryItem | None] = {}
        for index, proposal in enumerate(proposal_tuple):
            normalized, reason = self._validate_one(
                scope, source_tuple, proposal, normalized_actor, staged_items
            )
            if reason is not None:
                rejected.append(RejectedProposal(proposal, reason, index))
            else:
                assert normalized is not None
                accepted.append(normalized)
                self._stage_operation(scope, normalized, staged_items)
        return ValidationResult(tuple(accepted), tuple(rejected))

    @staticmethod
    def _reject_all(
        proposals: tuple[MemoryProposal, ...], reason: str
    ) -> ValidationResult:
        return ValidationResult(
            (), tuple(RejectedProposal(proposal, reason, index) for index, proposal in enumerate(proposals))
        )

    def _validate_one(
        self,
        scope: MemoryScope,
        sources: tuple[MemorySource, ...],
        proposal: MemoryProposal,
        actor: MemoryActor | None,
        staged_items: Mapping[str, MemoryItem | None],
    ) -> tuple[MemoryProposal | None, str | None]:
        if proposal.operation not in ALLOWED_OPERATIONS:
            return None, "invalid_operation"
        if proposal.category is not None and proposal.category not in ALLOWED_CATEGORIES:
            return None, "invalid_category"
        if proposal.status is not None and proposal.status not in ALLOWED_STATUSES:
            return None, "invalid_status"
        if (
            proposal.sensitivity is not None
            and proposal.sensitivity not in ALLOWED_SENSITIVITIES
        ):
            return None, "invalid_sensitivity"
        if proposal.source_kind not in ALLOWED_SOURCE_KINDS:
            return None, "invalid_source_kind"
        if proposal.confidence is not None and (
            isinstance(proposal.confidence, bool)
            or not isinstance(proposal.confidence, (int, float))
            or not 0.0 <= float(proposal.confidence) <= 1.0
        ):
            return None, "invalid_confidence"
        if proposal.content is not None:
            content = _normalize_text(proposal.content)
            if not content:
                return None, "empty_content"
            if len(content) > MAX_MEMORY_CONTENT_CHARS:
                return None, "content_too_long"
            if proposal.sensitivity == "secret" or _contains_secret(content):
                return None, "secret_content"
            proposal = replace(proposal, content=content)

        reason = self._validate_operation_shape(proposal, actor)
        if reason is not None:
            return None, reason

        target = self._target(scope, proposal, staged_items)
        if proposal.operation in STATEFUL_OPERATIONS:
            if self.store is None:
                return None, "target_resolver_required"
            if target is None:
                return None, "target_not_found"
            reason = self._validate_target_transition(target, proposal)
            if reason is not None:
                return None, reason
            if self._target_metadata_mismatch(proposal, target):
                return None, "target_metadata_mismatch"
            proposal = self._with_target_metadata(proposal, target)
            if proposal.operation == "merge" and not self._valid_related_targets(
                scope, proposal, target, staged_items
            ):
                return None, "invalid_related_target"
        elif proposal.sensitivity is None:
            proposal = replace(proposal, sensitivity="normal")

        if proposal.sensitivity == "secret":
            return None, "secret_content"

        reason = self._validate_subject(scope, sources, proposal, actor)
        if reason is not None:
            return None, reason

        explicit_evidence = any(
            source.sender_id == proposal.subject_id and source.explicit
            for source in sources
        )
        if proposal.explicit_memory and not explicit_evidence:
            return None, "explicit_consent_required"
        if proposal.decay_exempt:
            allowed_exemption = bool(
                explicit_evidence
                and proposal.subject_kind == "user"
                and proposal.category == "identity"
            ) or bool(
                proposal.subject_kind == "group"
                and proposal.category == "group_norm"
                and actor is not None
                and actor.role == "group_owner"
            )
            if not allowed_exemption:
                return None, "decay_exempt_not_allowed"

        if proposal.sensitivity == "sensitive" and proposal.subject_kind == "user":
            if not explicit_evidence or (
                actor is not None and actor.id != proposal.subject_id
            ):
                return None, "sensitivity_consent_required"
        duplicate = None
        if proposal.operation == "add":
            duplicate, sensitivity_collision = self._duplicate(
                scope, proposal, staged_items
            )
            if sensitivity_collision:
                return None, "sensitivity_collision"
        proposal = self._candidate_if_ambiguous(proposal)
        if proposal.operation == "add" and duplicate is not None:
            proposal = MemoryProposal.reinforce(
                duplicate.id,
                confidence=proposal.confidence,
                source_kind=proposal.source_kind,
                actor_class=proposal.actor_class,
            )
        return proposal, None

    @staticmethod
    def _validate_operation_shape(
        proposal: MemoryProposal, actor: MemoryActor | None
    ) -> str | None:
        operation = proposal.operation
        if operation in {"add", "mark_candidate"}:
            if not proposal.subject_kind or not proposal.subject_id or not proposal.content:
                return "missing_required_field"
            if proposal.status not in {None, "active", "candidate"}:
                return "invalid_state_transition"
        elif operation == "revise":
            if not proposal.item_id or not proposal.content:
                return "missing_required_field"
            if proposal.status in {"contradicted", "rejected"}:
                return "invalid_state_transition"
        elif operation == "reinforce":
            if not proposal.item_id:
                return "missing_required_field"
            if proposal.content is not None or proposal.status is not None:
                return "invalid_operation_fields"
        elif operation == "contradict":
            if not proposal.item_id or not proposal.content:
                return "missing_required_field"
            if proposal.status is not None:
                return "invalid_state_transition"
        elif operation == "merge":
            if not proposal.item_id or not proposal.related_item_ids:
                return "missing_required_field"
            if proposal.item_id in proposal.related_item_ids:
                return "invalid_merge"
        elif operation == "forget":
            if not proposal.item_id:
                return "missing_required_field"
            if actor is None and proposal.expires_at is None and not proposal.related_item_ids:
                return "actor_not_authorized"
        return None

    def _target(
        self,
        scope: MemoryScope,
        proposal: MemoryProposal,
        staged_items: Mapping[str, MemoryItem | None],
    ) -> MemoryItem | None:
        if proposal.item_id is None:
            return None
        if proposal.item_id in staged_items:
            return staged_items[proposal.item_id]
        if self.store is None:
            return None
        return self.store.get_item(scope, proposal.item_id)

    def _duplicate(
        self,
        scope: MemoryScope,
        proposal: MemoryProposal,
        staged_items: Mapping[str, MemoryItem | None],
    ) -> tuple[MemoryItem | None, bool]:
        if (
            self.store is None
            or proposal.subject_kind is None
            or proposal.subject_id is None
            or proposal.content is None
        ):
            return None, False
        proposal_key = memory_identity_key(
            subject_kind=proposal.subject_kind,
            subject_id=proposal.subject_id,
            category=proposal.category,
            content=proposal.content,
            sensitivity=proposal.sensitivity,
        )
        duplicate: MemoryItem | None = None
        for item in self.store.list_items(
            scope,
            subject_kind=proposal.subject_kind,
            subject_id=proposal.subject_id,
            statuses=("active", "candidate", "dormant"),
            include_expired=True,
        ):
            staged_item = staged_items.get(item.id, item)
            if staged_item is None or staged_item.status not in {
                "active",
                "candidate",
                "dormant",
            }:
                continue
            item_key = memory_identity_key(
                subject_kind=staged_item.subject_kind,
                subject_id=staged_item.subject_id,
                category=staged_item.category,
                content=staged_item.content,
                sensitivity=staged_item.sensitivity,
            )
            if item_key[:-1] != proposal_key[:-1]:
                continue
            if item_key[-1] != proposal_key[-1]:
                return None, True
            duplicate = staged_item
        return duplicate, False

    def _valid_related_targets(
        self,
        scope: MemoryScope,
        proposal: MemoryProposal,
        target: MemoryItem,
        staged_items: Mapping[str, MemoryItem | None],
    ) -> bool:
        assert self.store is not None
        for related_id in proposal.related_item_ids:
            related = (
                staged_items[related_id]
                if related_id in staged_items
                else self.store.get_item(scope, related_id)
            )
            if related is None or related.id == target.id:
                return False
            if (
                related.subject_kind != target.subject_kind
                or related.subject_id != target.subject_id
                or related.category != target.category
                or related.status in {"contradicted", "rejected"}
            ):
                return False
        return True

    def _stage_operation(
        self,
        scope: MemoryScope,
        proposal: MemoryProposal,
        staged_items: dict[str, MemoryItem | None],
    ) -> None:
        if proposal.operation not in STATEFUL_OPERATIONS or proposal.item_id is None:
            return
        target = self._target(scope, proposal, staged_items)
        if target is None:
            return
        if proposal.operation == "forget":
            staged_items[target.id] = None
        elif proposal.operation == "merge":
            for related_id in proposal.related_item_ids:
                staged_items[related_id] = None
        elif proposal.operation == "contradict":
            staged_items[target.id] = replace(target, status="contradicted")
        elif proposal.operation == "revise":
            staged_items[target.id] = replace(
                target,
                content=proposal.content or target.content,
                status=proposal.status or target.status,
            )
        elif proposal.operation == "reinforce" and target.status in {
            "candidate",
            "dormant",
        }:
            staged_items[target.id] = replace(target, status="active")

    @staticmethod
    def _validate_target_transition(
        target: MemoryItem, proposal: MemoryProposal
    ) -> str | None:
        if target.status in {"contradicted", "rejected"}:
            return "invalid_state_transition"
        if proposal.operation == "merge" and target.status not in {
            "active",
            "candidate",
            "dormant",
        }:
            return "invalid_state_transition"
        return None

    @staticmethod
    def _target_metadata_mismatch(
        proposal: MemoryProposal, target: MemoryItem
    ) -> bool:
        return any(
            getattr(proposal, field) is not None
            and getattr(proposal, field) != getattr(target, field)
            for field in TARGET_METADATA_FIELDS
        )

    @staticmethod
    def _with_target_metadata(
        proposal: MemoryProposal, target: MemoryItem
    ) -> MemoryProposal:
        return replace(
            proposal,
            subject_kind=target.subject_kind,
            subject_id=target.subject_id,
            category=target.category,
            sensitivity=target.sensitivity,
        )

    @staticmethod
    def _validate_subject(
        scope: MemoryScope,
        sources: tuple[MemorySource, ...],
        proposal: MemoryProposal,
        actor: MemoryActor | None,
    ) -> str | None:
        subject_kind = proposal.subject_kind
        subject_id = str(proposal.subject_id or "")
        if subject_kind not in {"group", "user"} or not subject_id:
            return "invalid_subject"

        if subject_kind == "group":
            if scope.kind != "group" or subject_id != scope.id:
                return "invalid_subject"
            if proposal.category not in {"group_norm", "recurring_topic"}:
                return "invalid_subject_category"
            if actor is not None and actor.role == "member":
                return "actor_not_authorized"
            if not any(
                source.scope == scope
                and bool(str(source.sender_id).strip())
                and bool(_normalize_text(source.text))
                for source in sources
            ):
                return "source_evidence_required"
            return None

        if scope.kind == "private":
            if subject_id != scope.id:
                return "invalid_subject"
            if actor is not None and actor.id != scope.id:
                return "actor_not_authorized"
        elif actor is not None:
            if actor.role in {"private_user", "subject"}:
                return "actor_not_authorized"
            if actor.role == "member" and actor.id != subject_id:
                return "actor_not_authorized"
            if actor.role == "group_owner" and actor.id != subject_id:
                if proposal.source_kind != "owner_confirmed":
                    return "actor_not_authorized"
            if actor.role not in {"member", "group_owner", "private_user", "subject"}:
                return "actor_not_authorized"

        authored = tuple(source for source in sources if source.sender_id == subject_id)
        if proposal.source_kind == "owner_confirmed":
            if scope.kind != "group" or actor is None or actor.role != "group_owner":
                return "actor_not_authorized"
            return None
        if not authored:
            return "third_party_personal_claim"
        if proposal.source_kind == "direct_interaction" and not any(
            source.direct_interaction for source in authored
        ):
            return "invalid_subject_provenance"
        if proposal.source_kind == "explicit_request" and not any(
            source.explicit for source in authored
        ):
            return "invalid_subject_provenance"
        return None

    @staticmethod
    def _candidate_if_ambiguous(proposal: MemoryProposal) -> MemoryProposal:
        confidence = 0.75 if proposal.confidence is None else float(proposal.confidence)
        if confidence >= ACTIVE_CONFIDENCE_THRESHOLD:
            return proposal
        if proposal.operation in {"add", "mark_candidate"}:
            return replace(proposal, operation="mark_candidate", status="candidate")
        if proposal.operation in {"revise", "contradict"}:
            return replace(
                proposal,
                operation="mark_candidate",
                item_id=None,
                related_item_ids=(proposal.item_id,) if proposal.item_id else (),
                status="candidate",
            )
        return proposal


def _parse_proposal(value: object) -> MemoryProposal:
    if not isinstance(value, dict):
        raise ValueError("each curator operation must be an object")
    unknown = set(value) - _PROPOSAL_FIELDS
    if unknown:
        raise ValueError("curator operation contains unknown fields")
    operation = value.get("operation")
    if not isinstance(operation, str) or operation not in ALLOWED_OPERATIONS:
        raise ValueError("curator operation is invalid")
    for field in _STRING_FIELDS:
        if field in value and value[field] is not None and not isinstance(value[field], str):
            raise ValueError(f"curator field {field} must be a string")
    for field in ("explicit_memory", "decay_exempt"):
        if field in value and not isinstance(value[field], bool):
            raise ValueError(f"curator field {field} must be a boolean")
    if "confidence" in value and (
        isinstance(value["confidence"], bool)
        or not isinstance(value["confidence"], (int, float))
    ):
        raise ValueError("curator confidence must be a number")
    if "expires_at" in value and value["expires_at"] is not None and (
        isinstance(value["expires_at"], bool) or not isinstance(value["expires_at"], int)
    ):
        raise ValueError("curator expires_at must be an integer or null")
    related = value.get("related_item_ids", ())
    if not isinstance(related, (list, tuple)) or any(
        not isinstance(item, str) for item in related
    ):
        raise ValueError("curator related_item_ids must be a string list")
    if value.get("category") is not None and value["category"] not in ALLOWED_CATEGORIES:
        raise ValueError("curator category is invalid")
    if value.get("status") is not None and value["status"] not in ALLOWED_STATUSES:
        raise ValueError("curator status is invalid")
    if value.get("sensitivity", "normal") not in ALLOWED_SENSITIVITIES:
        raise ValueError("curator sensitivity is invalid")
    if value.get("source_kind", "inferred") not in ALLOWED_SOURCE_KINDS:
        raise ValueError("curator source kind is invalid")

    return MemoryProposal(
        operation=operation,
        item_id=value.get("item_id"),
        related_item_ids=tuple(related),
        subject_kind=value.get("subject_kind"),
        subject_id=value.get("subject_id"),
        category=value.get("category"),
        content=value.get("content"),
        confidence=value.get("confidence"),
        status=value.get("status"),
        sensitivity=value.get("sensitivity"),
        source_kind=value.get("source_kind", "inferred"),
        explicit_memory=value.get("explicit_memory", False),
        decay_exempt=value.get("decay_exempt", False),
        expires_at=value.get("expires_at"),
    )


def _normalize_actor(actor: object | None) -> MemoryActor | None:
    if actor is None:
        return None
    if isinstance(actor, MemoryActor):
        return MemoryActor(str(actor.id), _normalize_role(actor.role))
    if isinstance(actor, Mapping):
        actor_id = actor.get("id", actor.get("actor_id", actor.get("sender_id", "")))
        role = actor.get("role", actor.get("actor_class", "member"))
        return MemoryActor(str(actor_id or ""), _normalize_role(str(role)))
    if isinstance(actor, str):
        if actor in {"member", "group_owner", "owner", "private_user", "subject"}:
            return MemoryActor("", _normalize_role(actor))
        return MemoryActor(actor, "member")
    actor_id = getattr(actor, "id", getattr(actor, "actor_id", getattr(actor, "sender_id", "")))
    role = getattr(actor, "role", getattr(actor, "actor_class", "member"))
    return MemoryActor(str(actor_id or ""), _normalize_role(str(role)))


def _normalize_role(role: str) -> str:
    normalized = role.strip().lower()
    return {
        "owner": "group_owner",
        "group_member": "member",
        "user": "member",
    }.get(normalized, normalized)


def _memory_config(cfg: BridgeConfig | LongTermMemoryConfig) -> LongTermMemoryConfig:
    value = getattr(cfg, "long_term_memory", cfg)
    if not isinstance(value, LongTermMemoryConfig):
        raise TypeError("cfg must provide LongTermMemoryConfig")
    return value


def _normalize_text(text: object) -> str:
    return " ".join(str(text or "").split())


def _contains_secret(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _SECRET_PATTERNS)


__all__ = [
    "ACTIVE_CONFIDENCE_THRESHOLD",
    "MAX_MEMORY_CONTENT_CHARS",
    "MAX_PROPOSALS_PER_REVIEW",
    "MAX_SOURCE_TEXT_CHARS",
    "MemoryActor",
    "MemoryCollector",
    "MemoryValidator",
    "RejectedProposal",
    "ValidationResult",
    "parse_curator_output",
]
