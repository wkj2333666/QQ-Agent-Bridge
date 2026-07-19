"""Eligibility, parsing, and deterministic validation for memory curation."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
import unicodedata
from typing import Mapping, Sequence

from .config import BridgeConfig, LongTermMemoryConfig
from .long_term_memory import LongTermMemoryStore
from .long_term_memory_models import (
    ACTIVE_CONFIDENCE_THRESHOLD,
    ALLOWED_CATEGORIES,
    ALLOWED_OPERATIONS,
    ALLOWED_STATUSES,
    MemoryItem,
    MemoryProposal,
    MemoryScope,
    MemorySource,
    exact_memory_scope,
    memory_identity_key,
)
from .types import ChatEvent, trusted_reply_sender_id


MAX_SOURCE_TEXT_CHARS = 2_000
MAX_MEMORY_CONTENT_CHARS = 500
MAX_PROPOSALS_PER_REVIEW = 20
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
CONTENT_OPERATIONS = frozenset({"add", "mark_candidate", "revise", "contradict"})
TARGET_METADATA_FIELDS = ("subject_kind", "subject_id", "category", "sensitivity")

_SEMANTIC_COMMANDS = frozenset({"ask", "plan", "task"})
_SLASH_COMMAND_RE = re.compile(r"^\s*[/／]")
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
_SECRET_LABEL_SEPARATOR = r"[\s_-]*"
_ENGLISH_RECOVERY_LABEL = (
    rf"(?:recovery|backup|seed){_SECRET_LABEL_SEPARATOR}"
    rf"(?:phrase|words?|key|codes?)"
)
_ENGLISH_PASSPHRASE_LABEL = (
    rf"(?:(?:private|secret|key){_SECRET_LABEL_SEPARATOR})?"
    rf"pass{_SECRET_LABEL_SEPARATOR}phrase"
)
_ENGLISH_CREDENTIAL_LABEL = (
    rf"(?:(?:auth(?:entication)?|login|service){_SECRET_LABEL_SEPARATOR})?"
    rf"credentials?"
)
_ENGLISH_SECRET_LABEL = (
    rf"(?:(?:aws{_SECRET_LABEL_SEPARATOR})?access{_SECRET_LABEL_SEPARATOR}"
    rf"key{_SECRET_LABEL_SEPARATOR}id|"
    rf"(?:api|oauth2?|session|client){_SECRET_LABEL_SEPARATOR}"
    rf"(?:(?:access|refresh){_SECRET_LABEL_SEPARATOR})?(?:token|key|secret)|"
    rf"(?:access|refresh|auth){_SECRET_LABEL_SEPARATOR}(?:token|key)|"
    rf"bearer(?:{_SECRET_LABEL_SEPARATOR}token)?|"
    rf"private{_SECRET_LABEL_SEPARATOR}key|"
    rf"secret{_SECRET_LABEL_SEPARATOR}key|"
    rf"{_ENGLISH_RECOVERY_LABEL}|"
    rf"mnemonic(?:{_SECRET_LABEL_SEPARATOR}(?:phrase|words?))?|"
    rf"{_ENGLISH_PASSPHRASE_LABEL}|token|"
    rf"{_ENGLISH_CREDENTIAL_LABEL}|"
    r"password|passwd|secret|cookie)"
)
_SECRET_ASSIGNMENT = (
    r"(?:(?:is|are|equals|changed?\s+to|set\s+to)\b|"
    r"(?:es|son|igual\s+a|cambi[oó]\s+a)\b|"
    r"(?:est|sont)\b|(?:ist|sind)\b|(?:é|são)\b|"
    r"(?:это|равен)\b|"
    r"(?:就是|是|为|等于|改成(?:了)?|改为|变成|设为|更新为)|"
    r"(?:は|です|に変更(?:した)?|은|는|입니다)|[=:：])"
)
_CHINESE_SECRET_LABEL = (
    r"(?:(?:接口|会话|客户端|访问|刷新|授权)?(?:令牌|密钥)|"
    r"私钥|密码|口令|助记(?:词|短语)|(?:恢复|备份|种子)(?:短语|密钥|代码|码)|"
    r"(?:登录|认证|身份)?(?:凭据|凭证)|认证信息)"
)
_MULTILINGUAL_SECRET_LABEL = (
    r"(?:contraseña|clave(?:\s+(?:de\s+acceso|secreta))?|credenciales|"
    r"mot\s+de\s+passe|clé\s+api|jeton\s+d['’]accès|identifiants|"
    r"passwort|api[-\s_]*schlüssel|zugangstoken|anmeldedaten|"
    r"senha|chave\s+api|token\s+de\s+acesso|credenciais|"
    r"пароль|ключ\s+api|токен\s+доступа|учетные\s+данные|"
    r"パスワード|暗証番号|秘密鍵|api\s*キー|アクセストークン|認証情報|"
    r"비밀번호|암호|api\s*키|액세스\s*토큰|인증\s*정보)"
)
_ANY_SECRET_LABEL = (
    rf"(?:{_ENGLISH_SECRET_LABEL}|{_CHINESE_SECRET_LABEL}|"
    rf"{_MULTILINGUAL_SECRET_LABEL})"
)
_SECRET_LIKE_VALUE = (
    r"(?=[^\s]{8,})(?=[^\s]*[0-9_./+@:-])"
    r"[A-Za-z0-9_.$~+/@:-]{8,}"
)
_ENV_SECRET_SUFFIX = (
    r"(?:(?:aws_)?access_key_id|"
    r"(?:api|oauth2?|session|client)(?:_(?:access|refresh))?_(?:token|key|secret)|"
    r"secret_access_key|(?:access|refresh|auth)_(?:token|key)|bearer(?:_token)?|"
    r"private_key|secret_key|password|passwd|cookie|token|secret|"
    r"credentials?|"
    r"(?:recovery|backup|seed)_(?:phrase|words?|key|codes?)|"
    r"mnemonic(?:_(?:phrase|words?))?|"
    r"(?:(?:private|secret|key)_)?pass(?:_?phrase))"
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk|ghp|gho|github_pat)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/-]{8,}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<![A-Za-z0-9_]){_ENGLISH_SECRET_LABEL}(?![A-Za-z0-9_])"
        rf"\s*{_SECRET_ASSIGNMENT}\s*\S",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_CHINESE_SECRET_LABEL}\s*{_SECRET_ASSIGNMENT}\s*\S",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_MULTILINGUAL_SECRET_LABEL}\s*{_SECRET_ASSIGNMENT}\s*\S",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_ANY_SECRET_LABEL}[^\n]{{0,48}}?{_SECRET_LIKE_VALUE}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<![A-Za-z0-9_])_*(?:[A-Za-z0-9]+_)*{_ENV_SECRET_SUFFIX}"
        rf"\s*{_SECRET_ASSIGNMENT}\s*\S",
        re.IGNORECASE,
    ),
)

_PHONE_SEPARATOR_CHARS = (
    "-. \t\u00a0\u202f\u3000"
    "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d\uff0e"
)
_PHONE_SEPARATOR_CLASS = f"[{re.escape(_PHONE_SEPARATOR_CHARS)}]"
_FORMATTED_PHONE_CANDIDATE_RE = re.compile(
    rf"(?<!\d)(?:[+＋]86{_PHONE_SEPARATOR_CLASS}{{0,3}})?"
    rf"(?:1[3-9][0-9]|[(（]1[3-9][0-9][)）])"
    rf"(?:{_PHONE_SEPARATOR_CLASS}{{0,3}}[0-9]){{8}}(?!\d)"
)
_PHONE_FORMAT_TRANSLATION = str.maketrans(
    {
        **{character: None for character in _PHONE_SEPARATOR_CHARS},
        "(": None,
        ")": None,
        "（": None,
        "）": None,
        "＋": "+",
    }
)
_FORMATTED_MAINLAND_ID_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Za-z])[1-9](?:[- \t]?\d){16}[- \t]?[0-9Xx]"
    r"(?![0-9A-Za-z])"
)
_FORMATTED_FINANCIAL_ID_CANDIDATE_RE = re.compile(
    r"(?<!\d)\d(?:[- \t]?\d){15,18}(?!\d)"
)
_MAINLAND_ID_RE = re.compile(
    r"[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]"
)

_SENSITIVE_PATTERNS = (
    # Health and medical status.
    re.compile(
        r"(?:确诊|诊断|病史|患有|用药|服药|怀孕|孕期|抑郁症?|焦虑症?|"
        r"双相|癌症|糖尿病|艾滋|HIV|血压|过敏|哮喘|心脏病|癫痫|慢性病)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:medical\s+(?:diagnosis|condition|history)|diagnosed\s+with|"
        r"pregnan(?:t|cy)|depression|anxiety|bipolar|cancer|diabetes|hiv|"
        r"blood\s+pressure|allerg(?:y|ic)|medication|asthma|epilepsy|"
        r"heart\s+disease|chronic\s+(?:illness|disease))\b",
        re.IGNORECASE,
    ),
    # Precise location and contact details.
    re.compile(
        r"(?:家庭住址|详细地址|现住址|住址|住在.{0,30}(?:区|县).{0,30}"
        r"(?:路|街|巷|号|小区)|经纬度|坐标)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:home|street|residential|mailing)\s+address\b|"
        r"\b(?:address\s+is|live\s+at)\s+\d+\b",
        re.IGNORECASE,
    ),
    re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE),
    re.compile(r"(?:手机号|手机号码|联系电话|电话号码|邮箱|电子邮箱|微信号|QQ号)"),
    re.compile(
        r"(?:我的?微信(?:号|ID)?(?:是|为)?|加我微信|我的?联系方式(?:是|为)?)"
        r"\s*[：:=]?\s*[A-Za-z][A-Za-z0-9_.-]{3,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:我\s*家\s*(?:住|在)|家庭\s*(?:住址|地址)|"
        r"收件\s*地址|邮寄\s*地址|通信\s*地址)"
        r"\s*[：:=＝,，]?\s*[^\n]{0,80}"
        r"(?:省|市)[^\n]{0,40}(?:区|县)[^\n]{0,40}"
        r"(?:路|街|大道|大街|巷)[^\n]{0,20}\d+\s*号",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:phone|mobile|telephone|email|wechat|qq)\s*"
        r"(?:number|address|id|account)?\s*(?:is|=|:)\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    # Legal identity and financial information.
    re.compile(
        r"(?:身份证(?:号|号码)?|护照(?:号|号码)?|真实姓名|法定姓名|全名|社保号)"
    ),
    re.compile(
        r"(?:我\s*叫|我\s*的?\s*名字\s*(?:是|叫|为)|"
        r"(?:我\s*的?\s*)?姓\s*名\s*(?:(?:是|叫|为)|[：:=＝]))"
        r"[\s:：=＝,，;；-]*(?:[\u3400-\u9fff\u00b7]\s*){2,12}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:legal\s+name|full\s+name|passport\s+(?:number|no)|"
        r"social\s+security\s+number|ssn)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![0-9A-Za-z])[1-9]\d{5}(?:18|19|20)\d{2}"
        r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]"
        r"(?![0-9A-Za-z])"
    ),
    re.compile(r"(?:银行卡(?:号|号码)?|银行账户|信用卡(?:号|号码)?|工资|收入|债务|负债|资产)"),
    re.compile(
        r"\b(?:bank\s+account|bank\s+card|credit\s+card|annual\s+salary|"
        r"salary|income|debt|net\s+worth)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    # Intimate relationships, politics, and religion.
    re.compile(r"(?:伴侣|配偶|男朋友|女朋友|婚姻|已婚|离婚|性取向|性生活)"),
    re.compile(
        r"\b(?:spouse|partner|boyfriend|girlfriend|married|divorced|"
        r"bisexual|homosexual|heterosexual|sexual\s+orientation)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:政治立场|政治观点|党派|党员|投票倾向|宗教|信仰|佛教|基督教|伊斯兰教)"),
    re.compile(
        r"\b(?:political\s+(?:affiliation|view|belief)|party\s+membership|"
        r"voting\s+preference|religion|religious\s+belief|faith|buddhis[mt]|"
        r"christian(?:ity)?|islam|muslim)\b",
        re.IGNORECASE,
    ),
)

_PROPOSAL_FIELDS = frozenset(
    {
        "operation",
        "source_ids",
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
        scope = exact_memory_scope(
            is_group=ev.is_group,
            chat_id=ev.chat_id,
            sender_id=ev.sender_id,
        )
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
        if command is None and _SLASH_COMMAND_RE.match(text):
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
        quoted_sender = trusted_reply_sender_id(ev.reply)
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
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateKeyError as exc:
        raise ValueError("curator output contains duplicate key") from exc
    except (TypeError, json.JSONDecodeError, _NonJsonConstantError) as exc:
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
        staged_content: list[MemoryProposal] = []
        for index, proposal in enumerate(proposal_tuple):
            normalized, reason = self._validate_one(
                scope,
                source_tuple,
                proposal,
                normalized_actor,
                staged_items,
                staged_content,
            )
            if reason is not None:
                rejected.append(RejectedProposal(proposal, reason, index))
            else:
                assert normalized is not None
                accepted.append(normalized)
                self._stage_operation(
                    scope, normalized, staged_items, staged_content
                )
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
        staged_content: Sequence[MemoryProposal],
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

        target: MemoryItem | None = None
        if proposal.operation in STATEFUL_OPERATIONS:
            if self.store is None:
                return None, "target_resolver_required"
            proposal, resolved_target, reason = self._resolve_operation_ids(
                scope, proposal
            )
            if reason is not None:
                return None, reason
            assert proposal is not None and resolved_target is not None
            target = (
                staged_items[resolved_target.id]
                if resolved_target.id in staged_items
                else resolved_target
            )
            if target is None:
                return None, "target_not_found"
            reason = self._validate_target_transition(target, proposal)
            if reason is not None:
                return None, reason
            if self._target_metadata_mismatch(proposal, target):
                return None, "target_metadata_mismatch"
            proposal = self._with_target_metadata(proposal, target)
            if proposal.operation in {"merge", "forget"} and proposal.related_item_ids:
                if not self._valid_related_targets(
                    scope, proposal, target, staged_items
                ):
                    return None, "invalid_related_target"
        elif proposal.sensitivity is None:
            proposal = replace(proposal, sensitivity="normal")

        if (
            proposal.content is not None
            and classify_memory_sensitivity(proposal.content) == "sensitive"
        ):
            proposal = replace(proposal, sensitivity="sensitive")

        if proposal.sensitivity == "secret":
            return None, "secret_content"

        if proposal.operation in {"forget", "merge"} and (
            actor is None or proposal.evidence_required
        ):
            return None, "actor_not_authorized"

        cited_sources, reason = self._cited_sources(sources, proposal)
        if reason is not None:
            return None, reason
        evidence_content = proposal.content or (target.content if target is not None else None)
        evidence_sources = sources if cited_sources is None else cited_sources
        if proposal.source_kind == "owner_confirmed" and evidence_content is not None:
            explicit_item_confirmation = bool(
                not proposal.evidence_required
                and cited_sources is None
                and proposal.actor_class == "user"
                and proposal.operation == "reinforce"
                and proposal.item_id
                and actor is not None
                and any(
                    source.sender_id == actor.id and source.explicit
                    for source in evidence_sources
                )
            )
            owner_supports_content = actor is not None and any(
                source.sender_id == actor.id
                and _content_affirmatively_supported_by_source(
                    evidence_content, source.text
                )
                and (
                    not _curator_proposal_can_activate(proposal)
                    or _content_is_direct_assertion(evidence_content, source.text)
                )
                for source in evidence_sources
            )
            if not explicit_item_confirmation and not owner_supports_content:
                return None, "owner_confirmation_required"
        elif cited_sources is not None and evidence_content is not None:
            matching_sources = tuple(
                source
                for source in cited_sources
                if _content_supported_by_source(evidence_content, source.text)
            )
            if not matching_sources:
                return None, "source_content_mismatch"
            if not any(
                _content_affirmatively_supported_by_source(
                    evidence_content, source.text
                )
                for source in matching_sources
            ):
                return None, "source_evidence_disallowed"
            if _curator_proposal_can_activate(proposal) and not any(
                _content_is_direct_assertion(evidence_content, source.text)
                for source in matching_sources
            ):
                return None, "source_evidence_disallowed"
        reason = self._validate_subject(scope, evidence_sources, proposal, actor)
        if reason is not None:
            return None, reason

        explicit_evidence = any(
            source.sender_id == proposal.subject_id and source.explicit
            for source in evidence_sources
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
        proposal = self._candidate_if_ambiguous(proposal)
        duplicate = None
        if proposal.operation in CONTENT_OPERATIONS:
            duplicate, sensitivity_collision = self._duplicate(
                scope, proposal, staged_items, staged_content
            )
            if sensitivity_collision:
                return None, "sensitivity_collision"
        if (
            proposal.operation == "mark_candidate"
            and proposal.candidate_target_id is None
            and duplicate is not None
            and duplicate.status != "candidate"
        ):
            proposal = replace(proposal, candidate_target_id=duplicate.id)
            duplicate, sensitivity_collision = self._duplicate(
                scope, proposal, staged_items, staged_content
            )
            if sensitivity_collision:
                return None, "sensitivity_collision"
        if proposal.operation == "revise" and duplicate is not None:
            assert proposal.item_id is not None and target is not None
            if duplicate.id == proposal.item_id:
                proposal = MemoryProposal.reinforce(
                    proposal.item_id,
                    confidence=proposal.confidence,
                    source_kind=proposal.source_kind,
                    actor_class=proposal.actor_class,
                    source_ids=proposal.source_ids,
                    evidence_required=proposal.evidence_required,
                )
            else:
                if actor is None or proposal.evidence_required:
                    return None, "actor_not_authorized"
                proposal = MemoryProposal(
                    operation="merge",
                    item_id=duplicate.id,
                    related_item_ids=(proposal.item_id,),
                    confidence=(
                        proposal.confidence
                        if proposal.confidence is not None
                        else target.base_confidence
                    ),
                    source_kind=proposal.source_kind,
                    actor_class=proposal.actor_class,
                    source_ids=proposal.source_ids,
                    evidence_required=proposal.evidence_required,
                )
        if proposal.operation == "add" and duplicate is not None:
            proposal = MemoryProposal.reinforce(
                duplicate.id,
                confidence=proposal.confidence,
                source_kind=proposal.source_kind,
                actor_class=proposal.actor_class,
                source_ids=proposal.source_ids,
                evidence_required=proposal.evidence_required,
            )
        return proposal, None

    @staticmethod
    def _cited_sources(
        sources: tuple[MemorySource, ...], proposal: MemoryProposal
    ) -> tuple[tuple[MemorySource, ...] | None, str | None]:
        if not proposal.evidence_required and not proposal.source_ids:
            return None, None
        if not proposal.source_ids:
            return (), "source_evidence_required"
        by_id = {source.id: source for source in sources if source.id is not None}
        cited: list[MemorySource] = []
        for source_id in proposal.source_ids:
            source = by_id.get(source_id)
            if source is None:
                return (), "invalid_source_evidence"
            cited.append(source)
        return tuple(cited), None

    @staticmethod
    def _validate_operation_shape(
        proposal: MemoryProposal, actor: MemoryActor | None
    ) -> str | None:
        if proposal.candidate_target_id is not None:
            return "invalid_operation_fields"
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
        return None

    def _resolve_operation_ids(
        self,
        scope: MemoryScope,
        proposal: MemoryProposal,
    ) -> tuple[MemoryProposal | None, MemoryItem | None, str | None]:
        assert self.store is not None and proposal.item_id is not None
        target = self.store.get_item(scope, proposal.item_id)
        if target is None:
            return None, None, "target_not_found"

        related_ids = proposal.related_item_ids
        if proposal.operation in {"merge", "forget"} and related_ids:
            canonical_related: list[str] = []
            for related_id in related_ids:
                related = self.store.get_item(scope, related_id)
                if related is None:
                    return None, None, "invalid_related_target"
                canonical_related.append(related.id)
            if target.id in canonical_related or len(canonical_related) != len(
                set(canonical_related)
            ):
                return None, None, "invalid_related_target"
            related_ids = tuple(canonical_related)

        return (
            replace(
                proposal,
                item_id=target.id,
                related_item_ids=related_ids,
            ),
            target,
            None,
        )

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
        staged_content: Sequence[MemoryProposal],
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
            if staged_item.candidate_target_id != proposal.candidate_target_id:
                continue
            duplicate = staged_item
        for staged_proposal in staged_content:
            staged_key = memory_identity_key(
                subject_kind=staged_proposal.subject_kind,
                subject_id=staged_proposal.subject_id,
                category=staged_proposal.category,
                content=staged_proposal.content,
                sensitivity=staged_proposal.sensitivity,
            )
            if (
                staged_key[:-1] == proposal_key[:-1]
                and staged_key[-1] != proposal_key[-1]
            ):
                return None, True
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
        staged_content: list[MemoryProposal],
    ) -> None:
        if proposal.operation in {"add", "mark_candidate"}:
            staged_content.append(proposal)
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
            confidence = (
                target.base_confidence
                if proposal.confidence is None
                else float(proposal.confidence)
            )
            staged_items[target.id] = replace(
                target,
                base_confidence=max(target.base_confidence, confidence),
                effective_score=max(target.effective_score, confidence),
                status=(
                    "active"
                    if target.status in {"candidate", "dormant"}
                    else target.status
                ),
                source_kind=proposal.source_kind,
                source_count=target.source_count + len(proposal.related_item_ids),
                dormant_at=None,
            )
        elif proposal.operation == "contradict":
            staged_items[target.id] = replace(target, status="contradicted")
            staged_content.append(proposal)
        elif proposal.operation == "revise":
            staged_items[target.id] = replace(
                target,
                content=proposal.content or target.content,
                status=proposal.status or target.status,
                sensitivity=proposal.sensitivity or target.sensitivity,
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
        if any(
            getattr(proposal, field) is not None
            and getattr(proposal, field) != getattr(target, field)
            for field in TARGET_METADATA_FIELDS
            if field != "sensitivity"
        ):
            return True
        if proposal.sensitivity is None or proposal.sensitivity == target.sensitivity:
            return False
        return not (
            target.sensitivity == "normal"
            and proposal.sensitivity == "sensitive"
            and proposal.content is not None
            and classify_memory_sensitivity(proposal.content) == "sensitive"
        )

    @staticmethod
    def _with_target_metadata(
        proposal: MemoryProposal, target: MemoryItem
    ) -> MemoryProposal:
        sensitivity = (
            "sensitive"
            if "sensitive" in {target.sensitivity, proposal.sensitivity}
            else target.sensitivity
        )
        return replace(
            proposal,
            subject_kind=target.subject_kind,
            subject_id=target.subject_id,
            category=target.category,
            sensitivity=sensitivity,
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
            if not any(source.sender_id == actor.id for source in sources):
                return "owner_confirmation_required"
            if proposal.content is not None and not any(
                source.sender_id == actor.id
                and _content_affirmatively_supported_by_source(
                    proposal.content, source.text
                )
                for source in sources
            ):
                return "owner_confirmation_required"
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
                related_item_ids=(),
                candidate_target_id=proposal.item_id,
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
    source_ids = value.get("source_ids", ())
    if not isinstance(source_ids, (list, tuple)) or any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0
        for item in source_ids
    ):
        raise ValueError("curator source_ids must be a positive integer list")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("curator source_ids must be unique")
    _require_curator_operation_fields(value, operation, tuple(source_ids), tuple(related))
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
        source_ids=tuple(source_ids),
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
        evidence_required=True,
    )


def _require_curator_operation_fields(
    value: Mapping[str, object],
    operation: str,
    source_ids: tuple[int, ...],
    related_item_ids: tuple[str, ...],
) -> None:
    if not source_ids:
        raise ValueError("curator operation is missing required source_ids")
    required_strings = {
        "add": ("subject_kind", "subject_id", "content"),
        "mark_candidate": ("subject_kind", "subject_id", "content"),
        "revise": ("item_id", "content"),
        "reinforce": ("item_id",),
        "contradict": ("item_id", "content"),
        "merge": ("item_id",),
        "forget": ("item_id",),
    }[operation]
    if any(
        not isinstance(value.get(field), str) or not str(value[field]).strip()
        for field in required_strings
    ):
        raise ValueError(f"curator {operation} operation is missing required fields")
    if operation == "merge" and not related_item_ids:
        raise ValueError("curator merge operation is missing required related_item_ids")


class _DuplicateKeyError(ValueError):
    pass


class _NonJsonConstantError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise _NonJsonConstantError(value)


def _evidence_normal_form(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", _normalize_text(text)).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _content_supported_by_source(content: str, source_text: str) -> bool:
    proposed = _evidence_normal_form(content)
    source = _evidence_normal_form(source_text)
    return bool(proposed and proposed in source)


def _content_affirmatively_supported_by_source(content: str, source_text: str) -> bool:
    proposed = _evidence_normal_form(content)
    normalized_source, compact_source, positions = _evidence_source_map(source_text)
    if not proposed:
        return False
    offset = compact_source.find(proposed)
    while offset >= 0:
        start = positions[offset]
        end = positions[offset + len(proposed) - 1] + 1
        if not _evidence_occurrence_disallowed(normalized_source, start, end):
            return True
        offset = compact_source.find(proposed, offset + 1)
    return False


def _curator_proposal_can_activate(proposal: MemoryProposal) -> bool:
    if not proposal.evidence_required:
        return False
    if proposal.operation == "mark_candidate" or proposal.status == "candidate":
        return False
    if proposal.operation == "reinforce":
        return True
    confidence = 0.75 if proposal.confidence is None else float(proposal.confidence)
    return confidence >= ACTIVE_CONFIDENCE_THRESHOLD


def _content_is_direct_assertion(content: str, source_text: str) -> bool:
    proposed = _evidence_normal_form(content)
    normalized_source, compact_source, positions = _evidence_source_map(source_text)
    if not proposed:
        return False
    offset = compact_source.find(proposed)
    while offset >= 0:
        start = positions[offset]
        end = positions[offset + len(proposed) - 1] + 1
        if _trivial_assertion_wrappers(normalized_source[:start], normalized_source[end:]):
            return True
        offset = compact_source.find(proposed, offset + 1)
    return False


def _trivial_assertion_wrappers(prefix: str, suffix: str) -> bool:
    normalized_prefix = prefix.strip()
    normalized_prefix = re.sub(r"^(?:[-*•]\s*)", "", normalized_prefix)
    if normalized_prefix not in {
        "",
        "i",
        "my",
        "we",
        "our",
        "我",
        "我的",
        "我们",
        "我们的",
        "本人",
        "本群",
    }:
        return False
    return suffix.strip() in {"", ".", "。", "!", "！"}


def _evidence_source_map(text: str) -> tuple[str, str, tuple[int, ...]]:
    normalized = unicodedata.normalize("NFKC", _normalize_text(text)).casefold()
    compact: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(normalized):
        if character.isalnum():
            compact.append(character)
            positions.append(index)
    return normalized, "".join(compact), tuple(positions)


def _evidence_occurrence_disallowed(source: str, start: int, end: int) -> bool:
    for pattern in (
        r'"[^"\n]*"',
        r"'[^'\n]*'",
        r"“[^”\n]*”",
        r"‘[^’\n]*’",
        r"「[^」\n]*」",
        r"『[^』\n]*』",
    ):
        if any(
            match.start() < start and end < match.end()
            for match in re.finditer(pattern, source)
        ):
            return True

    window_start = max(0, start - 120)
    window_end = min(len(source), end + 120)
    window = source[window_start:window_end]
    prefix = source[max(0, start - 80):start]
    suffix = source[end:min(len(source), end + 80)]
    if re.search(
        r"\b(?:do\s+not|don['’]t|dont|never|must\s+not|should\s+not)\b"
        r".{0,80}\b(?:store|remember|save|retain|memorize|record)\b",
        window,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"\b(?:forget|delete|erase)\b.{0,80}$", prefix, re.IGNORECASE):
        return True
    if re.search(
        r"\b(?:it\s+is\s+)?(?:not\s+true|false|untrue|incorrect)(?:\s+that)?\s*$",
        prefix,
        re.IGNORECASE,
    ) or re.search(
        r"\b(?:it\s+)?(?:is|are|was|were)n['’]t\s+"
        r"(?:true|correct)(?:\s+that)?\s*$",
        prefix,
        re.IGNORECASE,
    ) or re.search(
        r"\b(?:i\s+)?(?:deny|denied|denies)(?:\s+that)?\s*$",
        prefix,
        re.IGNORECASE,
    ) or re.match(
        r"\s+(?:is\s+)?(?:not\s+true|false|untrue|incorrect)\b", suffix
    ):
        return True
    if re.search(
        r"(?:^|[.!?;:]\s*)"
        r"(?:for\s+(?:example|instance)|e\.\s*g\.|hypothetically|"
        r"suppose|supposing|assuming|imagine|if)\b[^.!?;]{0,80}$",
        prefix,
        re.IGNORECASE,
    ) or re.match(
        r"\s*(?:,\s*)?(?:if|unless)\b|"
        r"\s+(?:is|was|would\s+be)\s+(?:only\s+)?"
        r"(?:an?\s+)?(?:example|hypothetical)\b",
        suffix,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(?:say|repeat|output|print|write)(?:\s+(?:the\s+)?(?:phrase|sentence|words?))?\s*$",
        prefix,
        re.IGNORECASE,
    ):
        return True
    compact_window = re.sub(r"\s+", "", window)
    compact_prefix = re.sub(r"\s+", "", prefix)
    compact_suffix = re.sub(r"\s+", "", suffix)
    if re.search(r"(?:不要|别|勿|无需|禁止|不许)", compact_window) and re.search(
        r"(?:记住|记为|记录|存储|保存|保留)", compact_window
    ):
        return True
    if re.search(r"(?:忘记|忘掉|删除|清除)[^。！？\n]{0,80}$", prefix):
        return True
    if re.search(r"(?:并非|不是|不是真的|不属实|否认)\s*$", prefix) or re.match(
        r"\s*(?:并非事实|不是真的|不属实)", suffix
    ):
        return True
    if re.search(
        r"(?:例如|比如|举例(?:来说)?|假设|假如|如果|倘若)[：:,，]?$",
        compact_prefix,
    ) or re.match(
        r"[，,]?(?:如果|除非)|(?:只是|仅是)?(?:一个)?(?:例子|示例|假设)",
        compact_suffix,
    ):
        return True
    if re.search(r"(?:说|重复|输出|打印|写下)\s*$", prefix):
        return True
    return False


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
    normalized = _security_normal_form(text)
    return any(pattern.search(normalized) is not None for pattern in _SECRET_PATTERNS)


def classify_memory_sensitivity(text: str) -> str:
    """Conservatively classify personal content without delegating policy to a model."""
    normalized = _security_normal_form(text)
    if _contains_structured_sensitive_identifier(normalized) or any(
        pattern.search(normalized) is not None for pattern in _SENSITIVE_PATTERNS
    ):
        return "sensitive"
    return "normal"


def _security_normal_form(text: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cf", "Cc", "Cs"}
        and not unicodedata.category(character).startswith("M")
    )
    return _normalize_text(normalized).casefold()


def _contains_structured_sensitive_identifier(text: str) -> bool:
    for match in _FORMATTED_PHONE_CANDIDATE_RE.finditer(text):
        compact = match.group().translate(_PHONE_FORMAT_TRANSLATION)
        if compact.startswith("+86"):
            compact = compact[3:]
        if re.fullmatch(r"1[3-9][0-9]{9}", compact):
            return True
    for match in _FORMATTED_MAINLAND_ID_CANDIDATE_RE.finditer(text):
        compact = re.sub(r"[- \t]", "", match.group())
        if _MAINLAND_ID_RE.fullmatch(compact):
            return True
    for match in _FORMATTED_FINANCIAL_ID_CANDIDATE_RE.finditer(text):
        compact = re.sub(r"[- \t]", "", match.group())
        if re.fullmatch(r"\d{16,19}", compact):
            return True
    return False


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
    "classify_memory_sensitivity",
]
