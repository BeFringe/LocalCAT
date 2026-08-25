"""Frozen, body-safe contracts for collaborative chunk plans.

This module is a carrier-neutral leaf.  It depends only on the already
approved Project/Document/Segment identity contracts and never materializes
workspace text, package members, resources, or export payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
import secrets
import unicodedata

from project_workspace_contracts import SegmentIdentity, SourcePresence
from project_workspace_identity import (
    EMPTY_SHA256,
    MAX_LOCAL_SEGMENT_ID_BYTES,
    ProjectWorkspaceError,
    validate_project_id,
    validate_sha256,
)


CHUNK_METADATA_SCHEMA = "localcat-collaborative-chunk-metadata-v1"
CHUNK_METADATA_NAMESPACE = "localcat.collaboration.chunks.v1"
CHUNK_STORE_SCHEMA = "localcat-collaborative-chunk-store-v1"
CHUNK_LIFECYCLE_SCHEMA = "localcat-collaborative-chunk-lifecycle-v1"
CHUNK_AUDIT_RECORD_SCHEMA = "localcat-collaborative-chunk-audit-record-v1"
CHUNK_JOURNAL_SCHEMA = "localcat-collaborative-chunk-journal-v1"
CHUNK_REBASE_INTENT_SCHEMA = "localcat-collaborative-chunk-rebase-intent-v1"
CHUNK_LIMIT_PROFILE_ID = "localcat-collaborative-chunk-limits-v1"
EMPTY_CHUNK_AUDIT_DIGEST = EMPTY_SHA256
DISSOLVED_CHUNK_PLAN_DIGEST = hashlib.sha256(
    b"localcat.chunk.plan.dissolved.v1\0"
).hexdigest()

MAX_ACTIVE_CHUNKS = 4_096
MAX_ACTIVE_MEMBERS = 100_000
MAX_MEMBERS_PER_CHUNK = 100_000
MAX_CHUNK_NAME_SCALARS = 256
MAX_CHUNK_NAME_BYTES = 1_024
MAX_ACTOR_REF_BYTES = 256
MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_REBASE_INTENT_BYTES = 512 * 1024 * 1024
MAX_JSON_NESTING_DEPTH = 32
MAX_RETAINED_SAFE_ISSUES = 256
MAX_AUDIT_RECORDS = 100_000
MAX_PUBLIC_AFFECTED_IDS = 100_000

# The rebase sidecar contains at most two complete workspace universes.  JSON
# may double every byte of a valid local id when it consists only of quote or
# backslash characters; source-changed members are encoded as current-universe
# indices and therefore do not create a third ID copy.  The fixed allowance
# covers document IDs, presence values, punctuation, bindings and digests.
DERIVED_MAX_REBASE_INTENT_BYTES_V1 = (
    2
    * MAX_ACTIVE_MEMBERS
    * (68 + 2 * MAX_LOCAL_SEGMENT_ID_BYTES + 64)
    + MAX_ACTIVE_MEMBERS * 8
    + 64 * 1024
)
if DERIVED_MAX_REBASE_INTENT_BYTES_V1 > MAX_REBASE_INTENT_BYTES:
    raise RuntimeError("Chunk rebase-intent limit no longer covers v1 identities")


@dataclass(frozen=True, slots=True)
class ChunkLimitProfile:
    profile_id: str
    max_active_chunks: int
    max_active_members: int
    max_members_per_chunk: int
    max_chunk_name_scalars: int
    max_chunk_name_bytes: int
    max_actor_ref_bytes: int
    max_metadata_bytes: int
    max_rebase_intent_bytes: int
    max_json_nesting_depth: int
    max_retained_safe_issues: int
    max_audit_records: int
    max_public_affected_ids: int


CHUNK_LIMIT_PROFILE_V1 = ChunkLimitProfile(
    profile_id=CHUNK_LIMIT_PROFILE_ID,
    max_active_chunks=MAX_ACTIVE_CHUNKS,
    max_active_members=MAX_ACTIVE_MEMBERS,
    max_members_per_chunk=MAX_MEMBERS_PER_CHUNK,
    max_chunk_name_scalars=MAX_CHUNK_NAME_SCALARS,
    max_chunk_name_bytes=MAX_CHUNK_NAME_BYTES,
    max_actor_ref_bytes=MAX_ACTOR_REF_BYTES,
    max_metadata_bytes=MAX_METADATA_BYTES,
    max_rebase_intent_bytes=MAX_REBASE_INTENT_BYTES,
    max_json_nesting_depth=MAX_JSON_NESTING_DEPTH,
    max_retained_safe_issues=MAX_RETAINED_SAFE_ISSUES,
    max_audit_records=MAX_AUDIT_RECORDS,
    max_public_affected_ids=MAX_PUBLIC_AFFECTED_IDS,
)

_CHUNK_PLAN_ID = re.compile(r"cpl-[0-9a-f]{64}\Z")
_CHUNK_ID = re.compile(r"chk-[0-9a-f]{64}\Z")
_OPERATION_ID = re.compile(r"cop-[0-9a-f]{64}\Z")
_WORKSPACE_OPERATION_ID = re.compile(r"reconcile-[0-9a-f]{64}\Z")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

_ERROR_CODES = frozenset(
    {
        "CHUNK.CONTRACT_INVALID",
        "CHUNK.IDENTITY_DUPLICATE",
        "CHUNK.IDENTITY_FOREIGN",
        "CHUNK.LIMIT_EXCEEDED",
        "CHUNK.MEMBER_UNKNOWN",
        "CHUNK.MEMBER_DUPLICATE",
        "CHUNK.MEMBER_OVERLAP",
        "CHUNK.MEMBER_UNALLOCATED_REQUIRED",
        "CHUNK.SPLIT_INVALID",
        "CHUNK.MERGE_DECISION_REQUIRED",
        "CHUNK.REBASE_REQUIRED",
        "CHUNK.REBASE_DECISION_REQUIRED",
        "CHUNK.ASSIGNMENT_UNAVAILABLE",
        "CHUNK.ACTOR_UNAVAILABLE",
        "CHUNK.ACTOR_UNVERIFIED",
        "CHUNK.MANAGER_REQUIRED",
        "CHUNK.NOT_ASSIGNEE",
        "CHUNK.OUTSIDE_CURRENT",
        "CHUNK.UNALLOCATED_READ_ONLY",
        "CHUNK.DETACHED_READ_ONLY",
        "CHUNK.PERMISSION_STALE",
        "CHUNK.PREVIEW_STALE",
        "CHUNK.REVISION_STALE",
        "CHUNK.DIVERGED",
        "CHUNK.UNIVERSE_MISMATCH",
        "CHUNK.UNDO_NOT_HEAD",
        "CHUNK.UNDO_UNAVAILABLE",
        "CHUNK.METADATA_UNSUPPORTED",
        "CHUNK.METADATA_INVALID",
        "CHUNK.METADATA_UNAVAILABLE",
        "CHUNK.METADATA_BINDING_STALE",
        "CHUNK.CONFLICT_STALE",
        "CHUNK.CONFLICT_RESOLUTION_INVALID",
        "CHUNK.CONFLICT_RESOLUTION_REQUIRED",
        "CHUNK.CONFLICT_REPLACE_UNAVAILABLE",
        "CHUNK.DIGEST_MISMATCH",
        "CHUNK.STAGE_FAILED",
        "CHUNK.DESTINATION_STALE",
        "CHUNK.COMMIT_FAILED",
        "CHUNK.RECOVERY_REQUIRED",
    }
)
CHUNK_ERROR_CODES = _ERROR_CODES


class ChunkError(RuntimeError):
    """Immutable error exposing only a stable CHUNK code."""

    __slots__ = ("_code", "_retryable", "_frozen")

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        if type(code) is not str or code not in _ERROR_CODES:
            raise ValueError("chunk error code must be a stable CHUNK code")
        if type(retryable) is not bool:
            raise TypeError("chunk retryable flag must be exact bool")
        object.__setattr__(self, "_code", code)
        object.__setattr__(self, "_retryable", retryable)
        super().__init__(code)
        object.__setattr__(self, "_frozen", True)

    @property
    def code(self) -> str:
        return self._code

    @property
    def retryable(self) -> bool:
        return self._retryable

    def __setattr__(self, name: str, value: object) -> None:
        if name in {
            "__traceback__",
            "__context__",
            "__cause__",
            "__suppress_context__",
            "__notes__",
        }:
            BaseException.__setattr__(self, name, value)
            return
        if getattr(self, "_frozen", False):
            raise AttributeError("chunk error is immutable")
        raise AttributeError("chunk error state is constructor-owned")

    def __str__(self) -> str:
        return self.code


def _fail(code: str = "CHUNK.CONTRACT_INVALID", *, retryable: bool = False) -> None:
    raise ChunkError(code, retryable=retryable)


def _translate_workspace_error(error: ProjectWorkspaceError) -> None:
    code = {
        "PROJECT.WORKSPACE.IDENTITY_DUPLICATE": "CHUNK.IDENTITY_DUPLICATE",
        "PROJECT.WORKSPACE.LIMIT_EXCEEDED": "CHUNK.LIMIT_EXCEEDED",
    }.get(error.code, "CHUNK.CONTRACT_INVALID")
    raise ChunkError(code) from None


def _validate_project_id(value: object) -> str:
    try:
        return validate_project_id(value)
    except ProjectWorkspaceError as error:
        _translate_workspace_error(error)


def _validate_sha256(value: object) -> str:
    try:
        return validate_sha256(value)
    except ProjectWorkspaceError as error:
        _translate_workspace_error(error)


def _exact_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        _fail()
    return value


def validate_chunk_project_id(value: object) -> str:
    """Validate the shared project identity without exposing workspace errors."""

    return _validate_project_id(value)


def _exact_positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        _fail()
    return value


def _exact_bool(value: object) -> bool:
    if type(value) is not bool:
        _fail()
    return value


def _exact_tuple(value: object) -> tuple[object, ...]:
    if type(value) is not tuple:
        _fail()
    return value


def _checked_add(left: int, right: int, *, maximum: int) -> int:
    _exact_nonnegative_int(left)
    _exact_nonnegative_int(right)
    if left > maximum - right:
        _fail("CHUNK.LIMIT_EXCEEDED")
    return left + right


def _exact_utf8_text(
    value: object,
    *,
    max_bytes: int,
    max_scalars: int | None = None,
) -> str:
    if type(value) is not str or not value.strip():
        _fail()
    assert type(value) is str
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail()
    if len(encoded) > max_bytes or (
        max_scalars is not None and len(value) > max_scalars
    ):
        _fail("CHUNK.LIMIT_EXCEEDED")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        _fail()
    return value


def validate_chunk_plan_id(value: object) -> str:
    if type(value) is not str or _CHUNK_PLAN_ID.fullmatch(value) is None:
        _fail()
    return value


def validate_chunk_id(value: object) -> str:
    if type(value) is not str or _CHUNK_ID.fullmatch(value) is None:
        _fail()
    return value


def validate_chunk_operation_id(value: object) -> str:
    if type(value) is not str or _OPERATION_ID.fullmatch(value) is None:
        _fail()
    return value


def validate_chunk_name(value: object) -> str:
    return _exact_utf8_text(
        value,
        max_bytes=MAX_CHUNK_NAME_BYTES,
        max_scalars=MAX_CHUNK_NAME_SCALARS,
    )


def validate_workspace_session_id(value: object) -> str:
    if type(value) is not str or _SESSION_ID.fullmatch(value) is None:
        _fail()
    return value


def _issue_token(prefix: str, domain: bytes, seed: bytes | None) -> str:
    if seed is None:
        seed = secrets.token_bytes(32)
    if type(seed) is not bytes or len(seed) != 32:
        _fail()
    return prefix + hashlib.sha256(domain + seed).hexdigest()


def issue_chunk_plan_id(seed: bytes | None = None) -> str:
    return _issue_token("cpl-", b"localcat.chunk.plan.issue.v1\0", seed)


def issue_chunk_id(seed: bytes | None = None) -> str:
    return _issue_token("chk-", b"localcat.chunk.issue.v1\0", seed)


def issue_chunk_operation_id(seed: bytes | None = None) -> str:
    return _issue_token("cop-", b"localcat.chunk.operation.issue.v1\0", seed)


def _length_prefixed(value: str) -> bytes:
    encoded = value.encode("utf-8", errors="strict")
    return len(encoded).to_bytes(8, "big", signed=False) + encoded


@dataclass(frozen=True, slots=True)
class AssigneeRef:
    authority_id: str
    subject_id: str

    def __post_init__(self) -> None:
        _exact_utf8_text(self.authority_id, max_bytes=MAX_ACTOR_REF_BYTES)
        _exact_utf8_text(self.subject_id, max_bytes=MAX_ACTOR_REF_BYTES)


@dataclass(frozen=True, slots=True)
class ChunkSegmentRef:
    project_id: str
    identity: SegmentIdentity

    def __post_init__(self) -> None:
        _validate_project_id(self.project_id)
        if type(self.identity) is not SegmentIdentity:
            _fail()
        try:
            self.identity.__post_init__()
        except ProjectWorkspaceError as error:
            _translate_workspace_error(error)


def chunk_segment_ref_from_ids(
    project_id: object,
    document_id: object,
    local_segment_id: object,
) -> ChunkSegmentRef:
    """Construct an exact reference while keeping workspace types private here."""

    project = _validate_project_id(project_id)
    try:
        identity = SegmentIdentity(document_id, local_segment_id)
    except ProjectWorkspaceError as error:
        _translate_workspace_error(error)
    return ChunkSegmentRef(project, identity)


@dataclass(frozen=True, slots=True)
class ChunkUniverseEntry:
    segment: ChunkSegmentRef
    source_presence: SourcePresence

    def __post_init__(self) -> None:
        if type(self.segment) is not ChunkSegmentRef:
            _fail()
        if type(self.source_presence) is not SourcePresence:
            _fail()


def _member_sort_key(member: ChunkSegmentRef) -> tuple[bytes, bytes]:
    local = member.identity.local_segment_id.encode("utf-8", errors="strict")
    return (
        member.identity.document_id.encode("ascii", errors="strict"),
        len(local).to_bytes(8, "big", signed=False) + local,
    )


def canonicalize_chunk_members(
    members: object,
    *,
    allow_empty: bool = False,
) -> tuple[ChunkSegmentRef, ...]:
    values = _exact_tuple(members)
    if type(allow_empty) is not bool:
        _fail()
    if not values and not allow_empty:
        _fail()
    if len(values) > MAX_MEMBERS_PER_CHUNK:
        _fail("CHUNK.LIMIT_EXCEEDED")
    if any(type(item) is not ChunkSegmentRef for item in values):
        _fail()
    seen: set[tuple[str, str, str]] = set()
    typed = tuple(values)
    for item in typed:
        assert type(item) is ChunkSegmentRef
        item.__post_init__()
        key = (
            item.project_id,
            item.identity.document_id,
            item.identity.local_segment_id,
        )
        if key in seen:
            _fail("CHUNK.MEMBER_DUPLICATE")
        seen.add(key)
    return tuple(sorted(typed, key=_member_sort_key))


def segment_universe_digest_v1(
    project_id: object,
    entries: object,
) -> str:
    project = _validate_project_id(project_id)
    values = _exact_tuple(entries)
    if len(values) > MAX_ACTIVE_MEMBERS:
        _fail("CHUNK.LIMIT_EXCEEDED")
    if any(type(item) is not ChunkUniverseEntry for item in values):
        _fail()
    typed = tuple(values)
    seen: set[tuple[str, str]] = set()
    canonical: list[ChunkUniverseEntry] = []
    for entry in typed:
        assert type(entry) is ChunkUniverseEntry
        entry.__post_init__()
        entry.segment.__post_init__()
        if entry.segment.project_id != project:
            _fail("CHUNK.IDENTITY_FOREIGN")
        key = (
            entry.segment.identity.document_id,
            entry.segment.identity.local_segment_id,
        )
        if key in seen:
            _fail("CHUNK.MEMBER_DUPLICATE")
        seen.add(key)
        canonical.append(entry)
    canonical.sort(key=lambda item: _member_sort_key(item.segment))
    digest = hashlib.sha256()
    digest.update(b"localcat.chunk.segment-universe.v1\0")
    digest.update(_length_prefixed(project))
    for entry in canonical:
        digest.update(_length_prefixed(entry.segment.identity.document_id))
        digest.update(_length_prefixed(entry.segment.identity.local_segment_id))
        digest.update(
            b"\x01"
            if entry.source_presence is SourcePresence.ATTACHED
            else b"\x02"
        )
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class CollaborativeChunk:
    chunk_id: str
    name: str
    order: int
    members: tuple[ChunkSegmentRef, ...]
    assignee: AssigneeRef | None

    def __post_init__(self) -> None:
        validate_chunk_id(self.chunk_id)
        validate_chunk_name(self.name)
        _exact_nonnegative_int(self.order)
        canonicalize_chunk_members(self.members)
        for member in self.members:
            member.__post_init__()
        if self.assignee is not None:
            if type(self.assignee) is not AssigneeRef:
                _fail()
            self.assignee.__post_init__()


@dataclass(frozen=True, slots=True)
class ChunkPlanSnapshot:
    schema_version: int
    namespace: str
    chunk_plan_id: str
    project_id: str
    revision: int
    segment_universe_digest: str
    chunks: tuple[CollaborativeChunk, ...]
    audit_head_digest: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail()
        if self.namespace != CHUNK_METADATA_NAMESPACE:
            _fail()
        validate_chunk_plan_id(self.chunk_plan_id)
        _validate_project_id(self.project_id)
        _exact_positive_int(self.revision)
        _validate_sha256(self.segment_universe_digest)
        values = _exact_tuple(self.chunks)
        if len(values) > MAX_ACTIVE_CHUNKS:
            _fail("CHUNK.LIMIT_EXCEEDED")
        if any(type(item) is not CollaborativeChunk for item in values):
            _fail()
        _validate_sha256(self.audit_head_digest)


def validate_chunk_plan_snapshot(snapshot: object) -> ChunkPlanSnapshot:
    if type(snapshot) is not ChunkPlanSnapshot:
        _fail()
    snapshot.__post_init__()
    chunk_ids: set[str] = set()
    member_keys: set[tuple[str, str]] = set()
    total = 0
    for expected_order, chunk in enumerate(snapshot.chunks):
        chunk.__post_init__()
        if chunk.order != expected_order:
            _fail()
        if chunk.chunk_id in chunk_ids:
            _fail("CHUNK.IDENTITY_DUPLICATE")
        chunk_ids.add(chunk.chunk_id)
        canonical = canonicalize_chunk_members(chunk.members)
        if canonical != chunk.members:
            _fail()
        total = _checked_add(total, len(chunk.members), maximum=MAX_ACTIVE_MEMBERS)
        for member in chunk.members:
            member.__post_init__()
            if member.project_id != snapshot.project_id:
                _fail("CHUNK.IDENTITY_FOREIGN")
            key = (
                member.identity.document_id,
                member.identity.local_segment_id,
            )
            if key in member_keys:
                _fail("CHUNK.MEMBER_OVERLAP")
            member_keys.add(key)
    return snapshot


def validate_c1_snapshot(snapshot: object) -> ChunkPlanSnapshot:
    validated = validate_chunk_plan_snapshot(snapshot)
    if any(chunk.assignee is not None for chunk in validated.chunks):
        _fail()
    return validated


def _chunk_semantic_value(chunk: CollaborativeChunk) -> dict[str, object]:
    assignee = None
    if chunk.assignee is not None:
        assignee = {
            "authority_id": chunk.assignee.authority_id,
            "subject_id": chunk.assignee.subject_id,
        }
    return {
        "assignee": assignee,
        "chunk_id": chunk.chunk_id,
        "members": [
            {
                "document_id": member.identity.document_id,
                "local_segment_id": member.identity.local_segment_id,
            }
            for member in chunk.members
        ],
        "name": chunk.name,
        "order": chunk.order,
    }


def chunk_plan_digest_v1(snapshot: object) -> str:
    snapshot = validate_chunk_plan_snapshot(snapshot)
    semantic = {
        "chunk_plan_id": snapshot.chunk_plan_id,
        "chunks": [_chunk_semantic_value(chunk) for chunk in snapshot.chunks],
        "namespace": snapshot.namespace,
        "project_id": snapshot.project_id,
        "revision": snapshot.revision,
        "schema_version": snapshot.schema_version,
        "segment_universe_digest": snapshot.segment_universe_digest,
    }
    encoded = json.dumps(
        semantic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(b"localcat.chunk.plan.semantic.v1\0" + encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkPlanBinding:
    project_id: str
    chunk_plan_id: str
    plan_revision: int
    plan_digest: str
    segment_universe_digest: str

    def __post_init__(self) -> None:
        _validate_project_id(self.project_id)
        validate_chunk_plan_id(self.chunk_plan_id)
        _exact_positive_int(self.plan_revision)
        _validate_sha256(self.plan_digest)
        _validate_sha256(self.segment_universe_digest)


def chunk_rebase_intent_digest_v1(
    project_id: object,
    plan_binding: object,
    transition: object,
) -> str:
    project = _validate_project_id(project_id)
    if type(plan_binding) is not ChunkPlanBinding:
        _fail()
    plan_binding.__post_init__()
    if type(transition) is not ChunkPublishedWorkspaceTransition:
        _fail()
    transition.__post_init__()
    semantic = {
        "project_id": project,
        "plan_binding": {
            "project_id": plan_binding.project_id,
            "chunk_plan_id": plan_binding.chunk_plan_id,
            "plan_revision": plan_binding.plan_revision,
            "plan_digest": plan_binding.plan_digest,
            "segment_universe_digest": plan_binding.segment_universe_digest,
        },
        "transition_digest": transition.transition_digest,
    }
    payload = json.dumps(
        semantic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(b"localcat.chunk.rebase-intent.v1\0" + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkRebaseIntent:
    """Durable pending evidence captured while the exact Workspace owner is live."""

    schema: str
    project_id: str
    plan_binding: ChunkPlanBinding
    transition: ChunkPublishedWorkspaceTransition
    intent_digest: str

    def __post_init__(self) -> None:
        if self.schema != CHUNK_REBASE_INTENT_SCHEMA:
            _fail("CHUNK.METADATA_UNSUPPORTED")
        project = _validate_project_id(self.project_id)
        if type(self.plan_binding) is not ChunkPlanBinding:
            _fail()
        self.plan_binding.__post_init__()
        if type(self.transition) is not ChunkPublishedWorkspaceTransition:
            _fail()
        self.transition.__post_init__()
        previous = self.transition.previous.binding
        current = self.transition.current.binding
        if (
            self.plan_binding.project_id != project
            or previous.project_id != project
            or current.project_id != project
            or self.plan_binding.segment_universe_digest
            != previous.segment_universe_digest
            or previous.segment_universe_digest == current.segment_universe_digest
        ):
            _fail("CHUNK.UNIVERSE_MISMATCH")
        expected = chunk_rebase_intent_digest_v1(
            project,
            self.plan_binding,
            self.transition,
        )
        if _validate_sha256(self.intent_digest) != expected:
            _fail("CHUNK.DIGEST_MISMATCH")


def _canonical_body_safe_members(
    values: object,
) -> tuple[ChunkSegmentRef, ...]:
    return canonicalize_chunk_members(values, allow_empty=True)


@dataclass(frozen=True, slots=True)
class ChunkRebaseInspection:
    intent_digest: str
    transition_digest: str
    plan_binding: ChunkPlanBinding
    previous_workspace_binding: ChunkPublishedUniverseBinding
    current_workspace_binding: ChunkPublishedUniverseBinding
    retained_attached_members: tuple[ChunkSegmentRef, ...]
    retained_detached_members: tuple[ChunkSegmentRef, ...]
    source_changed_members: tuple[ChunkSegmentRef, ...]
    missing_members: tuple[ChunkSegmentRef, ...]
    new_unallocated_members: tuple[ChunkSegmentRef, ...]

    def __post_init__(self) -> None:
        _validate_sha256(self.intent_digest)
        _validate_sha256(self.transition_digest)
        if type(self.plan_binding) is not ChunkPlanBinding:
            _fail()
        self.plan_binding.__post_init__()
        if (
            type(self.previous_workspace_binding) is not ChunkPublishedUniverseBinding
            or type(self.current_workspace_binding) is not ChunkPublishedUniverseBinding
        ):
            _fail()
        self.previous_workspace_binding.__post_init__()
        self.current_workspace_binding.__post_init__()
        groups = (
            self.retained_attached_members,
            self.retained_detached_members,
            self.source_changed_members,
            self.missing_members,
            self.new_unallocated_members,
        )
        canonical = tuple(_canonical_body_safe_members(group) for group in groups)
        if canonical != groups:
            _fail()
        retained = set(self.retained_attached_members) | set(
            self.retained_detached_members
        )
        if (
            set(self.retained_attached_members)
            & set(self.retained_detached_members)
            or not set(self.source_changed_members).issubset(retained)
            or set(self.missing_members) & retained
            or set(self.new_unallocated_members)
            & (retained | set(self.missing_members))
        ):
            _fail("CHUNK.UNIVERSE_MISMATCH")


@dataclass(frozen=True, slots=True)
class ChunkRebasePreview:
    inspection: ChunkRebaseInspection
    mutation: ChunkMutationPreview
    released_missing_members: tuple[ChunkSegmentRef, ...]
    retired_empty_chunk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.inspection) is not ChunkRebaseInspection:
            _fail()
        self.inspection.__post_init__()
        if type(self.mutation) is not ChunkMutationPreview:
            _fail()
        self.mutation.__post_init__()
        released = _canonical_body_safe_members(self.released_missing_members)
        if (
            released != self.released_missing_members
            or released != self.inspection.missing_members
            or self.mutation.action is not TopologyAction.REBASE
            or self.mutation.created_chunk_count != 0
            or self.mutation.created_chunk_ids
            or self.mutation.assignment_count != 0
        ):
            _fail("CHUNK.REBASE_DECISION_REQUIRED")
        retired = _validate_chunk_ids(self.retired_empty_chunk_ids)
        if (
            retired != self.retired_empty_chunk_ids
            or retired != self.mutation.retired_chunk_ids
        ):
            _fail("CHUNK.REBASE_DECISION_REQUIRED")


def validate_chunk_published_workspace_transition(
    value: object,
) -> ChunkPublishedWorkspaceTransition:
    if type(value) is not ChunkPublishedWorkspaceTransition:
        _fail()
    value.__post_init__()
    return value


def validate_chunk_rebase_intent(value: object) -> ChunkRebaseIntent:
    if type(value) is not ChunkRebaseIntent:
        _fail()
    value.__post_init__()
    return value


def validate_chunk_rebase_inspection(value: object) -> ChunkRebaseInspection:
    if type(value) is not ChunkRebaseInspection:
        _fail()
    value.__post_init__()
    return value


def validate_chunk_rebase_preview(value: object) -> ChunkRebasePreview:
    if type(value) is not ChunkRebasePreview:
        _fail()
    value.__post_init__()
    return value


@dataclass(frozen=True, slots=True)
class ChunkWorkspaceBinding:
    project_id: str
    workspace_session_id: str
    workspace_revision: int
    segment_universe_digest: str
    workspace_composition_revision: int = 0

    def __post_init__(self) -> None:
        _validate_project_id(self.project_id)
        validate_workspace_session_id(self.workspace_session_id)
        _exact_nonnegative_int(self.workspace_revision)
        _validate_sha256(self.segment_universe_digest)
        _exact_nonnegative_int(self.workspace_composition_revision)


@dataclass(frozen=True, slots=True)
class ChunkWorkspaceUniverseProjection:
    """Body-free authoritative universe facts for topology validation."""

    binding: ChunkWorkspaceBinding
    entries: tuple[ChunkUniverseEntry, ...]

    def __post_init__(self) -> None:
        if type(self.binding) is not ChunkWorkspaceBinding:
            _fail()
        self.binding.__post_init__()
        values = _exact_tuple(self.entries)
        if any(type(entry) is not ChunkUniverseEntry for entry in values):
            _fail()
        for entry in values:
            entry.__post_init__()
        if (
            segment_universe_digest_v1(self.binding.project_id, values)
            != self.binding.segment_universe_digest
        ):
            _fail("CHUNK.UNIVERSE_MISMATCH")


@dataclass(frozen=True, slots=True)
class ChunkPublishedUniverseBinding:
    """One body-free Workspace publication binding captured for rebase."""

    project_id: str
    workspace_session_id: str
    workspace_revision: int
    workspace_composition_revision: int
    workspace_digest: str
    segment_universe_digest: str

    def __post_init__(self) -> None:
        _validate_project_id(self.project_id)
        validate_workspace_session_id(self.workspace_session_id)
        _exact_nonnegative_int(self.workspace_revision)
        _exact_nonnegative_int(self.workspace_composition_revision)
        _validate_sha256(self.workspace_digest)
        _validate_sha256(self.segment_universe_digest)


@dataclass(frozen=True, slots=True)
class ChunkPublishedUniverseProjection:
    binding: ChunkPublishedUniverseBinding
    entries: tuple[ChunkUniverseEntry, ...]

    def __post_init__(self) -> None:
        if type(self.binding) is not ChunkPublishedUniverseBinding:
            _fail()
        self.binding.__post_init__()
        values = _exact_tuple(self.entries)
        if len(values) > MAX_ACTIVE_MEMBERS:
            _fail("CHUNK.LIMIT_EXCEEDED")
        if any(type(entry) is not ChunkUniverseEntry for entry in values):
            _fail()
        canonical = tuple(sorted(values, key=lambda entry: _member_sort_key(entry.segment)))
        if values != canonical:
            _fail()
        if (
            segment_universe_digest_v1(self.binding.project_id, values)
            != self.binding.segment_universe_digest
        ):
            _fail("CHUNK.UNIVERSE_MISMATCH")


def chunk_published_workspace_transition_digest_v1(
    operation_id: object,
    previous: object,
    current: object,
    source_changed_members: object,
) -> str:
    if type(operation_id) is not str or _WORKSPACE_OPERATION_ID.fullmatch(operation_id) is None:
        _fail()
    if (
        type(previous) is not ChunkPublishedUniverseProjection
        or type(current) is not ChunkPublishedUniverseProjection
    ):
        _fail()
    previous.__post_init__()
    current.__post_init__()
    changed = canonicalize_chunk_members(source_changed_members, allow_empty=True)
    semantic = {
        "operation_id": operation_id,
        "previous": _published_universe_semantic_value(previous),
        "current": _published_universe_semantic_value(current),
        "source_changed_members": [_segment_semantic_value(member) for member in changed],
    }
    payload = json.dumps(
        semantic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(
        b"localcat.chunk.workspace-transition.v1\0" + payload
    ).hexdigest()


def _segment_semantic_value(member: ChunkSegmentRef) -> dict[str, str]:
    member.__post_init__()
    return {
        "project_id": member.project_id,
        "document_id": member.identity.document_id,
        "local_segment_id": member.identity.local_segment_id,
    }


def _published_universe_semantic_value(
    projection: ChunkPublishedUniverseProjection,
) -> dict[str, object]:
    projection.__post_init__()
    binding = projection.binding
    return {
        "binding": {
            "project_id": binding.project_id,
            "workspace_session_id": binding.workspace_session_id,
            "workspace_revision": binding.workspace_revision,
            "workspace_composition_revision": (
                binding.workspace_composition_revision
            ),
            "workspace_digest": binding.workspace_digest,
            "segment_universe_digest": binding.segment_universe_digest,
        },
        "entries": [
            {
                "segment": _segment_semantic_value(entry.segment),
                "source_presence": entry.source_presence.value,
            }
            for entry in projection.entries
        ],
    }


@dataclass(frozen=True, slots=True)
class ChunkPublishedWorkspaceTransition:
    """Trusted-composition translation of one live owner-issued transition."""

    operation_id: str
    previous: ChunkPublishedUniverseProjection
    current: ChunkPublishedUniverseProjection
    source_changed_members: tuple[ChunkSegmentRef, ...]
    transition_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.previous) is not ChunkPublishedUniverseProjection
            or type(self.current) is not ChunkPublishedUniverseProjection
        ):
            _fail()
        self.previous.__post_init__()
        self.current.__post_init__()
        previous = self.previous.binding
        current = self.current.binding
        if (
            previous.project_id != current.project_id
            or previous.workspace_session_id != current.workspace_session_id
            or current.workspace_revision != previous.workspace_revision + 1
            or current.workspace_composition_revision
            != previous.workspace_composition_revision + 1
        ):
            _fail()
        changed = canonicalize_chunk_members(
            self.source_changed_members,
            allow_empty=True,
        )
        if changed != self.source_changed_members:
            _fail()
        previous_members = {entry.segment for entry in self.previous.entries}
        current_members = {entry.segment for entry in self.current.entries}
        if any(
            member not in previous_members or member not in current_members
            for member in changed
        ):
            _fail("CHUNK.UNIVERSE_MISMATCH")
        expected = chunk_published_workspace_transition_digest_v1(
            self.operation_id,
            self.previous,
            self.current,
            changed,
        )
        if _validate_sha256(self.transition_digest) != expected:
            _fail("CHUNK.DIGEST_MISMATCH")


@dataclass(frozen=True, slots=True)
class ChunkProgressSegmentFact:
    """Body-free Workspace fact used only to derive Chunk progress."""

    segment: ChunkSegmentRef
    source_presence: SourcePresence
    target_is_blank: bool
    confirmed: bool

    def __post_init__(self) -> None:
        if type(self.segment) is not ChunkSegmentRef:
            _fail()
        self.segment.__post_init__()
        if type(self.source_presence) is not SourcePresence:
            _fail()
        _exact_bool(self.target_is_blank)
        _exact_bool(self.confirmed)


@dataclass(frozen=True, slots=True)
class ChunkWorkspaceProgressProjection:
    """Complete body-free Workspace universe plus progress classifications."""

    binding: ChunkWorkspaceBinding
    entries: tuple[ChunkProgressSegmentFact, ...]

    def __post_init__(self) -> None:
        if type(self.binding) is not ChunkWorkspaceBinding:
            _fail()
        self.binding.__post_init__()
        values = _exact_tuple(self.entries)
        if len(values) > MAX_ACTIVE_MEMBERS:
            _fail("CHUNK.LIMIT_EXCEEDED")
        if any(type(entry) is not ChunkProgressSegmentFact for entry in values):
            _fail()
        universe: list[ChunkUniverseEntry] = []
        for entry in values:
            entry.__post_init__()
            universe.append(
                ChunkUniverseEntry(entry.segment, entry.source_presence)
            )
        if (
            segment_universe_digest_v1(self.binding.project_id, tuple(universe))
            != self.binding.segment_universe_digest
        ):
            _fail("CHUNK.UNIVERSE_MISMATCH")


@dataclass(frozen=True, slots=True)
class ChunkProgress:
    chunk_id: str
    attached_total: int
    unfilled: int
    draft: int
    confirmed: int
    detached: int
    completion_numerator: int
    completion_denominator: int

    def __post_init__(self) -> None:
        validate_chunk_id(self.chunk_id)
        values = (
            self.attached_total,
            self.unfilled,
            self.draft,
            self.confirmed,
            self.detached,
            self.completion_numerator,
            self.completion_denominator,
        )
        for value in values:
            _exact_nonnegative_int(value)
        if self.attached_total != self.unfilled + self.draft + self.confirmed:
            _fail()
        if self.completion_numerator != self.confirmed:
            _fail()
        if self.completion_denominator != self.attached_total:
            _fail()


def chunk_plan_binding(snapshot: object) -> ChunkPlanBinding:
    validated = validate_chunk_plan_snapshot(snapshot)
    return ChunkPlanBinding(
        project_id=validated.project_id,
        chunk_plan_id=validated.chunk_plan_id,
        plan_revision=validated.revision,
        plan_digest=chunk_plan_digest_v1(validated),
        segment_universe_digest=validated.segment_universe_digest,
    )


@dataclass(frozen=True, slots=True)
class ChunkScopeProjection:
    project_id: str
    chunk_plan_id: str
    plan_revision: int
    plan_digest: str
    segment_universe_digest: str
    chunk_id: str
    members: tuple[ChunkSegmentRef, ...]

    def __post_init__(self) -> None:
        ChunkPlanBinding(
            self.project_id,
            self.chunk_plan_id,
            self.plan_revision,
            self.plan_digest,
            self.segment_universe_digest,
        )
        validate_chunk_id(self.chunk_id)
        canonical = canonicalize_chunk_members(self.members)
        if canonical != self.members:
            _fail()
        if any(member.project_id != self.project_id for member in self.members):
            _fail("CHUNK.IDENTITY_FOREIGN")


class ChunkAccessKind(Enum):
    EDITABLE_ASSIGNED_CURRENT = "editable_assigned_current"
    READ_ONLY_NO_PLAN = "read_only_no_plan"
    READ_ONLY_NO_CURRENT_CHUNK = "read_only_no_current_chunk"
    READ_ONLY_UNALLOCATED = "read_only_unallocated"
    READ_ONLY_OUTSIDE_CURRENT = "read_only_outside_current"
    READ_ONLY_NOT_ASSIGNEE = "read_only_not_assignee"
    READ_ONLY_DETACHED = "read_only_detached"
    READ_ONLY_STALE = "read_only_stale"


_ACCESS_SAFE_CODE = {
    ChunkAccessKind.READ_ONLY_NO_PLAN: "CHUNK.PERMISSION_STALE",
    ChunkAccessKind.READ_ONLY_NO_CURRENT_CHUNK: "CHUNK.OUTSIDE_CURRENT",
    ChunkAccessKind.READ_ONLY_UNALLOCATED: "CHUNK.UNALLOCATED_READ_ONLY",
    ChunkAccessKind.READ_ONLY_OUTSIDE_CURRENT: "CHUNK.OUTSIDE_CURRENT",
    ChunkAccessKind.READ_ONLY_NOT_ASSIGNEE: "CHUNK.NOT_ASSIGNEE",
    ChunkAccessKind.READ_ONLY_DETACHED: "CHUNK.DETACHED_READ_ONLY",
    ChunkAccessKind.READ_ONLY_STALE: "CHUNK.PERMISSION_STALE",
}


class ChunkEditOperation(Enum):
    SEGMENT_EDIT = "segment_edit"


@dataclass(frozen=True, slots=True)
class ChunkAccessDecision:
    project_id: str
    chunk_plan_id: str | None
    plan_revision: int | None
    plan_digest: str | None
    actor: AssigneeRef
    current_chunk_id: str | None
    segment: ChunkSegmentRef
    access: ChunkAccessKind
    may_edit_target: bool
    may_change_confirmed: bool
    safe_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        project_id = _validate_project_id(self.project_id)
        if type(self.actor) is not AssigneeRef:
            _fail()
        self.actor.__post_init__()
        if type(self.segment) is not ChunkSegmentRef:
            _fail()
        self.segment.__post_init__()
        if self.segment.project_id != project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        if self.chunk_plan_id is None:
            if self.plan_revision is not None or self.plan_digest is not None:
                _fail()
            if (
                self.current_chunk_id is not None
                or self.access is not ChunkAccessKind.READ_ONLY_NO_PLAN
            ):
                _fail()
        else:
            validate_chunk_plan_id(self.chunk_plan_id)
            _exact_positive_int(self.plan_revision)
            _validate_sha256(self.plan_digest)
            if self.access is ChunkAccessKind.READ_ONLY_NO_PLAN:
                _fail()
        if self.current_chunk_id is not None:
            validate_chunk_id(self.current_chunk_id)
        if type(self.access) is not ChunkAccessKind:
            _fail()
        may_edit = _exact_bool(self.may_edit_target)
        may_confirm = _exact_bool(self.may_change_confirmed)
        codes = _validate_safe_codes(self.safe_codes)
        if self.access is ChunkAccessKind.EDITABLE_ASSIGNED_CURRENT:
            if not may_edit or not may_confirm or codes:
                _fail()
        else:
            expected = _ACCESS_SAFE_CODE.get(self.access)
            if may_edit or may_confirm or expected is None or codes != (expected,):
                _fail()
        if (
            self.access
            in {
                ChunkAccessKind.EDITABLE_ASSIGNED_CURRENT,
                ChunkAccessKind.READ_ONLY_UNALLOCATED,
                ChunkAccessKind.READ_ONLY_OUTSIDE_CURRENT,
                ChunkAccessKind.READ_ONLY_NOT_ASSIGNEE,
                ChunkAccessKind.READ_ONLY_DETACHED,
            }
            and self.current_chunk_id is None
        ):
            _fail()
        if (
            self.access is ChunkAccessKind.READ_ONLY_NO_CURRENT_CHUNK
            and self.current_chunk_id is not None
        ):
            _fail()


class TopologyAction(Enum):
    CREATE = "create"
    RENAME = "rename"
    REORDER = "reorder"
    SPLIT = "split"
    MERGE = "merge"
    MOVE = "move"
    RELEASE = "release"
    DISSOLVE_CHUNK = "dissolve_chunk"
    DISSOLVE_PLAN = "dissolve_plan"
    REBASE = "rebase"
    ASSIGN = "assign"
    REASSIGN = "reassign"
    UNASSIGN = "unassign"
    CONFLICT_REPLACE = "conflict_replace"
    UNDO = "undo"


_ASSIGNMENT_ACTIONS = frozenset(
    {
        TopologyAction.ASSIGN,
        TopologyAction.REASSIGN,
        TopologyAction.UNASSIGN,
    }
)


@dataclass(frozen=True, slots=True)
class ChunkSplitChild:
    """Exact child proposal with an explicit C2 final-assignment decision."""

    name: str
    members: tuple[ChunkSegmentRef, ...]
    assignee: AssigneeRef | None = None
    assignment_decided: bool = False

    def __post_init__(self) -> None:
        validate_chunk_name(self.name)
        canonical = canonicalize_chunk_members(self.members)
        if canonical != self.members:
            _fail()
        if self.assignee is not None:
            if type(self.assignee) is not AssigneeRef:
                _fail()
            self.assignee.__post_init__()
        _exact_bool(self.assignment_decided)


def _validate_chunk_ids(values: object) -> tuple[str, ...]:
    typed = _exact_tuple(values)
    if len(typed) > MAX_PUBLIC_AFFECTED_IDS:
        _fail("CHUNK.LIMIT_EXCEEDED")
    if any(type(value) is not str for value in typed):
        _fail()
    result = tuple(validate_chunk_id(value) for value in typed)
    if len(result) != len(set(result)):
        _fail()
    return result


def _validate_safe_codes(values: object) -> tuple[str, ...]:
    typed = _exact_tuple(values)
    if len(typed) > MAX_RETAINED_SAFE_ISSUES:
        _fail("CHUNK.LIMIT_EXCEEDED")
    if any(type(value) is not str or value not in _ERROR_CODES for value in typed):
        _fail()
    return tuple(typed)


def _validate_operation_projection(
    *,
    operation_id: object,
    action: object,
    project_id: object,
    chunk_plan_id: object,
    base_revision: object,
    published_revision: object,
    before_plan_digest: object,
    after_plan_digest: object,
    affected_chunk_ids: object,
    created_chunk_ids: object,
    retired_chunk_ids: object,
    affected_chunk_count: object,
    created_chunk_count: object,
    retired_chunk_count: object,
    affected_member_count: object,
    assignment_count: object,
) -> None:
    validate_chunk_operation_id(operation_id)
    if type(action) is not TopologyAction:
        _fail()
    _validate_project_id(project_id)
    validate_chunk_plan_id(chunk_plan_id)
    base = _exact_nonnegative_int(base_revision)
    published = _exact_positive_int(published_revision)
    if published != base + 1:
        _fail()
    before = _validate_sha256(before_plan_digest)
    after = _validate_sha256(after_plan_digest)
    if before == after:
        _fail()
    affected = _validate_chunk_ids(affected_chunk_ids)
    created = _validate_chunk_ids(created_chunk_ids)
    retired = _validate_chunk_ids(retired_chunk_ids)
    if not set(created).issubset(affected) or not set(retired).issubset(affected):
        _fail()
    if set(created) & set(retired):
        _fail()
    affected_count = _exact_nonnegative_int(affected_chunk_count)
    created_count = _exact_nonnegative_int(created_chunk_count)
    retired_count = _exact_nonnegative_int(retired_chunk_count)
    if (
        affected_count < len(affected)
        or created_count < len(created)
        or retired_count < len(retired)
        or created_count > affected_count
        or retired_count > affected_count
        or created_count > affected_count - retired_count
    ):
        _fail()
    count = _exact_nonnegative_int(affected_member_count)
    if count > MAX_ACTIVE_MEMBERS:
        _fail("CHUNK.LIMIT_EXCEEDED")
    if _exact_nonnegative_int(assignment_count) > MAX_ACTIVE_CHUNKS:
        _fail("CHUNK.LIMIT_EXCEEDED")


def _validate_assignment_projection_semantics(
    projection: ChunkMutationPreview | ChunkOperationReceipt,
) -> None:
    if projection.action in _ASSIGNMENT_ACTIONS:
        if (
            projection.assignment_count != 1
            or len(projection.affected_chunk_ids) != 1
            or projection.affected_chunk_count != 1
            or projection.created_chunk_ids
            or projection.retired_chunk_ids
            or projection.created_chunk_count != 0
            or projection.retired_chunk_count != 0
            or projection.affected_member_count == 0
        ):
            _fail()


def _validate_truncation_projection(
    *,
    affected_chunk_ids: tuple[str, ...],
    created_chunk_ids: tuple[str, ...],
    retired_chunk_ids: tuple[str, ...],
    affected_chunk_count: int,
    created_chunk_count: int,
    retired_chunk_count: int,
    truncated: object,
    safe_codes: tuple[str, ...],
) -> None:
    is_truncated = _exact_bool(truncated)
    retained_counts = (
        len(affected_chunk_ids),
        len(created_chunk_ids),
        len(retired_chunk_ids),
    )
    total_counts = (
        affected_chunk_count,
        created_chunk_count,
        retired_chunk_count,
    )
    if not is_truncated:
        if total_counts != retained_counts:
            _fail()
        return
    if (
        max(total_counts) <= MAX_PUBLIC_AFFECTED_IDS
        or not any(total > retained for total, retained in zip(total_counts, retained_counts))
        or "CHUNK.LIMIT_EXCEEDED" not in safe_codes
    ):
        _fail()


@dataclass(frozen=True, slots=True)
class ChunkMutationPreview:
    operation_id: str
    action: TopologyAction
    project_id: str
    chunk_plan_id: str
    base_revision: int
    published_revision: int
    before_plan_digest: str
    after_plan_digest: str
    affected_chunk_ids: tuple[str, ...]
    created_chunk_ids: tuple[str, ...]
    retired_chunk_ids: tuple[str, ...]
    affected_chunk_count: int
    created_chunk_count: int
    retired_chunk_count: int
    affected_member_count: int
    assignment_count: int
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    truncated: bool

    def __post_init__(self) -> None:
        _validate_operation_projection(
            operation_id=self.operation_id,
            action=self.action,
            project_id=self.project_id,
            chunk_plan_id=self.chunk_plan_id,
            base_revision=self.base_revision,
            published_revision=self.published_revision,
            before_plan_digest=self.before_plan_digest,
            after_plan_digest=self.after_plan_digest,
            affected_chunk_ids=self.affected_chunk_ids,
            created_chunk_ids=self.created_chunk_ids,
            retired_chunk_ids=self.retired_chunk_ids,
            affected_chunk_count=self.affected_chunk_count,
            created_chunk_count=self.created_chunk_count,
            retired_chunk_count=self.retired_chunk_count,
            affected_member_count=self.affected_member_count,
            assignment_count=self.assignment_count,
        )
        warnings = _validate_safe_codes(self.warnings)
        _validate_safe_codes(self.blockers)
        _validate_truncation_projection(
            affected_chunk_ids=self.affected_chunk_ids,
            created_chunk_ids=self.created_chunk_ids,
            retired_chunk_ids=self.retired_chunk_ids,
            affected_chunk_count=self.affected_chunk_count,
            created_chunk_count=self.created_chunk_count,
            retired_chunk_count=self.retired_chunk_count,
            truncated=self.truncated,
            safe_codes=warnings,
        )


@dataclass(frozen=True, slots=True)
class ChunkOperationReceipt:
    operation_id: str
    action: TopologyAction
    project_id: str
    chunk_plan_id: str
    base_revision: int
    published_revision: int
    before_plan_digest: str
    after_plan_digest: str
    affected_chunk_ids: tuple[str, ...]
    created_chunk_ids: tuple[str, ...]
    retired_chunk_ids: tuple[str, ...]
    affected_chunk_count: int
    created_chunk_count: int
    retired_chunk_count: int
    affected_member_count: int
    assignment_count: int
    actor_ref: AssigneeRef
    safe_issues: tuple[str, ...]
    truncated: bool
    audit_record_digest: str

    def __post_init__(self) -> None:
        _validate_operation_projection(
            operation_id=self.operation_id,
            action=self.action,
            project_id=self.project_id,
            chunk_plan_id=self.chunk_plan_id,
            base_revision=self.base_revision,
            published_revision=self.published_revision,
            before_plan_digest=self.before_plan_digest,
            after_plan_digest=self.after_plan_digest,
            affected_chunk_ids=self.affected_chunk_ids,
            created_chunk_ids=self.created_chunk_ids,
            retired_chunk_ids=self.retired_chunk_ids,
            affected_chunk_count=self.affected_chunk_count,
            created_chunk_count=self.created_chunk_count,
            retired_chunk_count=self.retired_chunk_count,
            affected_member_count=self.affected_member_count,
            assignment_count=self.assignment_count,
        )
        if type(self.actor_ref) is not AssigneeRef:
            _fail()
        self.actor_ref.__post_init__()
        safe_issues = _validate_safe_codes(self.safe_issues)
        _validate_truncation_projection(
            affected_chunk_ids=self.affected_chunk_ids,
            created_chunk_ids=self.created_chunk_ids,
            retired_chunk_ids=self.retired_chunk_ids,
            affected_chunk_count=self.affected_chunk_count,
            created_chunk_count=self.created_chunk_count,
            retired_chunk_count=self.retired_chunk_count,
            truncated=self.truncated,
            safe_codes=safe_issues,
        )
        _validate_sha256(self.audit_record_digest)


def validate_chunk_mutation_preview(preview: object) -> ChunkMutationPreview:
    if type(preview) is not ChunkMutationPreview:
        _fail()
    preview.__post_init__()
    _validate_assignment_projection_semantics(preview)
    return preview


def validate_c1_mutation_preview(preview: object) -> ChunkMutationPreview:
    preview = validate_chunk_mutation_preview(preview)
    if preview.assignment_count != 0:
        _fail()
    if preview.action in _ASSIGNMENT_ACTIONS or preview.action in {
        TopologyAction.REBASE,
        TopologyAction.CONFLICT_REPLACE,
        TopologyAction.UNDO,
    }:
        _fail()
    return preview


def validate_chunk_operation_receipt(receipt: object) -> ChunkOperationReceipt:
    if type(receipt) is not ChunkOperationReceipt:
        _fail()
    receipt.__post_init__()
    _validate_assignment_projection_semantics(receipt)
    return receipt


def validate_c1_operation_receipt(receipt: object) -> ChunkOperationReceipt:
    receipt = validate_chunk_operation_receipt(receipt)
    if receipt.assignment_count != 0:
        _fail()
    if receipt.action in _ASSIGNMENT_ACTIONS or receipt.action in {
        TopologyAction.REBASE,
        TopologyAction.CONFLICT_REPLACE,
        TopologyAction.UNDO,
    }:
        _fail()
    return receipt


def validate_assignment_plan_successor(
    before: object,
    after: object,
    receipt: object,
) -> ChunkPlanSnapshot:
    """Prove that an assignment publication changed exactly one assignee."""

    previous = validate_chunk_plan_snapshot(before)
    candidate = validate_chunk_plan_snapshot(after)
    operation = validate_chunk_operation_receipt(receipt)
    if operation.action not in _ASSIGNMENT_ACTIONS:
        _fail()
    if (
        previous.project_id != candidate.project_id
        or previous.chunk_plan_id != candidate.chunk_plan_id
        or previous.schema_version != candidate.schema_version
        or previous.namespace != candidate.namespace
        or previous.segment_universe_digest != candidate.segment_universe_digest
        or candidate.revision != previous.revision + 1
        or operation.project_id != previous.project_id
        or operation.chunk_plan_id != previous.chunk_plan_id
        or operation.base_revision != previous.revision
        or operation.published_revision != candidate.revision
        or operation.before_plan_digest != chunk_plan_digest_v1(previous)
        or operation.after_plan_digest != chunk_plan_digest_v1(candidate)
        or candidate.audit_head_digest != operation.audit_record_digest
        or len(previous.chunks) != len(candidate.chunks)
    ):
        _fail()
    affected_id = operation.affected_chunk_ids[0]
    changed = 0
    for old, new in zip(previous.chunks, candidate.chunks):
        if (
            old.chunk_id != new.chunk_id
            or old.name != new.name
            or old.order != new.order
            or old.members != new.members
        ):
            _fail()
        if old.assignee == new.assignee:
            if old.chunk_id == affected_id:
                _fail()
            continue
        if old.chunk_id != affected_id:
            _fail()
        changed += 1
        if operation.action is TopologyAction.ASSIGN:
            if old.assignee is not None or new.assignee is None:
                _fail()
        elif operation.action is TopologyAction.REASSIGN:
            if old.assignee is None or new.assignee is None:
                _fail()
        elif old.assignee is None or new.assignee is not None:
            _fail()
    if changed != 1:
        _fail()
    return candidate


def validate_topology_assignment_successor(
    before: ChunkPlanSnapshot | None,
    after: ChunkPlanSnapshot | None,
    receipt: object,
) -> None:
    """Keep existing assignees exact and count assigned newly-created chunks."""

    operation = validate_chunk_operation_receipt(receipt)
    if operation.action in _ASSIGNMENT_ACTIONS or operation.action in {
        TopologyAction.REBASE,
        TopologyAction.CONFLICT_REPLACE,
        TopologyAction.UNDO,
    }:
        _fail()
    previous = None if before is None else validate_chunk_plan_snapshot(before)
    candidate = None if after is None else validate_chunk_plan_snapshot(after)
    before_by_id = (
        {} if previous is None else {chunk.chunk_id: chunk for chunk in previous.chunks}
    )
    after_by_id = (
        {} if candidate is None else {chunk.chunk_id: chunk for chunk in candidate.chunks}
    )
    if (
        previous is not None
        and candidate is not None
        and previous.segment_universe_digest != candidate.segment_universe_digest
    ):
        _fail("CHUNK.UNIVERSE_MISMATCH")
    for chunk_id in set(before_by_id) & set(after_by_id):
        if before_by_id[chunk_id].assignee != after_by_id[chunk_id].assignee:
            _fail()
    expected_count = sum(
        chunk.assignee is not None
        for chunk_id, chunk in after_by_id.items()
        if chunk_id not in before_by_id
    )
    if operation.assignment_count != expected_count:
        _fail()


def validate_conflict_replace_plan_successor(
    before: object,
    after: object,
    receipt: object,
) -> ChunkPlanSnapshot:
    """Validate one explicit whole-semantic replacement as a local successor."""

    previous = validate_chunk_plan_snapshot(before)
    candidate = validate_chunk_plan_snapshot(after)
    operation = validate_chunk_operation_receipt(receipt)
    if (
        operation.action is not TopologyAction.CONFLICT_REPLACE
        or previous.project_id != candidate.project_id
        or previous.chunk_plan_id != candidate.chunk_plan_id
        or previous.schema_version != candidate.schema_version
        or previous.namespace != candidate.namespace
        or previous.segment_universe_digest != candidate.segment_universe_digest
        or candidate.revision != previous.revision + 1
        or operation.project_id != previous.project_id
        or operation.chunk_plan_id != previous.chunk_plan_id
        or operation.base_revision != previous.revision
        or operation.published_revision != candidate.revision
        or operation.before_plan_digest != chunk_plan_digest_v1(previous)
        or operation.after_plan_digest != chunk_plan_digest_v1(candidate)
        or candidate.audit_head_digest != operation.audit_record_digest
    ):
        _fail()
    before_by_id = {chunk.chunk_id: chunk for chunk in previous.chunks}
    after_by_id = {chunk.chunk_id: chunk for chunk in candidate.chunks}
    created = set(after_by_id) - set(before_by_id)
    retired = set(before_by_id) - set(after_by_id)
    affected = {
        chunk_id
        for chunk_id in set(before_by_id) | set(after_by_id)
        if before_by_id.get(chunk_id) != after_by_id.get(chunk_id)
    }
    assignment_count = sum(
        (
            None if before_by_id.get(chunk_id) is None
            else before_by_id[chunk_id].assignee
        )
        != (
            None if after_by_id.get(chunk_id) is None
            else after_by_id[chunk_id].assignee
        )
        for chunk_id in affected
    )
    if (
        set(operation.created_chunk_ids) != created
        or set(operation.retired_chunk_ids) != retired
        or set(operation.affected_chunk_ids) != affected
        or operation.created_chunk_count != len(created)
        or operation.retired_chunk_count != len(retired)
        or operation.affected_chunk_count != len(affected)
        or operation.affected_member_count
        != sum(len(chunk.members) for chunk in candidate.chunks)
        or operation.assignment_count != assignment_count
    ):
        _fail()
    return candidate


def validate_undo_plan_successor(
    before: object,
    after: object,
    receipt: object,
    exact_previous: object,
) -> ChunkPlanSnapshot:
    """Validate restoration of the stored current-head predecessor semantics."""

    current = validate_chunk_plan_snapshot(before)
    candidate = validate_chunk_plan_snapshot(after)
    previous = validate_chunk_plan_snapshot(exact_previous)
    operation = validate_chunk_operation_receipt(receipt)
    if (
        operation.action is not TopologyAction.UNDO
        or current.project_id != candidate.project_id
        or current.project_id != previous.project_id
        or current.chunk_plan_id != candidate.chunk_plan_id
        or current.chunk_plan_id != previous.chunk_plan_id
        or current.segment_universe_digest != candidate.segment_universe_digest
        or current.segment_universe_digest != previous.segment_universe_digest
        or candidate.revision != current.revision + 1
        or operation.project_id != current.project_id
        or operation.chunk_plan_id != current.chunk_plan_id
        or operation.base_revision != current.revision
        or operation.published_revision != candidate.revision
        or operation.before_plan_digest != chunk_plan_digest_v1(current)
        or operation.after_plan_digest != chunk_plan_digest_v1(candidate)
        or candidate.audit_head_digest != operation.audit_record_digest
        or tuple(
            (item.chunk_id, item.name, item.order, item.members, item.assignee)
            for item in candidate.chunks
        )
        != tuple(
            (item.chunk_id, item.name, item.order, item.members, item.assignee)
            for item in previous.chunks
        )
    ):
        _fail()
    before_by_id = {chunk.chunk_id: chunk for chunk in current.chunks}
    after_by_id = {chunk.chunk_id: chunk for chunk in candidate.chunks}
    affected = {
        chunk_id
        for chunk_id in set(before_by_id) | set(after_by_id)
        if before_by_id.get(chunk_id) != after_by_id.get(chunk_id)
    }
    assignment_count = sum(
        (
            None if before_by_id.get(chunk_id) is None
            else before_by_id[chunk_id].assignee
        )
        != (
            None if after_by_id.get(chunk_id) is None
            else after_by_id[chunk_id].assignee
        )
        for chunk_id in affected
    )
    if (
        operation.created_chunk_ids
        or operation.retired_chunk_ids
        or operation.created_chunk_count != 0
        or operation.retired_chunk_count != 0
        or set(operation.affected_chunk_ids) != affected
        or operation.affected_chunk_count != len(affected)
        or operation.affected_member_count
        != sum(len(chunk.members) for chunk in candidate.chunks)
        or operation.assignment_count != assignment_count
    ):
        _fail()
    return candidate


def validate_rebase_plan_successor(
    before: object,
    after: object,
    receipt: object,
) -> ChunkPlanSnapshot:
    """Prove an exact shrink-only membership rebase onto a new universe."""

    previous = validate_chunk_plan_snapshot(before)
    candidate = validate_chunk_plan_snapshot(after)
    operation = validate_chunk_operation_receipt(receipt)
    if (
        operation.action is not TopologyAction.REBASE
        or previous.project_id != candidate.project_id
        or previous.chunk_plan_id != candidate.chunk_plan_id
        or previous.schema_version != candidate.schema_version
        or previous.namespace != candidate.namespace
        or previous.segment_universe_digest == candidate.segment_universe_digest
        or candidate.revision != previous.revision + 1
        or operation.project_id != previous.project_id
        or operation.chunk_plan_id != previous.chunk_plan_id
        or operation.base_revision != previous.revision
        or operation.published_revision != candidate.revision
        or operation.before_plan_digest != chunk_plan_digest_v1(previous)
        or operation.after_plan_digest != chunk_plan_digest_v1(candidate)
        or candidate.audit_head_digest != operation.audit_record_digest
        or operation.created_chunk_ids
        or operation.created_chunk_count != 0
        or operation.assignment_count != 0
    ):
        _fail()
    before_by_id = {chunk.chunk_id: chunk for chunk in previous.chunks}
    after_by_id = {chunk.chunk_id: chunk for chunk in candidate.chunks}
    if not after_by_id or not set(after_by_id).issubset(before_by_id):
        _fail("CHUNK.REBASE_DECISION_REQUIRED")
    retired = set(before_by_id) - set(after_by_id)
    if retired != set(operation.retired_chunk_ids):
        _fail("CHUNK.REBASE_DECISION_REQUIRED")
    retained_order = [
        chunk.chunk_id for chunk in previous.chunks if chunk.chunk_id in after_by_id
    ]
    if retained_order != [chunk.chunk_id for chunk in candidate.chunks]:
        _fail()
    removed_member_count = 0
    for chunk_id, old in before_by_id.items():
        new = after_by_id.get(chunk_id)
        if new is None:
            removed_member_count += len(old.members)
            continue
        if (
            old.name != new.name
            or old.assignee != new.assignee
            or not set(new.members).issubset(old.members)
        ):
            _fail()
        removed_member_count += len(old.members) - len(new.members)
    if (
        operation.affected_chunk_ids != tuple(chunk.chunk_id for chunk in previous.chunks)
        or operation.affected_chunk_count != len(previous.chunks)
        or operation.affected_member_count != removed_member_count
    ):
        _fail()
    return candidate


@dataclass(frozen=True, slots=True)
class ChunkAuditRecord:
    schema: str
    previous_audit_head_digest: str
    outcome: str
    receipt: ChunkOperationReceipt
    record_digest: str

    def __post_init__(self) -> None:
        if self.schema != CHUNK_AUDIT_RECORD_SCHEMA:
            _fail("CHUNK.METADATA_UNSUPPORTED")
        previous = _validate_sha256(self.previous_audit_head_digest)
        if self.outcome != "published":
            _fail("CHUNK.METADATA_INVALID")
        receipt = validate_chunk_operation_receipt(self.receipt)
        if receipt.truncated:
            _fail("CHUNK.METADATA_INVALID")
        digest = _validate_sha256(self.record_digest)
        if receipt.audit_record_digest != digest:
            _fail("CHUNK.DIGEST_MISMATCH")
        if chunk_audit_record_digest_from_receipt_v1(receipt, previous) != digest:
            _fail("CHUNK.DIGEST_MISMATCH")


def validate_chunk_audit_record(record: object) -> ChunkAuditRecord:
    if type(record) is not ChunkAuditRecord:
        _fail()
    record.__post_init__()
    return record


def validate_c1_audit_record(record: object) -> ChunkAuditRecord:
    record = validate_chunk_audit_record(record)
    validate_c1_operation_receipt(record.receipt)
    return record


def chunk_operation_audit_digest_v1(
    preview: object,
    actor_ref: object,
    previous_audit_head_digest: object,
) -> str:
    """Bind a successful topology operation to the previous audit head."""

    validated = validate_chunk_mutation_preview(preview)
    if validated.truncated:
        # A public truncated projection is not an audit authority.  A later
        # profile that can exceed this domain's ID bound must hash the owner-
        # private full operation facts instead.
        _fail("CHUNK.CONTRACT_INVALID")
    if type(actor_ref) is not AssigneeRef:
        _fail()
    actor_ref.__post_init__()
    previous = _validate_sha256(previous_audit_head_digest)
    semantic = {
        "action": validated.action.value,
        "actor_ref": {
            "authority_id": actor_ref.authority_id,
            "subject_id": actor_ref.subject_id,
        },
        "affected_chunk_count": validated.affected_chunk_count,
        "affected_chunk_ids": list(validated.affected_chunk_ids),
        "affected_member_count": validated.affected_member_count,
        "after_plan_digest": validated.after_plan_digest,
        "assignment_count": validated.assignment_count,
        "base_revision": validated.base_revision,
        "before_plan_digest": validated.before_plan_digest,
        "blockers": list(validated.blockers),
        "chunk_plan_id": validated.chunk_plan_id,
        "created_chunk_count": validated.created_chunk_count,
        "created_chunk_ids": list(validated.created_chunk_ids),
        "operation_id": validated.operation_id,
        "outcome": "published",
        "project_id": validated.project_id,
        "published_revision": validated.published_revision,
        "retired_chunk_count": validated.retired_chunk_count,
        "retired_chunk_ids": list(validated.retired_chunk_ids),
        "truncated": validated.truncated,
        "warnings": list(validated.warnings),
    }
    encoded = json.dumps(
        semantic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(
        b"localcat.chunk.audit-record.v1\0"
        + bytes.fromhex(previous)
        + encoded
    ).hexdigest()


def chunk_audit_record_digest_from_receipt_v1(
    receipt: object,
    previous_audit_head_digest: object,
) -> str:
    """Recompute one successful, complete C1 audit record from its receipt."""

    validated = validate_chunk_operation_receipt(receipt)
    if validated.truncated:
        _fail("CHUNK.METADATA_INVALID")
    preview = ChunkMutationPreview(
        operation_id=validated.operation_id,
        action=validated.action,
        project_id=validated.project_id,
        chunk_plan_id=validated.chunk_plan_id,
        base_revision=validated.base_revision,
        published_revision=validated.published_revision,
        before_plan_digest=validated.before_plan_digest,
        after_plan_digest=validated.after_plan_digest,
        affected_chunk_ids=tuple(validated.affected_chunk_ids),
        created_chunk_ids=tuple(validated.created_chunk_ids),
        retired_chunk_ids=tuple(validated.retired_chunk_ids),
        affected_chunk_count=validated.affected_chunk_count,
        created_chunk_count=validated.created_chunk_count,
        retired_chunk_count=validated.retired_chunk_count,
        affected_member_count=validated.affected_member_count,
        assignment_count=validated.assignment_count,
        warnings=tuple(validated.safe_issues),
        blockers=(),
        truncated=False,
    )
    return chunk_operation_audit_digest_v1(
        preview,
        validated.actor_ref,
        previous_audit_head_digest,
    )


@dataclass(frozen=True, slots=True)
class LocalReferenceManagerHandle:
    """Honest current-source identity marker, not account authentication."""

    authority_id: str
    subject_id: str
    is_account_authenticated: bool = False

    def __post_init__(self) -> None:
        AssigneeRef(self.authority_id, self.subject_id)
        if _exact_bool(self.is_account_authenticated):
            _fail("CHUNK.ACTOR_UNVERIFIED")

    @property
    def actor_ref(self) -> AssigneeRef:
        return AssigneeRef(self.authority_id, self.subject_id)
