"""Exact winner and raw context-v1 classification for TM retrieval.

Task 7.1 production slice: a per-resource exact/context classifier that
Task 7.3 composes into the full query pipeline.  It consumes only the
leased ``TMStore.exact_records`` port and performs no persistence, scoring,
capability or limit side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tm_contracts import (
    ContextEvidence,
    TMMatchType,
    TMQuery,
    TMRecord,
    TMResourceHandle,
    TMResult,
)


_CONTEXT_FIELD_NAMES = (
    "speaker_raw",
    "context_prev_raw",
    "context_next_raw",
)

_EMPTY_CONTEXT_EVIDENCE = ContextEvidence(
    comparable_fields=(),
    matched_fields=(),
    mismatched_fields=(),
    strength_v1=(0, 0, 0, 0, 0),
)


def _require_exact_type(value: object, expected_type: type[Any], label: str) -> None:
    if type(value) is not expected_type:
        raise TypeError(f"{label} must be an exact {expected_type.__name__}")


def _is_nonempty_builtin_string(value: object) -> bool:
    return type(value) is str and value != ""


def compare_context_v1(*, query: TMQuery, record: TMRecord) -> ContextEvidence:
    """Compare one record's raw context facts against a query under context-v1.

    A field is comparable only when both sides are non-empty built-in strings.
    Comparable fields are compared by raw full-string equality; case and
    whitespace differences are mismatches.  Missing facts are never invented.
    """

    _require_exact_type(query, TMQuery, "query")
    _require_exact_type(record, TMRecord, "record")

    comparable: list[str] = []
    matched: list[str] = []
    mismatched: list[str] = []
    for field_name in _CONTEXT_FIELD_NAMES:
        query_value = getattr(query, field_name)
        record_value = getattr(record, field_name)
        if not (
            _is_nonempty_builtin_string(query_value)
            and _is_nonempty_builtin_string(record_value)
        ):
            continue
        comparable.append(field_name)
        if query_value == record_value:
            matched.append(field_name)
        else:
            mismatched.append(field_name)

    speaker_match = int("speaker_raw" in matched)
    prev_match = int("context_prev_raw" in matched)
    next_match = int("context_next_raw" in matched)
    return ContextEvidence(
        comparable_fields=tuple(comparable),
        matched_fields=tuple(matched),
        mismatched_fields=tuple(mismatched),
        strength_v1=(
            len(matched),
            -len(mismatched),
            speaker_match,
            prev_match,
            next_match,
        ),
    )


@dataclass(frozen=True)
class ExactContextClassification:
    """Per-resource exact/context classification of one exact-source group.

    ``winner`` is the sole EXACT result (maximum valid record id).
    ``context_results`` are same-source variants with positive raw context
    evidence, ordered by record id descending.  ``retained_only_variants``
    are same-source records without positive context evidence: retained for
    export/audit but omitted from returned suggestions.
    """

    resource_id: str
    winner: TMResult | None
    context_results: tuple[TMResult, ...]
    retained_only_variants: tuple[TMRecord, ...]

    @property
    def returned_results(self) -> tuple[TMResult, ...]:
        """Return suggestions in the per-resource EXACT then CONTEXT order."""

        if self.winner is None:
            return self.context_results
        return (self.winner,) + self.context_results


def classify_exact_context(
    *,
    resource_id: str,
    resource_order: int,
    query: TMQuery,
    records: tuple[TMRecord, ...],
) -> ExactContextClassification:
    """Classify one resource's exact-source records into winner and variants.

    The maximum valid ``record_id`` is the sole compatibility EXACT winner,
    independent of the order in which records are supplied.  Other
    same-source variants become CONTEXT only when they carry at least one
    matched raw context fact; otherwise they are retained but omitted.
    """

    _require_exact_type(resource_id, str, "resource_id")
    _require_exact_type(resource_order, int, "resource_order")
    _require_exact_type(query, TMQuery, "query")
    _require_exact_type(records, tuple, "records")
    if not resource_id.strip():
        raise ValueError("resource_id must not be empty")
    if resource_order < 0:
        raise ValueError("resource_order must be non-negative")
    record_ids: set[int] = set()
    for record in records:
        _require_exact_type(record, TMRecord, "record")
        if record.source_raw != query.query_source:
            raise ValueError("records must belong to the raw exact source")
        if record.record_id in record_ids:
            raise ValueError("records must have unique record ids")
        record_ids.add(record.record_id)

    if not records:
        return ExactContextClassification(
            resource_id=resource_id,
            winner=None,
            context_results=(),
            retained_only_variants=(),
        )

    winner_record = max(records, key=lambda record: record.record_id)
    winner = TMResult(
        resource_id=resource_id,
        record_id=winner_record.record_id,
        query_source=query.query_source,
        matched_source=winner_record.source_raw,
        target=winner_record.target_raw,
        match_type=TMMatchType.EXACT,
        similarity=1.0,
        similarity_evidence=None,
        context_evidence=_EMPTY_CONTEXT_EVIDENCE,
        provenance=winner_record.provenance,
        stable_tie_key=(resource_order, winner_record.record_id),
    )

    context_results: list[TMResult] = []
    retained_only: list[TMRecord] = []
    for record in records:
        if record.record_id == winner_record.record_id:
            continue
        evidence = compare_context_v1(query=query, record=record)
        if evidence.matched_fields:
            context_results.append(
                TMResult(
                    resource_id=resource_id,
                    record_id=record.record_id,
                    query_source=query.query_source,
                    matched_source=record.source_raw,
                    target=record.target_raw,
                    match_type=TMMatchType.CONTEXT,
                    similarity=1.0,
                    similarity_evidence=None,
                    context_evidence=evidence,
                    provenance=record.provenance,
                    stable_tie_key=(resource_order, record.record_id),
                )
            )
        else:
            retained_only.append(record)

    context_results.sort(key=lambda result: result.record_id, reverse=True)
    retained_only.sort(key=lambda record: record.record_id, reverse=True)
    return ExactContextClassification(
        resource_id=resource_id,
        winner=winner,
        context_results=tuple(context_results),
        retained_only_variants=tuple(retained_only),
    )


def query_resource_exact(
    *,
    handle: TMResourceHandle,
    query: TMQuery,
) -> ExactContextClassification:
    """Classify one resource through its leased ``exact_records`` store port.

    Callers must only pass handles selected for lookup; the Active+Lookup
    resource gate is owned by the Task 7.3 aggregation layer.  This seam
    performs no persistence, scoring, capability publication or limit
    side effects.
    """

    _require_exact_type(handle, TMResourceHandle, "handle")
    _require_exact_type(query, TMQuery, "query")
    records = handle.store.exact_records(query.query_source)
    return classify_exact_context(
        resource_id=handle.resource_id,
        resource_order=handle.order,
        query=query,
        records=records,
    )
