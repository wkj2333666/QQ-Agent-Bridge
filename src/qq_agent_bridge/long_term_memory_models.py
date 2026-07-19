"""Domain types and constraints for scoped long-term memory."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ScopeKind = Literal["group", "private"]
MemoryStatusName = Literal[
    "candidate", "active", "dormant", "contradicted", "rejected"
]

ALLOWED_CATEGORIES = frozenset(
    {
        "preference",
        "identity",
        "project",
        "relationship",
        "group_norm",
        "recurring_topic",
    }
)
ALLOWED_STATUSES = frozenset(
    {"candidate", "active", "dormant", "contradicted", "rejected"}
)
ALLOWED_OPERATIONS = frozenset(
    {"add", "revise", "reinforce", "contradict", "merge", "mark_candidate", "forget"}
)
INDEXED_STATUSES = frozenset({"active", "dormant"})


@dataclass(frozen=True)
class MemoryScope:
    kind: ScopeKind
    id: str

    def __post_init__(self) -> None:
        if self.kind not in {"group", "private"}:
            raise ValueError("memory scope kind must be group or private")
        normalized_id = str(self.id).strip()
        if not normalized_id:
            raise ValueError("memory scope id must not be empty")
        object.__setattr__(self, "id", normalized_id)


@dataclass(frozen=True)
class MemorySource:
    scope: MemoryScope
    message_id: str
    sender_id: str
    text: str
    message_timestamp: int
    id: int | None = None
    mentioned_ids: tuple[str, ...] = ()
    quoted_sender_id: str | None = None
    is_reply: bool = False
    direct_interaction: bool = False
    command_class: str | None = None
    collection_reason: str = "ordinary_message"
    explicit: bool = False
    review_state: str = "pending"
    attempt_count: int = 0
    next_attempt_at: int = 0
    created_at: int | None = None


@dataclass(frozen=True)
class MemoryItem:
    id: str
    short_id: str
    scope: MemoryScope
    subject_kind: str
    subject_id: str
    category: str
    content: str
    base_confidence: float
    effective_score: float
    status: MemoryStatusName
    sensitivity: str
    source_kind: str
    source_count: int
    explicit_memory: bool
    decay_exempt: bool
    created_at: int
    updated_at: int
    last_supported_at: int
    expires_at: int | None
    dormant_at: int | None
    version: int


@dataclass(frozen=True)
class MemoryProposal:
    operation: str
    item_id: str | None = None
    related_item_ids: tuple[str, ...] = ()
    subject_kind: str | None = None
    subject_id: str | None = None
    category: str | None = None
    content: str | None = None
    confidence: float | None = None
    status: str | None = None
    sensitivity: str = "normal"
    source_kind: str = "inferred"
    explicit_memory: bool = False
    decay_exempt: bool = False
    expires_at: int | None = None
    created_at: int | None = None
    actor_class: str = "curator"

    @classmethod
    def add(
        cls,
        *,
        subject_kind: str,
        subject_id: str,
        category: str = "preference",
        content: str,
        confidence: float = 0.75,
        status: str = "active",
        sensitivity: str = "normal",
        source_kind: str = "inferred",
        explicit_memory: bool = False,
        decay_exempt: bool = False,
        expires_at: int | None = None,
        created_at: int | None = None,
        actor_class: str = "curator",
    ) -> MemoryProposal:
        return cls(
            operation="add",
            subject_kind=subject_kind,
            subject_id=subject_id,
            category=category,
            content=content,
            confidence=confidence,
            status=status,
            sensitivity=sensitivity,
            source_kind=source_kind,
            explicit_memory=explicit_memory,
            decay_exempt=decay_exempt,
            expires_at=expires_at,
            created_at=created_at,
            actor_class=actor_class,
        )

    @classmethod
    def reinforce(
        cls,
        item_id: str,
        *,
        confidence: float | None = None,
        source_kind: str = "inferred",
        actor_class: str = "curator",
    ) -> MemoryProposal:
        return cls(
            operation="reinforce",
            item_id=item_id,
            confidence=confidence,
            source_kind=source_kind,
            actor_class=actor_class,
        )


@dataclass(frozen=True)
class MemoryStoreStatus:
    enabled: bool
    pending_count: int
    active_count: int
    candidate_count: int
    last_review_at: int | None


__all__ = [
    "MemoryItem",
    "MemoryProposal",
    "MemoryScope",
    "MemorySource",
    "MemoryStatusName",
    "MemoryStoreStatus",
    "ScopeKind",
]
