"""Strict canonical codec and rooted durable store for Chunk metadata v1.

This module owns only the independent Chunk metadata envelope, lifecycle
ledger and local journal/LKG transaction.  It deliberately does not import
ProjectPackage, workspace save, Parser, TM, ResourcePackage, Qt or providers.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from threading import Lock
from typing import Iterator
import unicodedata

from collaborative_chunk_contracts import (
    CHUNK_AUDIT_RECORD_SCHEMA,
    CHUNK_JOURNAL_SCHEMA,
    CHUNK_LIFECYCLE_SCHEMA,
    CHUNK_METADATA_NAMESPACE,
    CHUNK_METADATA_SCHEMA,
    CHUNK_REBASE_INTENT_SCHEMA,
    CHUNK_STORE_SCHEMA,
    DISSOLVED_CHUNK_PLAN_DIGEST,
    EMPTY_CHUNK_AUDIT_DIGEST,
    MAX_AUDIT_RECORDS,
    MAX_JSON_NESTING_DEPTH,
    MAX_METADATA_BYTES,
    MAX_REBASE_INTENT_BYTES,
    MAX_ACTIVE_MEMBERS,
    AssigneeRef,
    ChunkAuditRecord,
    ChunkError,
    ChunkOperationReceipt,
    ChunkPlanSnapshot,
    ChunkPlanBinding,
    ChunkPublishedUniverseBinding,
    ChunkPublishedUniverseProjection,
    ChunkPublishedWorkspaceTransition,
    ChunkRebaseIntent,
    ChunkSegmentRef,
    ChunkUniverseEntry,
    CollaborativeChunk,
    SourcePresence,
    TopologyAction,
    chunk_audit_record_digest_from_receipt_v1,
    chunk_plan_binding,
    chunk_published_workspace_transition_digest_v1,
    chunk_rebase_intent_digest_v1,
    chunk_plan_digest_v1,
    chunk_segment_ref_from_ids,
    validate_chunk_audit_record,
    validate_chunk_operation_receipt,
    validate_chunk_rebase_intent,
    validate_chunk_plan_snapshot,
    validate_chunk_id,
    validate_assignment_plan_successor,
    validate_conflict_replace_plan_successor,
    validate_undo_plan_successor,
    validate_rebase_plan_successor,
    validate_topology_assignment_successor,
    validate_chunk_plan_id,
    validate_chunk_project_id,
)


_STORE_DIGEST_DOMAIN = b"localcat.chunk.store-envelope.v1\0"
_JOURNAL_PHASES = frozenset({"PREPARED", "TARGET_REPLACED"})
_MAX_FILENAME_BYTES = 255
_READ_CHUNK = 1024 * 1024


def _fail(code: str, *, retryable: bool = False) -> None:
    raise ChunkError(code, retryable=retryable)


def _canonical_json(value: object) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ChunkError("CHUNK.METADATA_INVALID") from error
    if len(payload) > MAX_METADATA_BYTES:
        _fail("CHUNK.LIMIT_EXCEEDED")
    return payload


def _json_depth(value: object, depth: int = 1) -> int:
    if depth > MAX_JSON_NESTING_DEPTH:
        _fail("CHUNK.LIMIT_EXCEEDED")
    if type(value) is dict:
        for item in value.values():
            _json_depth(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _json_depth(item, depth + 1)
    return depth


def _decode_canonical_json(payload: bytes) -> object:
    if type(payload) is not bytes:
        raise TypeError("Chunk metadata payload must be exact bytes")
    if len(payload) > MAX_METADATA_BYTES:
        _fail("CHUNK.LIMIT_EXCEEDED")
    duplicate = False

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate
        result: dict[str, object] = {}
        for key, value in values:
            if type(key) is not str or key in result:
                duplicate = True
            result[key] = value
        return result

    def reject_number(_value: str) -> object:
        raise ValueError("non-integer JSON number")

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise ChunkError("CHUNK.METADATA_INVALID") from error
    _json_depth(value)
    if duplicate or _canonical_json(value) != payload:
        _fail("CHUNK.METADATA_INVALID")
    return value


def _expect_object(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys:
        _fail("CHUNK.METADATA_INVALID")
    return value


def _expect_list(value: object, *, maximum: int | None) -> list[object]:
    if type(value) is not list:
        _fail("CHUNK.METADATA_INVALID")
    if maximum is not None and len(value) > maximum:
        _fail("CHUNK.LIMIT_EXCEEDED")
    return value


def _expect_int(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail("CHUNK.METADATA_INVALID")
    return value


def _expect_string(value: object) -> str:
    if type(value) is not str:
        _fail("CHUNK.METADATA_INVALID")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("CHUNK.METADATA_INVALID")
    if any(unicodedata.category(character) == "Cs" for character in value):
        _fail("CHUNK.METADATA_INVALID")
    return value


def _expect_digest(value: object) -> str:
    digest = _expect_string(value)
    try:
        raw = bytes.fromhex(digest)
    except ValueError:
        _fail("CHUNK.METADATA_INVALID")
    if len(digest) != 64 or len(raw) != 32 or digest != digest.casefold():
        _fail("CHUNK.METADATA_INVALID")
    return digest


def _canonical_rebase_intent_json(value: object) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ChunkError("CHUNK.METADATA_INVALID") from error
    if len(payload) > MAX_REBASE_INTENT_BYTES:
        _fail("CHUNK.LIMIT_EXCEEDED")
    return payload


def _decode_rebase_intent_json(payload: bytes) -> object:
    if type(payload) is not bytes:
        raise TypeError("Chunk rebase intent payload must be exact bytes")
    if len(payload) > MAX_REBASE_INTENT_BYTES:
        _fail("CHUNK.LIMIT_EXCEEDED")
    duplicate = False

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate
        result: dict[str, object] = {}
        for key, value in values:
            if type(key) is not str or key in result:
                duplicate = True
            result[key] = value
        return result

    def reject_number(_value: str) -> object:
        raise ValueError("non-integer JSON number")

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise ChunkError("CHUNK.METADATA_INVALID") from error
    _json_depth(value)
    if duplicate or _canonical_rebase_intent_json(value) != payload:
        _fail("CHUNK.METADATA_INVALID")
    return value


def _rebase_binding_to_value(binding: ChunkPublishedUniverseBinding) -> dict[str, object]:
    binding.__post_init__()
    return {
        "project_id": binding.project_id,
        "segment_universe_digest": binding.segment_universe_digest,
        "workspace_digest": binding.workspace_digest,
        "workspace_revision": binding.workspace_revision,
        "workspace_composition_revision": binding.workspace_composition_revision,
        "workspace_session_id": binding.workspace_session_id,
    }


def _rebase_binding_from_value(value: object) -> ChunkPublishedUniverseBinding:
    root = _expect_object(
        value,
        frozenset(
            {
                "project_id",
                "segment_universe_digest",
                "workspace_digest",
                "workspace_revision",
                "workspace_composition_revision",
                "workspace_session_id",
            }
        ),
    )
    return ChunkPublishedUniverseBinding(
        project_id=_expect_string(root["project_id"]),
        workspace_session_id=_expect_string(root["workspace_session_id"]),
        workspace_revision=_expect_int(root["workspace_revision"]),
        workspace_composition_revision=_expect_int(
            root["workspace_composition_revision"]
        ),
        workspace_digest=_expect_digest(root["workspace_digest"]),
        segment_universe_digest=_expect_digest(root["segment_universe_digest"]),
    )


def _rebase_universe_to_value(
    projection: ChunkPublishedUniverseProjection,
) -> dict[str, object]:
    projection.__post_init__()
    return {
        "binding": _rebase_binding_to_value(projection.binding),
        "entries": [
            [
                entry.segment.identity.document_id,
                entry.segment.identity.local_segment_id,
                entry.source_presence.value,
            ]
            for entry in projection.entries
        ],
    }


def _rebase_universe_from_value(
    value: object,
    *,
    project_id: str,
) -> ChunkPublishedUniverseProjection:
    root = _expect_object(value, frozenset({"binding", "entries"}))
    entries = []
    for raw in _expect_list(root["entries"], maximum=MAX_ACTIVE_MEMBERS):
        values = _expect_list(raw, maximum=3)
        if len(values) != 3:
            _fail("CHUNK.METADATA_INVALID")
        try:
            presence = SourcePresence(_expect_string(values[2]))
        except ValueError:
            _fail("CHUNK.METADATA_INVALID")
        entries.append(
            ChunkUniverseEntry(
                segment=chunk_segment_ref_from_ids(
                    project_id,
                    _expect_string(values[0]),
                    _expect_string(values[1]),
                ),
                source_presence=presence,
            )
        )
    return ChunkPublishedUniverseProjection(
        binding=_rebase_binding_from_value(root["binding"]),
        entries=tuple(entries),
    )


def _rebase_intent_to_value(intent: ChunkRebaseIntent) -> dict[str, object]:
    validated = validate_chunk_rebase_intent(intent)
    binding = validated.plan_binding
    transition = validated.transition
    current_indices = {
        entry.segment: index
        for index, entry in enumerate(transition.current.entries)
    }
    return {
        "intent_digest": validated.intent_digest,
        "plan_binding": {
            "chunk_plan_id": binding.chunk_plan_id,
            "plan_digest": binding.plan_digest,
            "plan_revision": binding.plan_revision,
            "project_id": binding.project_id,
            "segment_universe_digest": binding.segment_universe_digest,
        },
        "project_id": validated.project_id,
        "schema": CHUNK_REBASE_INTENT_SCHEMA,
        "transition": {
            "current": _rebase_universe_to_value(transition.current),
            "operation_id": transition.operation_id,
            "previous": _rebase_universe_to_value(transition.previous),
            # Indices avoid a third copy of maximum-length Segment IDs.  The
            # transition contract already requires every changed member to be
            # present in the canonical current universe.
            "source_changed_indices": [
                current_indices[member]
                for member in transition.source_changed_members
            ],
            "transition_digest": transition.transition_digest,
        },
    }


def encode_chunk_rebase_intent(intent: ChunkRebaseIntent) -> bytes:
    return _canonical_rebase_intent_json(_rebase_intent_to_value(intent))


def decode_chunk_rebase_intent(payload: bytes) -> ChunkRebaseIntent:
    root = _expect_object(
        _decode_rebase_intent_json(payload),
        frozenset(
            {"intent_digest", "plan_binding", "project_id", "schema", "transition"}
        ),
    )
    if root["schema"] != CHUNK_REBASE_INTENT_SCHEMA:
        _fail("CHUNK.METADATA_UNSUPPORTED")
    project_id = _expect_string(root["project_id"])
    plan_value = _expect_object(
        root["plan_binding"],
        frozenset(
            {
                "chunk_plan_id",
                "plan_digest",
                "plan_revision",
                "project_id",
                "segment_universe_digest",
            }
        ),
    )
    plan_binding = ChunkPlanBinding(
        project_id=_expect_string(plan_value["project_id"]),
        chunk_plan_id=_expect_string(plan_value["chunk_plan_id"]),
        plan_revision=_expect_int(plan_value["plan_revision"], minimum=1),
        plan_digest=_expect_digest(plan_value["plan_digest"]),
        segment_universe_digest=_expect_digest(
            plan_value["segment_universe_digest"]
        ),
    )
    transition_value = _expect_object(
        root["transition"],
        frozenset(
            {
                "current",
                "operation_id",
                "previous",
                "source_changed_indices",
                "transition_digest",
            }
        ),
    )
    previous = _rebase_universe_from_value(
        transition_value["previous"],
        project_id=project_id,
    )
    current = _rebase_universe_from_value(
        transition_value["current"],
        project_id=project_id,
    )
    changed = []
    seen_changed_indices: set[int] = set()
    for raw in _expect_list(
        transition_value["source_changed_indices"],
        maximum=MAX_ACTIVE_MEMBERS,
    ):
        index = _expect_int(raw)
        if index >= len(current.entries) or index in seen_changed_indices:
            _fail("CHUNK.METADATA_INVALID")
        seen_changed_indices.add(index)
        changed.append(current.entries[index].segment)
    transition = ChunkPublishedWorkspaceTransition(
        operation_id=_expect_string(transition_value["operation_id"]),
        previous=previous,
        current=current,
        source_changed_members=tuple(changed),
        transition_digest=_expect_digest(transition_value["transition_digest"]),
    )
    intent = ChunkRebaseIntent(
        schema=CHUNK_REBASE_INTENT_SCHEMA,
        project_id=project_id,
        plan_binding=plan_binding,
        transition=transition,
        intent_digest=_expect_digest(root["intent_digest"]),
    )
    if encode_chunk_rebase_intent(intent) != payload:
        _fail("CHUNK.METADATA_INVALID")
    return validate_chunk_rebase_intent(intent)


def _snapshot_to_value(snapshot: ChunkPlanSnapshot) -> dict[str, object]:
    validated = validate_chunk_plan_snapshot(snapshot)
    if not validated.chunks:
        _fail("CHUNK.METADATA_INVALID")
    return {
        "audit_head": validated.audit_head_digest,
        "chunk_plan_id": validated.chunk_plan_id,
        "chunks": [
            {
                "assignee": (
                    None
                    if chunk.assignee is None
                    else {
                        "authority_id": chunk.assignee.authority_id,
                        "subject_id": chunk.assignee.subject_id,
                    }
                ),
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
            for chunk in validated.chunks
        ],
        "namespace": CHUNK_METADATA_NAMESPACE,
        "project_id": validated.project_id,
        "revision": validated.revision,
        "schema": CHUNK_METADATA_SCHEMA,
        "segment_universe_digest": validated.segment_universe_digest,
    }


def _snapshot_from_value(value: object) -> ChunkPlanSnapshot:
    root = _expect_object(
        value,
        frozenset(
            {
                "audit_head",
                "chunk_plan_id",
                "chunks",
                "namespace",
                "project_id",
                "revision",
                "schema",
                "segment_universe_digest",
            }
        ),
    )
    if root["schema"] != CHUNK_METADATA_SCHEMA:
        _fail("CHUNK.METADATA_UNSUPPORTED")
    if root["namespace"] != CHUNK_METADATA_NAMESPACE:
        _fail("CHUNK.METADATA_UNSUPPORTED")
    project_id = _expect_string(root["project_id"])
    chunks_value = _expect_list(root["chunks"], maximum=4_096)
    if not chunks_value:
        _fail("CHUNK.METADATA_INVALID")
    chunks: list[CollaborativeChunk] = []
    for chunk_value in chunks_value:
        chunk = _expect_object(
            chunk_value,
            frozenset({"assignee", "chunk_id", "members", "name", "order"}),
        )
        assignee_value = chunk["assignee"]
        assignee = None
        if assignee_value is not None:
            assignee_object = _expect_object(
                assignee_value,
                frozenset({"authority_id", "subject_id"}),
            )
            assignee = AssigneeRef(
                authority_id=_expect_string(assignee_object["authority_id"]),
                subject_id=_expect_string(assignee_object["subject_id"]),
            )
        members_value = _expect_list(chunk["members"], maximum=100_000)
        if not members_value:
            _fail("CHUNK.METADATA_INVALID")
        members = []
        for member_value in members_value:
            member = _expect_object(
                member_value,
                frozenset({"document_id", "local_segment_id"}),
            )
            members.append(
                chunk_segment_ref_from_ids(
                    project_id,
                    _expect_string(member["document_id"]),
                    _expect_string(member["local_segment_id"]),
                )
            )
        chunks.append(
            CollaborativeChunk(
                chunk_id=_expect_string(chunk["chunk_id"]),
                name=_expect_string(chunk["name"]),
                order=_expect_int(chunk["order"]),
                members=tuple(members),
                assignee=assignee,
            )
        )
    return validate_chunk_plan_snapshot(
        ChunkPlanSnapshot(
            schema_version=1,
            namespace=CHUNK_METADATA_NAMESPACE,
            chunk_plan_id=_expect_string(root["chunk_plan_id"]),
            project_id=project_id,
            revision=_expect_int(root["revision"], minimum=1),
            segment_universe_digest=_expect_string(
                root["segment_universe_digest"]
            ),
            chunks=tuple(chunks),
            audit_head_digest=_expect_string(root["audit_head"]),
        )
    )


def _receipt_to_value(receipt: ChunkOperationReceipt) -> dict[str, object]:
    value = validate_chunk_operation_receipt(receipt)
    if value.truncated:
        _fail("CHUNK.METADATA_INVALID")
    return {
        "action": value.action.value,
        "actor_ref": {
            "authority_id": value.actor_ref.authority_id,
            "subject_id": value.actor_ref.subject_id,
        },
        "affected_chunk_count": value.affected_chunk_count,
        "affected_chunk_ids": list(value.affected_chunk_ids),
        "affected_member_count": value.affected_member_count,
        "after_plan_digest": value.after_plan_digest,
        "assignment_count": value.assignment_count,
        "audit_record_digest": value.audit_record_digest,
        "base_revision": value.base_revision,
        "before_plan_digest": value.before_plan_digest,
        "chunk_plan_id": value.chunk_plan_id,
        "created_chunk_count": value.created_chunk_count,
        "created_chunk_ids": list(value.created_chunk_ids),
        "operation_id": value.operation_id,
        "project_id": value.project_id,
        "published_revision": value.published_revision,
        "retired_chunk_count": value.retired_chunk_count,
        "retired_chunk_ids": list(value.retired_chunk_ids),
        "safe_issues": list(value.safe_issues),
        "truncated": False,
    }


_RECEIPT_KEYS = frozenset(
    {
        "action",
        "actor_ref",
        "affected_chunk_count",
        "affected_chunk_ids",
        "affected_member_count",
        "after_plan_digest",
        "assignment_count",
        "audit_record_digest",
        "base_revision",
        "before_plan_digest",
        "chunk_plan_id",
        "created_chunk_count",
        "created_chunk_ids",
        "operation_id",
        "project_id",
        "published_revision",
        "retired_chunk_count",
        "retired_chunk_ids",
        "safe_issues",
        "truncated",
    }
)


def _string_tuple(value: object, *, maximum: int | None) -> tuple[str, ...]:
    return tuple(_expect_string(item) for item in _expect_list(value, maximum=maximum))


def _receipt_from_value(value: object) -> ChunkOperationReceipt:
    receipt = _expect_object(value, _RECEIPT_KEYS)
    actor = _expect_object(
        receipt["actor_ref"],
        frozenset({"authority_id", "subject_id"}),
    )
    try:
        action = TopologyAction(_expect_string(receipt["action"]))
    except ValueError:
        _fail("CHUNK.METADATA_INVALID")
    if receipt["truncated"] is not False:
        _fail("CHUNK.METADATA_INVALID")
    return validate_chunk_operation_receipt(
        ChunkOperationReceipt(
            operation_id=_expect_string(receipt["operation_id"]),
            action=action,
            project_id=_expect_string(receipt["project_id"]),
            chunk_plan_id=_expect_string(receipt["chunk_plan_id"]),
            base_revision=_expect_int(receipt["base_revision"]),
            published_revision=_expect_int(
                receipt["published_revision"], minimum=1
            ),
            before_plan_digest=_expect_string(receipt["before_plan_digest"]),
            after_plan_digest=_expect_string(receipt["after_plan_digest"]),
            affected_chunk_ids=_string_tuple(
                receipt["affected_chunk_ids"], maximum=100_000
            ),
            created_chunk_ids=_string_tuple(
                receipt["created_chunk_ids"], maximum=100_000
            ),
            retired_chunk_ids=_string_tuple(
                receipt["retired_chunk_ids"], maximum=100_000
            ),
            affected_chunk_count=_expect_int(receipt["affected_chunk_count"]),
            created_chunk_count=_expect_int(receipt["created_chunk_count"]),
            retired_chunk_count=_expect_int(receipt["retired_chunk_count"]),
            affected_member_count=_expect_int(receipt["affected_member_count"]),
            assignment_count=_expect_int(receipt["assignment_count"]),
            actor_ref=AssigneeRef(
                _expect_string(actor["authority_id"]),
                _expect_string(actor["subject_id"]),
            ),
            safe_issues=_string_tuple(receipt["safe_issues"], maximum=256),
            truncated=False,
            audit_record_digest=_expect_string(receipt["audit_record_digest"]),
        )
    )


def _audit_to_value(record: ChunkAuditRecord) -> dict[str, object]:
    validated = validate_chunk_audit_record(record)
    return {
        "outcome": "published",
        "previous_audit_head": validated.previous_audit_head_digest,
        "receipt": _receipt_to_value(validated.receipt),
        "record_digest": validated.record_digest,
        "schema": CHUNK_AUDIT_RECORD_SCHEMA,
    }


def _audit_from_value(value: object) -> ChunkAuditRecord:
    record = _expect_object(
        value,
        frozenset(
            {"outcome", "previous_audit_head", "receipt", "record_digest", "schema"}
        ),
    )
    if record["schema"] != CHUNK_AUDIT_RECORD_SCHEMA:
        _fail("CHUNK.METADATA_UNSUPPORTED")
    if record["outcome"] != "published":
        _fail("CHUNK.METADATA_INVALID")
    return validate_chunk_audit_record(
        ChunkAuditRecord(
            schema=CHUNK_AUDIT_RECORD_SCHEMA,
            previous_audit_head_digest=_expect_string(
                record["previous_audit_head"]
            ),
            outcome="published",
            receipt=_receipt_from_value(record["receipt"]),
            record_digest=_expect_string(record["record_digest"]),
        )
    )


@dataclass(frozen=True, slots=True)
class ChunkMetadataState:
    project_id: str
    active_snapshot: ChunkPlanSnapshot | None
    retired_chunk_ids: tuple[str, ...]
    used_chunk_plan_ids: tuple[str, ...]
    audit_records: tuple[ChunkAuditRecord, ...]
    head_previous_active_snapshot: ChunkPlanSnapshot | None

    def __post_init__(self) -> None:
        validate_chunk_metadata_state(self)


def validate_chunk_metadata_state(state: object) -> ChunkMetadataState:
    if type(state) is not ChunkMetadataState:
        _fail("CHUNK.METADATA_INVALID")
    project_id = validate_chunk_project_id(state.project_id)
    active = state.active_snapshot
    if active is not None:
        active = validate_chunk_plan_snapshot(active)
        if not active.chunks or active.project_id != project_id:
            _fail("CHUNK.METADATA_INVALID")
    previous = state.head_previous_active_snapshot
    if previous is not None:
        previous = validate_chunk_plan_snapshot(previous)
        if not previous.chunks or previous.project_id != project_id:
            _fail("CHUNK.METADATA_INVALID")
    if type(state.retired_chunk_ids) is not tuple:
        _fail("CHUNK.METADATA_INVALID")
    retired = tuple(validate_chunk_id(value) for value in state.retired_chunk_ids)
    if tuple(sorted(retired)) != retired or len(retired) != len(set(retired)):
        _fail("CHUNK.METADATA_INVALID")
    if type(state.used_chunk_plan_ids) is not tuple:
        _fail("CHUNK.METADATA_INVALID")
    used = tuple(validate_chunk_plan_id(value) for value in state.used_chunk_plan_ids)
    if tuple(sorted(used)) != used or len(used) != len(set(used)):
        _fail("CHUNK.METADATA_INVALID")
    if type(state.audit_records) is not tuple:
        _fail("CHUNK.METADATA_INVALID")
    if not state.audit_records:
        _fail("CHUNK.METADATA_INVALID")
    if len(state.audit_records) > MAX_AUDIT_RECORDS:
        _fail("CHUNK.LIMIT_EXCEEDED")

    seen_operations: set[str] = set()
    seen_plans: set[str] = set()
    retired_from_audit: set[str] = set()
    last_plan: str | None = None
    last_revision = 0
    last_head = EMPTY_CHUNK_AUDIT_DIGEST
    last_after_digest = EMPTY_CHUNK_AUDIT_DIGEST
    last_was_dissolve = False
    for raw_record in state.audit_records:
        record = validate_chunk_audit_record(raw_record)
        receipt = record.receipt
        if receipt.project_id != project_id:
            _fail("CHUNK.METADATA_INVALID")
        if receipt.operation_id in seen_operations:
            _fail("CHUNK.METADATA_INVALID")
        seen_operations.add(receipt.operation_id)
        if receipt.chunk_plan_id != last_plan:
            if last_plan is not None and not last_was_dissolve:
                _fail("CHUNK.METADATA_INVALID")
            if receipt.chunk_plan_id in seen_plans:
                _fail("CHUNK.METADATA_INVALID")
            seen_plans.add(receipt.chunk_plan_id)
            if (
                receipt.action is not TopologyAction.CREATE
                or
                receipt.base_revision != 0
                or receipt.published_revision != 1
                or receipt.before_plan_digest != EMPTY_CHUNK_AUDIT_DIGEST
                or record.previous_audit_head_digest
                != EMPTY_CHUNK_AUDIT_DIGEST
            ):
                _fail("CHUNK.METADATA_INVALID")
            last_plan = receipt.chunk_plan_id
        else:
            if last_was_dissolve:
                _fail("CHUNK.METADATA_INVALID")
            if (
                receipt.base_revision != last_revision
                or receipt.published_revision != last_revision + 1
                or record.previous_audit_head_digest != last_head
                or receipt.before_plan_digest != last_after_digest
            ):
                _fail("CHUNK.METADATA_INVALID")
        if receipt.published_revision != receipt.base_revision + 1:
            _fail("CHUNK.METADATA_INVALID")
        if record.record_digest != receipt.audit_record_digest:
            _fail("CHUNK.DIGEST_MISMATCH")
        if retired_from_audit & set(receipt.retired_chunk_ids):
            _fail("CHUNK.METADATA_INVALID")
        retired_from_audit.update(receipt.retired_chunk_ids)
        last_revision = receipt.published_revision
        last_head = receipt.audit_record_digest
        last_after_digest = receipt.after_plan_digest
        last_was_dissolve = receipt.action is TopologyAction.DISSOLVE_PLAN
        if last_was_dissolve and receipt.after_plan_digest != DISSOLVED_CHUNK_PLAN_DIGEST:
            _fail("CHUNK.METADATA_INVALID")
        if not last_was_dissolve and receipt.after_plan_digest == DISSOLVED_CHUNK_PLAN_DIGEST:
            _fail("CHUNK.METADATA_INVALID")

    if frozenset(used) != seen_plans:
        _fail("CHUNK.METADATA_INVALID")
    if frozenset(retired) != retired_from_audit:
        _fail("CHUNK.METADATA_INVALID")
    if active is None:
        if not last_was_dissolve:
            _fail("CHUNK.METADATA_INVALID")
    else:
        final = state.audit_records[-1].receipt
        if (
            last_was_dissolve
            or active.chunk_plan_id != final.chunk_plan_id
            or active.revision != final.published_revision
            or active.audit_head_digest != final.audit_record_digest
            or chunk_plan_digest_v1(active) != final.after_plan_digest
            or any(chunk.chunk_id in retired_from_audit for chunk in active.chunks)
        ):
            _fail("CHUNK.METADATA_INVALID")

    final = state.audit_records[-1]
    receipt = final.receipt
    if receipt.base_revision == 0:
        if previous is not None or receipt.before_plan_digest != EMPTY_CHUNK_AUDIT_DIGEST:
            _fail("CHUNK.METADATA_INVALID")
    else:
        if previous is None:
            _fail("CHUNK.METADATA_INVALID")
        assert previous is not None
        if (
            previous.chunk_plan_id != receipt.chunk_plan_id
            or previous.revision != receipt.base_revision
            or previous.audit_head_digest != final.previous_audit_head_digest
            or chunk_plan_digest_v1(previous) != receipt.before_plan_digest
        ):
            _fail("CHUNK.METADATA_INVALID")
    return state


def build_next_chunk_metadata_state(
    current: ChunkMetadataState | None,
    *,
    project_id: str,
    active_snapshot: ChunkPlanSnapshot | None,
    retired_chunk_ids: tuple[str, ...],
    used_chunk_plan_ids: tuple[str, ...],
    receipt: ChunkOperationReceipt,
) -> ChunkMetadataState:
    project = validate_chunk_project_id(project_id)
    if current is not None:
        current = validate_chunk_metadata_state(current)
        if current.project_id != project:
            _fail("CHUNK.IDENTITY_FOREIGN")
        records = current.audit_records
        previous_active = current.active_snapshot
    else:
        records = ()
        previous_active = None
    validated_receipt = validate_chunk_operation_receipt(receipt)
    previous_head = (
        EMPTY_CHUNK_AUDIT_DIGEST
        if previous_active is None
        else previous_active.audit_head_digest
    )
    expected_digest = chunk_audit_record_digest_from_receipt_v1(
        validated_receipt,
        previous_head,
    )
    if expected_digest != validated_receipt.audit_record_digest:
        _fail("CHUNK.DIGEST_MISMATCH")
    record = ChunkAuditRecord(
        schema=CHUNK_AUDIT_RECORD_SCHEMA,
        previous_audit_head_digest=previous_head,
        outcome="published",
        receipt=validated_receipt,
        record_digest=expected_digest,
    )
    return validate_chunk_metadata_state(
        ChunkMetadataState(
            project_id=project,
            active_snapshot=active_snapshot,
            retired_chunk_ids=tuple(sorted(retired_chunk_ids)),
            used_chunk_plan_ids=tuple(sorted(used_chunk_plan_ids)),
            audit_records=records + (record,),
            head_previous_active_snapshot=previous_active,
        )
    )


def validate_chunk_metadata_successor(
    current: ChunkMetadataState | None,
    candidate: ChunkMetadataState,
) -> ChunkMetadataState:
    """Prove one append-only semantic transition before any publication."""

    next_state = validate_chunk_metadata_state(candidate)
    if current is None:
        old_records: tuple[ChunkAuditRecord, ...] = ()
        old_retired: tuple[str, ...] = ()
        old_used: tuple[str, ...] = ()
        old_active = None
        project_id = next_state.project_id
    else:
        current = validate_chunk_metadata_state(current)
        project_id = current.project_id
        old_records = current.audit_records
        old_retired = current.retired_chunk_ids
        old_used = current.used_chunk_plan_ids
        old_active = current.active_snapshot
    if next_state.project_id != project_id:
        _fail("CHUNK.IDENTITY_FOREIGN")
    if (
        len(next_state.audit_records) != len(old_records) + 1
        or next_state.audit_records[:-1] != old_records
        or next_state.head_previous_active_snapshot != old_active
    ):
        _fail("CHUNK.METADATA_INVALID")
    record = next_state.audit_records[-1]
    receipt = record.receipt
    expected_previous_head = (
        EMPTY_CHUNK_AUDIT_DIGEST
        if old_active is None
        else old_active.audit_head_digest
    )
    expected_before_digest = (
        EMPTY_CHUNK_AUDIT_DIGEST
        if old_active is None
        else chunk_plan_digest_v1(old_active)
    )
    expected_base_revision = 0 if old_active is None else old_active.revision
    if (
        record.previous_audit_head_digest != expected_previous_head
        or receipt.before_plan_digest != expected_before_digest
        or receipt.base_revision != expected_base_revision
        or receipt.published_revision != expected_base_revision + 1
    ):
        _fail("CHUNK.METADATA_INVALID")
    if old_active is None:
        if (
            receipt.action is not TopologyAction.CREATE
            or receipt.chunk_plan_id in old_used
        ):
            _fail("CHUNK.METADATA_INVALID")
    elif receipt.chunk_plan_id != old_active.chunk_plan_id:
        _fail("CHUNK.METADATA_INVALID")
    expected_retired = frozenset(old_retired) | set(receipt.retired_chunk_ids)
    expected_used = frozenset(old_used) | {receipt.chunk_plan_id}
    if (
        frozenset(next_state.retired_chunk_ids) != expected_retired
        or frozenset(next_state.used_chunk_plan_ids) != expected_used
    ):
        _fail("CHUNK.METADATA_INVALID")
    active = next_state.active_snapshot
    if receipt.action in {
        TopologyAction.ASSIGN,
        TopologyAction.REASSIGN,
        TopologyAction.UNASSIGN,
    }:
        if old_active is None or active is None:
            _fail("CHUNK.METADATA_INVALID")
        validate_assignment_plan_successor(old_active, active, receipt)
    elif receipt.action is TopologyAction.REBASE:
        if old_active is None or active is None:
            _fail("CHUNK.METADATA_INVALID")
        validate_rebase_plan_successor(old_active, active, receipt)
    elif receipt.action is TopologyAction.CONFLICT_REPLACE:
        if old_active is None or active is None:
            _fail("CHUNK.METADATA_INVALID")
        validate_conflict_replace_plan_successor(old_active, active, receipt)
    elif receipt.action is TopologyAction.UNDO:
        if (
            current is None
            or old_active is None
            or active is None
            or current.head_previous_active_snapshot is None
        ):
            _fail("CHUNK.METADATA_INVALID")
        validate_undo_plan_successor(
            old_active,
            active,
            receipt,
            current.head_previous_active_snapshot,
        )
    else:
        validate_topology_assignment_successor(old_active, active, receipt)
    if receipt.action is TopologyAction.DISSOLVE_PLAN:
        if (
            old_active is None
            or active is not None
            or receipt.after_plan_digest != DISSOLVED_CHUNK_PLAN_DIGEST
            or set(receipt.retired_chunk_ids)
            != {chunk.chunk_id for chunk in old_active.chunks}
        ):
            _fail("CHUNK.METADATA_INVALID")
    else:
        if active is None:
            _fail("CHUNK.METADATA_INVALID")
        if (
            active.chunk_plan_id != receipt.chunk_plan_id
            or active.revision != receipt.published_revision
            or active.audit_head_digest != receipt.audit_record_digest
            or chunk_plan_digest_v1(active) != receipt.after_plan_digest
        ):
            _fail("CHUNK.METADATA_INVALID")
    return next_state


def _validate_rebase_candidate_against_intent(
    current: ChunkMetadataState | None,
    candidate: ChunkMetadataState,
    intent: ChunkRebaseIntent,
) -> None:
    if (
        current is None
        or current.active_snapshot is None
        or candidate.active_snapshot is None
        or chunk_plan_binding(current.active_snapshot) != intent.plan_binding
    ):
        _fail("CHUNK.REBASE_REQUIRED")
    before = current.active_snapshot
    after = candidate.active_snapshot
    if (
        after.segment_universe_digest
        != intent.transition.current.binding.segment_universe_digest
    ):
        _fail("CHUNK.UNIVERSE_MISMATCH")
    before_members = {
        member for chunk in before.chunks for member in chunk.members
    }
    after_members = {
        member for chunk in after.chunks for member in chunk.members
    }
    current_universe = {
        entry.segment for entry in intent.transition.current.entries
    }
    expected_retained = before_members & current_universe
    if after_members != expected_retained:
        _fail("CHUNK.REBASE_DECISION_REQUIRED")


def _state_to_value(state: ChunkMetadataState) -> dict[str, object]:
    validated = validate_chunk_metadata_state(state)
    return {
        "active_metadata": (
            None
            if validated.active_snapshot is None
            else _snapshot_to_value(validated.active_snapshot)
        ),
        "lifecycle": {
            "audit_records": [
                _audit_to_value(record) for record in validated.audit_records
            ],
            "head_previous_active_metadata": (
                None
                if validated.head_previous_active_snapshot is None
                else _snapshot_to_value(validated.head_previous_active_snapshot)
            ),
            "project_id": validated.project_id,
            "retired_chunk_ids": list(validated.retired_chunk_ids),
            "schema": CHUNK_LIFECYCLE_SCHEMA,
            "used_chunk_plan_ids": list(validated.used_chunk_plan_ids),
        },
        "schema": CHUNK_STORE_SCHEMA,
    }


def encode_chunk_metadata_state(state: ChunkMetadataState) -> bytes:
    return _canonical_json(_state_to_value(state))


def decode_chunk_metadata_state(payload: bytes) -> ChunkMetadataState:
    try:
        root = _expect_object(
            _decode_canonical_json(payload),
            frozenset({"active_metadata", "lifecycle", "schema"}),
        )
        if root["schema"] != CHUNK_STORE_SCHEMA:
            _fail("CHUNK.METADATA_UNSUPPORTED")
        lifecycle = _expect_object(
            root["lifecycle"],
            frozenset(
                {
                    "audit_records",
                    "head_previous_active_metadata",
                    "project_id",
                    "retired_chunk_ids",
                    "schema",
                    "used_chunk_plan_ids",
                }
            ),
        )
        if lifecycle["schema"] != CHUNK_LIFECYCLE_SCHEMA:
            _fail("CHUNK.METADATA_UNSUPPORTED")
        active_value = root["active_metadata"]
        previous_value = lifecycle["head_previous_active_metadata"]
        state = ChunkMetadataState(
            project_id=_expect_string(lifecycle["project_id"]),
            active_snapshot=(
                None if active_value is None else _snapshot_from_value(active_value)
            ),
            retired_chunk_ids=_string_tuple(
                lifecycle["retired_chunk_ids"], maximum=None
            ),
            used_chunk_plan_ids=_string_tuple(
                lifecycle["used_chunk_plan_ids"], maximum=MAX_AUDIT_RECORDS
            ),
            audit_records=tuple(
                _audit_from_value(value)
                for value in _expect_list(
                    lifecycle["audit_records"], maximum=MAX_AUDIT_RECORDS
                )
            ),
            head_previous_active_snapshot=(
                None
                if previous_value is None
                else _snapshot_from_value(previous_value)
            ),
        )
        validate_chunk_metadata_state(state)
        if encode_chunk_metadata_state(state) != payload:
            _fail("CHUNK.METADATA_INVALID")
        return state
    except ChunkError as error:
        if error.code in {
            "CHUNK.LIMIT_EXCEEDED",
            "CHUNK.METADATA_UNSUPPORTED",
            "CHUNK.DIGEST_MISMATCH",
        }:
            raise
        raise ChunkError("CHUNK.METADATA_INVALID") from None


def chunk_metadata_state_digest_v1(state: ChunkMetadataState) -> str:
    return hashlib.sha256(
        _STORE_DIGEST_DOMAIN + encode_chunk_metadata_state(state)
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkMetadataPublicationResult:
    state: ChunkMetadataState
    metadata_digest: str
    previous_metadata_digest: str | None
    lkg_was_present: bool

    def __post_init__(self) -> None:
        validate_chunk_metadata_state(self.state)
        if chunk_metadata_state_digest_v1(self.state) != self.metadata_digest:
            _fail("CHUNK.DIGEST_MISMATCH")
        if self.previous_metadata_digest is not None:
            _expect_digest(self.previous_metadata_digest)
        if type(self.lkg_was_present) is not bool:
            _fail("CHUNK.METADATA_INVALID")


@dataclass(frozen=True, slots=True)
class ChunkMetadataRecoveryReport:
    outcome: str
    state: ChunkMetadataState | None
    metadata_digest: str | None

    def __post_init__(self) -> None:
        if self.outcome not in {"no_recovery", "rolled_back", "rolled_forward"}:
            _fail("CHUNK.METADATA_INVALID")
        if self.state is None:
            if self.metadata_digest is not None:
                _fail("CHUNK.METADATA_INVALID")
        else:
            validate_chunk_metadata_state(self.state)
            if chunk_metadata_state_digest_v1(self.state) != self.metadata_digest:
                _fail("CHUNK.DIGEST_MISMATCH")


@dataclass(frozen=True, slots=True)
class _JournalRecord:
    project_id: str
    phase: str
    expected_digest: str | None
    candidate_digest: str
    lkg_digest: str | None
    target_name: str
    candidate_name: str
    lkg_name: str


def _journal_bytes(record: _JournalRecord) -> bytes:
    if record.phase not in _JOURNAL_PHASES:
        _fail("CHUNK.METADATA_INVALID")
    validate_chunk_project_id(record.project_id)
    _expect_digest(record.candidate_digest)
    if record.expected_digest is not None:
        _expect_digest(record.expected_digest)
    if record.lkg_digest is not None:
        _expect_digest(record.lkg_digest)
    if (record.expected_digest is None) != (record.lkg_digest is None) or (
        record.expected_digest is not None
        and record.expected_digest != record.lkg_digest
    ):
        _fail("CHUNK.METADATA_INVALID")
    return _canonical_json(
        {
            "candidate_digest": record.candidate_digest,
            "candidate_name": record.candidate_name,
            "expected_digest": record.expected_digest,
            "lkg_digest": record.lkg_digest,
            "lkg_name": record.lkg_name,
            "phase": record.phase,
            "project_id": record.project_id,
            "schema": CHUNK_JOURNAL_SCHEMA,
            "target_name": record.target_name,
        }
    )


def _journal_from_bytes(payload: bytes) -> _JournalRecord:
    value = _expect_object(
        _decode_canonical_json(payload),
        frozenset(
            {
                "candidate_digest",
                "candidate_name",
                "expected_digest",
                "lkg_digest",
                "lkg_name",
                "phase",
                "project_id",
                "schema",
                "target_name",
            }
        ),
    )
    if value["schema"] != CHUNK_JOURNAL_SCHEMA:
        _fail("CHUNK.METADATA_UNSUPPORTED")
    expected = value["expected_digest"]
    lkg = value["lkg_digest"]
    if expected is not None:
        expected = _expect_digest(expected)
    if lkg is not None:
        lkg = _expect_digest(lkg)
    if (expected is None) != (lkg is None) or (
        expected is not None and expected != lkg
    ):
        _fail("CHUNK.METADATA_INVALID")
    record = _JournalRecord(
        project_id=_expect_string(value["project_id"]),
        phase=_expect_string(value["phase"]),
        expected_digest=expected,
        candidate_digest=_expect_digest(value["candidate_digest"]),
        lkg_digest=lkg,
        target_name=_expect_string(value["target_name"]),
        candidate_name=_expect_string(value["candidate_name"]),
        lkg_name=_expect_string(value["lkg_name"]),
    )
    if record.phase not in _JOURNAL_PHASES or _journal_bytes(record) != payload:
        _fail("CHUNK.METADATA_INVALID")
    return record


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(_STORE_DIGEST_DOMAIN + payload).hexdigest()


class CollaborativeChunkStore:
    """One rooted, same-parent Chunk store with journaled CAS publication."""

    __slots__ = (
        "__root",
        "__root_identity",
        "__target_name",
        "__candidate_name",
        "__lkg_name",
        "__lkg_temp_name",
        "__journal_name",
        "__journal_temp_name",
        "__lock_name",
        "__rebase_intent_name",
        "__rebase_intent_temp_name",
        "__project_id",
        "__lock",
    )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("CollaborativeChunkStore cannot be subclassed")

    def __init__(self, root: Path | str, filename: str, *, project_id: str) -> None:
        root_path = Path(root)
        if not root_path.is_absolute():
            _fail("CHUNK.CONTRACT_INVALID")
        try:
            if root_path.resolve(strict=True) != root_path:
                _fail("CHUNK.CONTRACT_INVALID")
        except OSError as error:
            raise ChunkError("CHUNK.CONTRACT_INVALID") from error
        self.__target_name = self._validate_filename(filename)
        self.__candidate_name = f".{filename}.candidate-v1"
        self.__lkg_name = f".{filename}.lkg-v1"
        self.__lkg_temp_name = f".{filename}.lkg-v1.tmp"
        self.__journal_name = f".{filename}.journal-v1"
        self.__journal_temp_name = f".{filename}.journal-v1.tmp"
        self.__lock_name = f".{filename}.lock-v1"
        self.__rebase_intent_name = f".{filename}.rebase-intent-v1"
        self.__rebase_intent_temp_name = f".{filename}.rebase-intent-v1.tmp"
        for value in (
            self.__candidate_name,
            self.__lkg_name,
            self.__lkg_temp_name,
            self.__journal_name,
            self.__journal_temp_name,
            self.__lock_name,
            self.__rebase_intent_name,
            self.__rebase_intent_temp_name,
        ):
            self._validate_filename(value)
        self.__project_id = validate_chunk_project_id(project_id)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(root_path, flags)
        except OSError as error:
            raise ChunkError("CHUNK.CONTRACT_INVALID") from error
        try:
            facts = os.fstat(descriptor)
            if not stat.S_ISDIR(facts.st_mode):
                _fail("CHUNK.CONTRACT_INVALID")
            self.__root_identity = (facts.st_dev, facts.st_ino)
        finally:
            os.close(descriptor)
        self.__root = root_path
        self.__lock = Lock()

    @staticmethod
    def _validate_filename(value: object) -> str:
        if type(value) is not str or value in {"", ".", ".."}:
            _fail("CHUNK.CONTRACT_INVALID")
        if "/" in value or "\x00" in value:
            _fail("CHUNK.CONTRACT_INVALID")
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeError:
            _fail("CHUNK.CONTRACT_INVALID")
        if len(encoded) > _MAX_FILENAME_BYTES or any(
            unicodedata.category(character) in {"Cc", "Cs"} for character in value
        ):
            _fail("CHUNK.CONTRACT_INVALID")
        return value

    @contextmanager
    def _locked_parent(self) -> Iterator[int]:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            parent = os.open(self.__root, flags)
        except OSError:
            raise ChunkError("CHUNK.DESTINATION_STALE", retryable=True) from None
        lock_descriptor = -1
        try:
            facts = os.fstat(parent)
            if (facts.st_dev, facts.st_ino) != self.__root_identity:
                _fail("CHUNK.DESTINATION_STALE", retryable=True)
            lock_descriptor = os.open(
                self.__lock_name,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
            lock_facts = os.fstat(lock_descriptor)
            if not stat.S_ISREG(lock_facts.st_mode):
                _fail("CHUNK.DESTINATION_STALE", retryable=True)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        except ChunkError:
            if lock_descriptor >= 0:
                os.close(lock_descriptor)
            os.close(parent)
            raise
        except OSError:
            if lock_descriptor >= 0:
                try:
                    os.close(lock_descriptor)
                except OSError:
                    pass
            try:
                os.close(parent)
            except OSError:
                pass
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        try:
            yield parent
        finally:
            if lock_descriptor >= 0:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                    os.close(lock_descriptor)
                except OSError:
                    pass
            try:
                os.close(parent)
            except OSError:
                pass

    @staticmethod
    def _entry_exists(parent: int, name: str) -> bool:
        try:
            facts = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        if not stat.S_ISREG(facts.st_mode):
            _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
        return True

    @staticmethod
    def _read_entry(
        parent: int,
        name: str,
        *,
        required: bool,
        maximum_bytes: int = MAX_METADATA_BYTES,
    ) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            if required:
                _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
            return None
        except OSError:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        try:
            facts = os.fstat(descriptor)
            if not stat.S_ISREG(facts.st_mode) or facts.st_size > maximum_bytes:
                _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(descriptor, _READ_CHUNK)
                if not block:
                    break
                total += len(block)
                if total > maximum_bytes:
                    _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                chunks.append(block)
            return b"".join(chunks)
        except ChunkError:
            raise
        except OSError:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_exclusive(parent: int, name: str, payload: bytes) -> None:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(name, flags, 0o600, dir_fd=parent)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short Chunk metadata write")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _unlink(parent: int, name: str, *, missing_ok: bool = True) -> None:
        try:
            facts = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        except OSError:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
        if not stat.S_ISREG(facts.st_mode):
            _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
        os.unlink(name, dir_fd=parent)

    def _write_replace(
        self,
        parent: int,
        temporary_name: str,
        final_name: str,
        payload: bytes,
    ) -> None:
        self._unlink(parent, temporary_name)
        self._write_exclusive(parent, temporary_name, payload)
        os.replace(
            temporary_name,
            final_name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        os.fsync(parent)

    def _pending(self, parent: int) -> bool:
        return any(
            self._entry_exists(parent, name)
            for name in (
                self.__candidate_name,
                self.__lkg_name,
                self.__lkg_temp_name,
                self.__journal_name,
                self.__journal_temp_name,
                self.__rebase_intent_temp_name,
            )
        )

    def _load_target(self, parent: int) -> tuple[ChunkMetadataState | None, str | None, bytes | None]:
        payload = self._read_entry(parent, self.__target_name, required=False)
        if payload is None:
            return None, None, None
        try:
            state = decode_chunk_metadata_state(payload)
        except ChunkError as error:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from error
        if state.project_id != self.__project_id:
            _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
        return state, _sha256(payload), payload

    def _load_rebase_intent(
        self,
        parent: int,
    ) -> tuple[ChunkRebaseIntent | None, bytes | None]:
        payload = self._read_entry(
            parent,
            self.__rebase_intent_name,
            required=False,
            maximum_bytes=MAX_REBASE_INTENT_BYTES,
        )
        if payload is None:
            return None, None
        try:
            intent = decode_chunk_rebase_intent(payload)
        except ChunkError as error:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from error
        if intent.project_id != self.__project_id:
            _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
        return intent, payload

    def load_rebase_intent(self) -> ChunkRebaseIntent | None:
        with self.__lock, self._locked_parent() as parent:
            if self._pending(parent):
                _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
            intent, _ = self._load_rebase_intent(parent)
            return intent

    def capture_rebase_intent(
        self,
        intent: ChunkRebaseIntent,
    ) -> ChunkRebaseIntent:
        validated = validate_chunk_rebase_intent(intent)
        if validated.project_id != self.__project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        payload = encode_chunk_rebase_intent(validated)
        cold = decode_chunk_rebase_intent(payload)
        if cold != validated:
            _fail("CHUNK.DIGEST_MISMATCH")
        with self.__lock, self._locked_parent() as parent:
            if self._pending(parent):
                _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
            state, _, _ = self._load_target(parent)
            if (
                state is None
                or state.active_snapshot is None
                or chunk_plan_binding(state.active_snapshot) != validated.plan_binding
            ):
                _fail("CHUNK.REBASE_REQUIRED")
            current, current_payload = self._load_rebase_intent(parent)
            if current is not None:
                if current == validated and current_payload == payload:
                    return current
                _fail("CHUNK.REBASE_REQUIRED")
            try:
                self._write_exclusive(
                    parent,
                    self.__rebase_intent_temp_name,
                    payload,
                )
                staged = self._read_entry(
                    parent,
                    self.__rebase_intent_temp_name,
                    required=True,
                    maximum_bytes=MAX_REBASE_INTENT_BYTES,
                )
                if staged != payload or decode_chunk_rebase_intent(staged) != validated:
                    _fail("CHUNK.DIGEST_MISMATCH")
                os.replace(
                    self.__rebase_intent_temp_name,
                    self.__rebase_intent_name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
                os.fsync(parent)
                readback = self._read_entry(
                    parent,
                    self.__rebase_intent_name,
                    required=True,
                    maximum_bytes=MAX_REBASE_INTENT_BYTES,
                )
                if readback != payload or decode_chunk_rebase_intent(readback) != validated:
                    _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                return validated
            except ChunkError:
                try:
                    self._unlink(parent, self.__rebase_intent_temp_name)
                    os.fsync(parent)
                except (ChunkError, OSError):
                    raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
                raise
            except OSError:
                try:
                    self._unlink(parent, self.__rebase_intent_temp_name)
                    os.fsync(parent)
                except (ChunkError, OSError):
                    raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None
                raise ChunkError("CHUNK.COMMIT_FAILED", retryable=True) from None

    def clear_consumed_rebase_intent(self) -> bool:
        """Cold cleanup only after the main audit proves rebase/dissolve consumed it."""

        with self.__lock, self._locked_parent() as parent:
            if self._pending(parent):
                _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
            intent, _ = self._load_rebase_intent(parent)
            if intent is None:
                return False
            state, _, _ = self._load_target(parent)
            if state is None:
                _fail("CHUNK.REBASE_REQUIRED")
            final = state.audit_records[-1].receipt
            active = state.active_snapshot
            rebase_consumed = (
                active is not None
                and final.action is TopologyAction.REBASE
                and final.chunk_plan_id == intent.plan_binding.chunk_plan_id
                and final.base_revision == intent.plan_binding.plan_revision
                and final.before_plan_digest == intent.plan_binding.plan_digest
                and active.segment_universe_digest
                == intent.transition.current.binding.segment_universe_digest
            )
            dissolve_consumed = (
                active is None
                and final.action is TopologyAction.DISSOLVE_PLAN
                and final.chunk_plan_id == intent.plan_binding.chunk_plan_id
                and final.before_plan_digest == intent.plan_binding.plan_digest
            )
            if not rebase_consumed and not dissolve_consumed:
                _fail("CHUNK.REBASE_REQUIRED")
            try:
                self._unlink(parent, self.__rebase_intent_name, missing_ok=False)
                os.fsync(parent)
            except ChunkError:
                raise
            except OSError:
                raise ChunkError(
                    "CHUNK.RECOVERY_REQUIRED",
                    retryable=True,
                ) from None
            return True

    def load(self) -> ChunkMetadataState | None:
        state, _ = self.load_with_digest()
        return state

    def load_with_digest(self) -> tuple[ChunkMetadataState | None, str | None]:
        with self.__lock, self._locked_parent() as parent:
            if self._pending(parent):
                _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
            state, digest, _ = self._load_target(parent)
            return state, digest

    def current_digest(self) -> str | None:
        with self.__lock, self._locked_parent() as parent:
            if self._pending(parent):
                _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
            _, digest, _ = self._load_target(parent)
            return digest

    def _cleanup_transaction(self, parent: int) -> None:
        for name in (
            self.__candidate_name,
            self.__lkg_temp_name,
            self.__journal_temp_name,
            self.__journal_name,
            self.__lkg_name,
            self.__rebase_intent_temp_name,
        ):
            self._unlink(parent, name)
        os.fsync(parent)

    def publish(
        self,
        state: ChunkMetadataState,
        *,
        expected_metadata_digest: str | None,
        expected_rebase_intent_digest: str | None = None,
    ) -> ChunkMetadataPublicationResult:
        validated = validate_chunk_metadata_state(state)
        if validated.project_id != self.__project_id:
            _fail("CHUNK.IDENTITY_FOREIGN")
        candidate_payload = encode_chunk_metadata_state(validated)
        cold_candidate = decode_chunk_metadata_state(candidate_payload)
        if cold_candidate != validated:
            _fail("CHUNK.DIGEST_MISMATCH")
        candidate_digest = _sha256(candidate_payload)
        journal_armed = False
        with self.__lock, self._locked_parent() as parent:
            if self._pending(parent):
                _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
            current_state, current_digest, current_payload = self._load_target(parent)
            pending_intent, _ = self._load_rebase_intent(parent)
            final_action = validated.audit_records[-1].receipt.action
            if final_action is TopologyAction.REBASE:
                if (
                    expected_rebase_intent_digest is None
                    or pending_intent is None
                    or pending_intent.intent_digest
                    != expected_rebase_intent_digest
                ):
                    _fail("CHUNK.REBASE_REQUIRED")
                _validate_rebase_candidate_against_intent(
                    current_state,
                    validated,
                    pending_intent,
                )
            elif expected_rebase_intent_digest is not None:
                _fail("CHUNK.METADATA_INVALID")
            elif (
                pending_intent is not None
                and final_action is not TopologyAction.DISSOLVE_PLAN
            ):
                _fail("CHUNK.REBASE_REQUIRED")
            clear_rebase_intent = pending_intent is not None and final_action in {
                TopologyAction.REBASE,
                TopologyAction.DISSOLVE_PLAN,
            }
            if current_digest != expected_metadata_digest:
                _fail("CHUNK.DESTINATION_STALE", retryable=True)
            if current_digest == candidate_digest:
                _fail("CHUNK.DESTINATION_STALE", retryable=True)
            validate_chunk_metadata_successor(current_state, validated)
            try:
                self._write_exclusive(
                    parent,
                    self.__candidate_name,
                    candidate_payload,
                )
                staged_payload = self._read_entry(
                    parent,
                    self.__candidate_name,
                    required=True,
                )
                if staged_payload != candidate_payload:
                    _fail("CHUNK.DIGEST_MISMATCH")
                decode_chunk_metadata_state(staged_payload)

                lkg_digest = None
                if current_payload is not None:
                    self._write_replace(
                        parent,
                        self.__lkg_temp_name,
                        self.__lkg_name,
                        current_payload,
                    )
                    lkg_payload = self._read_entry(
                        parent,
                        self.__lkg_name,
                        required=True,
                    )
                    if lkg_payload != current_payload:
                        _fail("CHUNK.DIGEST_MISMATCH")
                    lkg_digest = current_digest

                journal = _JournalRecord(
                    project_id=self.__project_id,
                    phase="PREPARED",
                    expected_digest=current_digest,
                    candidate_digest=candidate_digest,
                    lkg_digest=lkg_digest,
                    target_name=self.__target_name,
                    candidate_name=self.__candidate_name,
                    lkg_name=self.__lkg_name,
                )
                self._write_replace(
                    parent,
                    self.__journal_temp_name,
                    self.__journal_name,
                    _journal_bytes(journal),
                )
                journal_armed = True

                _, revalidated_digest, _ = self._load_target(parent)
                if revalidated_digest != current_digest:
                    self._cleanup_transaction(parent)
                    journal_armed = False
                    _fail("CHUNK.DESTINATION_STALE", retryable=True)

                os.replace(
                    self.__candidate_name,
                    self.__target_name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
                os.fsync(parent)
                journal = _JournalRecord(
                    project_id=journal.project_id,
                    phase="TARGET_REPLACED",
                    expected_digest=journal.expected_digest,
                    candidate_digest=journal.candidate_digest,
                    lkg_digest=journal.lkg_digest,
                    target_name=journal.target_name,
                    candidate_name=journal.candidate_name,
                    lkg_name=journal.lkg_name,
                )
                self._write_replace(
                    parent,
                    self.__journal_temp_name,
                    self.__journal_name,
                    _journal_bytes(journal),
                )
                readback_state, readback_digest, _ = self._load_target(parent)
                if readback_state != validated or readback_digest != candidate_digest:
                    _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                if clear_rebase_intent:
                    self._unlink(
                        parent,
                        self.__rebase_intent_name,
                        missing_ok=False,
                    )
                    os.fsync(parent)
                self._cleanup_transaction(parent)
                journal_armed = False
                return ChunkMetadataPublicationResult(
                    state=readback_state,
                    metadata_digest=candidate_digest,
                    previous_metadata_digest=current_digest,
                    lkg_was_present=current_payload is not None,
                )
            except ChunkError:
                if not journal_armed:
                    try:
                        self._cleanup_transaction(parent)
                    except (OSError, ChunkError):
                        raise ChunkError(
                            "CHUNK.RECOVERY_REQUIRED", retryable=True
                        ) from None
                raise
            except OSError:
                if not journal_armed:
                    try:
                        self._cleanup_transaction(parent)
                    except (OSError, ChunkError):
                        raise ChunkError(
                            "CHUNK.RECOVERY_REQUIRED", retryable=True
                        ) from None
                    raise ChunkError("CHUNK.COMMIT_FAILED", retryable=True) from None
                raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None

    def recover(self) -> ChunkMetadataRecoveryReport:
        try:
            return self._recover()
        except ChunkError:
            raise
        except OSError:
            raise ChunkError("CHUNK.RECOVERY_REQUIRED", retryable=True) from None

    def _recover(self) -> ChunkMetadataRecoveryReport:
        with self.__lock, self._locked_parent() as parent:
            journal_payload = self._read_entry(
                parent,
                self.__journal_name,
                required=False,
            )
            if journal_payload is None:
                had_pending = self._pending(parent)
                outcome = "rolled_back" if had_pending else "no_recovery"
                target_payload = self._read_entry(
                    parent,
                    self.__target_name,
                    required=False,
                )
                lkg_payload = self._read_entry(
                    parent,
                    self.__lkg_name,
                    required=False,
                )
                if lkg_payload is not None:
                    try:
                        lkg_state = decode_chunk_metadata_state(lkg_payload)
                    except ChunkError:
                        _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                    if lkg_state.project_id != self.__project_id:
                        _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                    if target_payload is None:
                        os.replace(
                            self.__lkg_name,
                            self.__target_name,
                            src_dir_fd=parent,
                            dst_dir_fd=parent,
                        )
                        os.fsync(parent)
                        target_payload = lkg_payload
                        state = lkg_state
                    elif target_payload == lkg_payload:
                        state = lkg_state
                    else:
                        try:
                            state = decode_chunk_metadata_state(target_payload)
                            if state.project_id != self.__project_id:
                                _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                            validate_chunk_metadata_successor(lkg_state, state)
                        except ChunkError:
                            _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                        outcome = "rolled_forward"
                else:
                    state = None
                if target_payload is not None:
                    if state is None:
                        try:
                            state = decode_chunk_metadata_state(target_payload)
                        except ChunkError:
                            _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                    if state.project_id != self.__project_id:
                        _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                self._cleanup_transaction(parent)
                digest = None if target_payload is None else _sha256(target_payload)
                return ChunkMetadataRecoveryReport(
                    outcome=outcome,
                    state=state,
                    metadata_digest=digest,
                )

            try:
                journal = _journal_from_bytes(journal_payload)
            except ChunkError:
                _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
            if (
                journal.project_id != self.__project_id
                or journal.target_name != self.__target_name
                or journal.candidate_name != self.__candidate_name
                or journal.lkg_name != self.__lkg_name
            ):
                _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
            target_payload = self._read_entry(
                parent,
                self.__target_name,
                required=False,
            )
            target_digest = None if target_payload is None else _sha256(target_payload)
            lkg_payload = self._read_entry(
                parent,
                self.__lkg_name,
                required=False,
            )
            if journal.lkg_digest is None:
                if lkg_payload is not None:
                    _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                lkg_valid = False
            else:
                lkg_valid = (
                    lkg_payload is not None
                    and _sha256(lkg_payload) == journal.lkg_digest
                )

            if target_digest == journal.candidate_digest:
                try:
                    assert target_payload is not None
                    state = decode_chunk_metadata_state(target_payload)
                except ChunkError:
                    state = None
                if state is not None and state.project_id == self.__project_id:
                    if journal.expected_digest is None:
                        prior_state = None
                    else:
                        if not lkg_valid or lkg_payload is None:
                            _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                        try:
                            prior_state = decode_chunk_metadata_state(lkg_payload)
                        except ChunkError:
                            _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                        if prior_state.project_id != self.__project_id:
                            _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                    try:
                        validate_chunk_metadata_successor(prior_state, state)
                    except ChunkError:
                        state = None
                    if state is not None:
                        self._cleanup_transaction(parent)
                        return ChunkMetadataRecoveryReport(
                            outcome="rolled_forward",
                            state=state,
                            metadata_digest=target_digest,
                        )
                if journal.expected_digest is None and lkg_payload is None:
                    self._unlink(parent, self.__target_name)
                    os.fsync(parent)
                    self._cleanup_transaction(parent)
                    return ChunkMetadataRecoveryReport(
                        outcome="rolled_back",
                        state=None,
                        metadata_digest=None,
                    )

            if target_digest == journal.expected_digest:
                try:
                    state = (
                        None
                        if target_payload is None
                        else decode_chunk_metadata_state(target_payload)
                    )
                except ChunkError:
                    _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                if state is not None and state.project_id != self.__project_id:
                    _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                self._cleanup_transaction(parent)
                return ChunkMetadataRecoveryReport(
                    outcome="rolled_back",
                    state=state,
                    metadata_digest=target_digest,
                )

            if lkg_valid:
                assert lkg_payload is not None
                try:
                    state = decode_chunk_metadata_state(lkg_payload)
                except ChunkError:
                    _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                if state.project_id != self.__project_id:
                    _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
                os.replace(
                    self.__lkg_name,
                    self.__target_name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
                os.fsync(parent)
                self._cleanup_transaction(parent)
                return ChunkMetadataRecoveryReport(
                    outcome="rolled_back",
                    state=state,
                    metadata_digest=journal.lkg_digest,
                )
            if journal.expected_digest is None and target_payload is None:
                self._cleanup_transaction(parent)
                return ChunkMetadataRecoveryReport(
                    outcome="rolled_back",
                    state=None,
                    metadata_digest=None,
                )
            _fail("CHUNK.RECOVERY_REQUIRED", retryable=True)
