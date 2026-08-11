"""Task 7.1/7.2/7.3 focused tests: classification, scoring and aggregation."""

from __future__ import annotations

import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from tm_contracts import (
    CANDIDATE_BUDGET_VERSION,
    CandidateEvidence,
    CandidateRecallMetadata,
    CandidateRetrievalReport,
    CandidateStage,
    CandidateStageMetadata,
    ContextEvidence,
    SimilarityEvidence,
    StoreHealth,
    TMMatchType,
    TMQuery,
    TMRecord,
    TMRecordDraft,
    TMResourceHandle,
    TMResult,
    TMStore,
    candidate_budget_v1,
    QueryReport,
    ResourceQueryFailure,
    ResourceQueryMetadata,
)
from text_matcher import fold_text_v1
from tm_retrieval import (
    ExactContextClassification,
    FuzzyScoringResult,
    TMRetrievalService,
    classify_exact_context,
    compare_context_v1,
    query_resource_exact,
    score_fuzzy_candidates,
)
from tm_similarity import SimilarityScorerV1
from tm_sqlite_store import SQLiteStoreLifecycleError, SQLiteStoreSchemaError


FIXTURE_VERSION = "tm-retrieval-vectors-v1"
QUERY_SOURCE = "Open the door."
WINNER_RECORD_ID = 100


class _StrSubclass(str):
    pass


def _empty_evidence() -> ContextEvidence:
    return ContextEvidence(
        comparable_fields=(),
        matched_fields=(),
        mismatched_fields=(),
        strength_v1=(0, 0, 0, 0, 0),
    )


def _record(
    record_id: int,
    *,
    source_raw: str = QUERY_SOURCE,
    target_raw: str = "target",
    speaker_raw: str | None = None,
    context_prev_raw: str | None = None,
    context_next_raw: str | None = None,
) -> TMRecord:
    return TMRecord(
        record_id=record_id,
        source_raw=source_raw,
        target_raw=target_raw,
        speaker_raw=speaker_raw,
        context_prev_raw=context_prev_raw,
        context_next_raw=context_next_raw,
        file_source=None,
        provenance=(("importer", f"legacy-{record_id}"),),
        legacy_line_no=None,
        origin_batch_id="batch.7.1",
        origin_ordinal=record_id,
    )


def _variant_record(
    *,
    record_id: int,
    target_raw: str = "variant-target",
    speaker_raw: str | None = None,
    context_prev_raw: str | None = None,
    context_next_raw: str | None = None,
) -> TMRecord:
    return _record(
        record_id=record_id,
        target_raw=target_raw,
        speaker_raw=speaker_raw,
        context_prev_raw=context_prev_raw,
        context_next_raw=context_next_raw,
    )


def _query(
    *,
    speaker_raw: str | None = None,
    context_prev_raw: str | None = None,
    context_next_raw: str | None = None,
) -> TMQuery:
    return TMQuery(
        query_source=QUERY_SOURCE,
        speaker_raw=speaker_raw,
        context_prev_raw=context_prev_raw,
        context_next_raw=context_next_raw,
        minimum_similarity=0.7,
        limit=10,
        resource_order=("tm.primary",),
    )


class _StubStore:
    def __init__(self, records: tuple[TMRecord, ...]) -> None:
        self.records = records
        self.exact_records_calls: list[str] = []
        self.records_by_id_calls = 0
        self.append_calls = 0
        self.export_records_calls = 0
        self.health_calls = 0

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        self.exact_records_calls.append(source_raw)
        return self.records

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]:
        self.records_by_id_calls += 1
        return ()

    def append(self, draft: TMRecordDraft) -> TMRecord:
        self.append_calls += 1
        raise AssertionError("retrieval must not append")

    def export_records(self) -> Iterator[TMRecord]:
        self.export_records_calls += 1
        return iter(())

    def health(self) -> StoreHealth:
        self.health_calls += 1
        return StoreHealth(
            healthy=True,
            schema_version=1,
            generation=0,
            record_count=0,
            index_kind="UNBUILT",
            snapshot_binding_digest=None,
            source_binding_state=None,
            exact_available=True,
            context_available=False,
            fuzzy_available=False,
            diagnostic_codes=(),
        )


def _handle(
    store: TMStore,
    *,
    resource_id: str = "tm.primary",
    order: int = 0,
) -> TMResourceHandle:
    return TMResourceHandle(
        resource_id=resource_id,
        store=store,
        active=True,
        lookup=True,
        update=True,
        order=order,
    )


# Evidence tuples are (comparable_fields, matched_fields, mismatched_fields,
# strength_v1).  The winner is always record 100 without context fields.
_GOLDEN_VECTORS: tuple[dict[str, Any], ...] = (
    {
        "id": "speaker-only-match",
        "query": {
            "speaker_raw": "Alice",
            "context_prev_raw": None,
            "context_next_raw": None,
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "Alice",
                "context_prev_raw": None,
                "context_next_raw": None,
            },
        ),
        "expected": {
            "context_record_ids": (1,),
            "retained_record_ids": (),
            "evidence": {
                1: (("speaker_raw",), ("speaker_raw",), (), (1, 0, 1, 0, 0)),
            },
        },
    },
    {
        "id": "prev-only-match",
        "query": {
            "speaker_raw": None,
            "context_prev_raw": "Wait.",
            "context_next_raw": None,
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": None,
                "context_prev_raw": "Wait.",
                "context_next_raw": None,
            },
        ),
        "expected": {
            "context_record_ids": (1,),
            "retained_record_ids": (),
            "evidence": {
                1: (
                    ("context_prev_raw",),
                    ("context_prev_raw",),
                    (),
                    (1, 0, 0, 1, 0),
                ),
            },
        },
    },
    {
        "id": "next-only-match",
        "query": {
            "speaker_raw": None,
            "context_prev_raw": None,
            "context_next_raw": "Now.",
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": None,
                "context_prev_raw": None,
                "context_next_raw": "Now.",
            },
        ),
        "expected": {
            "context_record_ids": (1,),
            "retained_record_ids": (),
            "evidence": {
                1: (
                    ("context_next_raw",),
                    ("context_next_raw",),
                    (),
                    (1, 0, 0, 0, 1),
                ),
            },
        },
    },
    {
        "id": "all-three-match",
        "query": {
            "speaker_raw": "Alice",
            "context_prev_raw": "Wait.",
            "context_next_raw": "Now.",
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "Alice",
                "context_prev_raw": "Wait.",
                "context_next_raw": "Now.",
            },
        ),
        "expected": {
            "context_record_ids": (1,),
            "retained_record_ids": (),
            "evidence": {
                1: (
                    ("speaker_raw", "context_prev_raw", "context_next_raw"),
                    ("speaker_raw", "context_prev_raw", "context_next_raw"),
                    (),
                    (3, 0, 1, 1, 1),
                ),
            },
        },
    },
    {
        "id": "speaker-match-prev-mismatch-next-missing",
        "query": {
            "speaker_raw": "Alice",
            "context_prev_raw": "Wait.",
            "context_next_raw": None,
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "Alice",
                "context_prev_raw": "Wait?",
                "context_next_raw": None,
            },
        ),
        "expected": {
            "context_record_ids": (1,),
            "retained_record_ids": (),
            "evidence": {
                1: (
                    ("speaker_raw", "context_prev_raw"),
                    ("speaker_raw",),
                    ("context_prev_raw",),
                    (1, -1, 1, 0, 0),
                ),
            },
        },
    },
    {
        "id": "prev-next-match-speaker-mismatch",
        "query": {
            "speaker_raw": "Alice",
            "context_prev_raw": "Wait.",
            "context_next_raw": "Now.",
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "Bob",
                "context_prev_raw": "Wait.",
                "context_next_raw": "Now.",
            },
        ),
        "expected": {
            "context_record_ids": (1,),
            "retained_record_ids": (),
            "evidence": {
                1: (
                    ("speaker_raw", "context_prev_raw", "context_next_raw"),
                    ("context_prev_raw", "context_next_raw"),
                    ("speaker_raw",),
                    (2, -1, 0, 1, 1),
                ),
            },
        },
    },
    {
        "id": "all-mismatch-has-no-positive-evidence",
        "query": {
            "speaker_raw": "Alice",
            "context_prev_raw": "Wait.",
            "context_next_raw": "Now.",
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "Bob",
                "context_prev_raw": "Hmm.",
                "context_next_raw": "Later.",
            },
        ),
        "expected": {
            "context_record_ids": (),
            "retained_record_ids": (1,),
            "evidence": {
                1: (
                    ("speaker_raw", "context_prev_raw", "context_next_raw"),
                    (),
                    ("speaker_raw", "context_prev_raw", "context_next_raw"),
                    (0, -3, 0, 0, 0),
                ),
            },
        },
    },
    {
        "id": "query-empty-string-is-not-comparable",
        "query": {
            "speaker_raw": "",
            "context_prev_raw": None,
            "context_next_raw": None,
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "Alice",
                "context_prev_raw": None,
                "context_next_raw": None,
            },
        ),
        "expected": {
            "context_record_ids": (),
            "retained_record_ids": (1,),
            "evidence": {1: ((), (), (), (0, 0, 0, 0, 0))},
        },
    },
    {
        "id": "record-empty-string-is-not-comparable",
        "query": {
            "speaker_raw": "Alice",
            "context_prev_raw": None,
            "context_next_raw": None,
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "",
                "context_prev_raw": None,
                "context_next_raw": None,
            },
        ),
        "expected": {
            "context_record_ids": (),
            "retained_record_ids": (1,),
            "evidence": {1: ((), (), (), (0, 0, 0, 0, 0))},
        },
    },
    {
        "id": "record-none-is-not-comparable",
        "query": {
            "speaker_raw": "Alice",
            "context_prev_raw": None,
            "context_next_raw": None,
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": None,
                "context_prev_raw": None,
                "context_next_raw": None,
            },
        ),
        "expected": {
            "context_record_ids": (),
            "retained_record_ids": (1,),
            "evidence": {1: ((), (), (), (0, 0, 0, 0, 0))},
        },
    },
    {
        "id": "query-none-is-not-comparable",
        "query": {
            "speaker_raw": None,
            "context_prev_raw": None,
            "context_next_raw": None,
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "Alice",
                "context_prev_raw": None,
                "context_next_raw": None,
            },
        ),
        "expected": {
            "context_record_ids": (),
            "retained_record_ids": (1,),
            "evidence": {1: ((), (), (), (0, 0, 0, 0, 0))},
        },
    },
    {
        "id": "case-difference-is-a-mismatch",
        "query": {
            "speaker_raw": "Alice",
            "context_prev_raw": None,
            "context_next_raw": None,
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "alice",
                "context_prev_raw": None,
                "context_next_raw": None,
            },
        ),
        "expected": {
            "context_record_ids": (),
            "retained_record_ids": (1,),
            "evidence": {
                1: (
                    ("speaker_raw",),
                    (),
                    ("speaker_raw",),
                    (0, -1, 0, 0, 0),
                ),
            },
        },
    },
    {
        "id": "trailing-whitespace-difference-is-a-mismatch",
        "query": {
            "speaker_raw": None,
            "context_prev_raw": "Wait.",
            "context_next_raw": None,
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": None,
                "context_prev_raw": "Wait. ",
                "context_next_raw": None,
            },
        ),
        "expected": {
            "context_record_ids": (),
            "retained_record_ids": (1,),
            "evidence": {
                1: (
                    ("context_prev_raw",),
                    (),
                    ("context_prev_raw",),
                    (0, -1, 0, 0, 0),
                ),
            },
        },
    },
    {
        "id": "leading-whitespace-difference-is-a-mismatch",
        "query": {
            "speaker_raw": None,
            "context_prev_raw": None,
            "context_next_raw": "Now.",
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": None,
                "context_prev_raw": None,
                "context_next_raw": " Now.",
            },
        ),
        "expected": {
            "context_record_ids": (),
            "retained_record_ids": (1,),
            "evidence": {
                1: (
                    ("context_next_raw",),
                    (),
                    ("context_next_raw",),
                    (0, -1, 0, 0, 0),
                ),
            },
        },
    },
    {
        "id": "substring-is-not-a-full-string-match",
        "query": {
            "speaker_raw": None,
            "context_prev_raw": "Hello world",
            "context_next_raw": None,
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": None,
                "context_prev_raw": "Hello",
                "context_next_raw": None,
            },
        ),
        "expected": {
            "context_record_ids": (),
            "retained_record_ids": (1,),
            "evidence": {
                1: (
                    ("context_prev_raw",),
                    (),
                    ("context_prev_raw",),
                    (0, -1, 0, 0, 0),
                ),
            },
        },
    },
    {
        "id": "two-matched-one-mismatched",
        "query": {
            "speaker_raw": "Alice",
            "context_prev_raw": "Wait.",
            "context_next_raw": "Now.",
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "Alice",
                "context_prev_raw": "Wait.",
                "context_next_raw": "Later.",
            },
        ),
        "expected": {
            "context_record_ids": (1,),
            "retained_record_ids": (),
            "evidence": {
                1: (
                    ("speaker_raw", "context_prev_raw", "context_next_raw"),
                    ("speaker_raw", "context_prev_raw"),
                    ("context_next_raw",),
                    (2, -1, 1, 1, 0),
                ),
            },
        },
    },
    {
        "id": "empty-on-both-sides-is-not-comparable",
        "query": {
            "speaker_raw": "Alice",
            "context_prev_raw": "",
            "context_next_raw": "Now.",
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "Alice",
                "context_prev_raw": "",
                "context_next_raw": "Now.",
            },
        ),
        "expected": {
            "context_record_ids": (1,),
            "retained_record_ids": (),
            "evidence": {
                1: (
                    ("speaker_raw", "context_next_raw"),
                    ("speaker_raw", "context_next_raw"),
                    (),
                    (2, 0, 1, 0, 1),
                ),
            },
        },
    },
    {
        "id": "two-context-variants-returned-by-record-id-descending",
        "query": {
            "speaker_raw": "Alice",
            "context_prev_raw": "Wait.",
            "context_next_raw": None,
        },
        "variants": (
            {
                "record_id": 1,
                "speaker_raw": "Alice",
                "context_prev_raw": None,
                "context_next_raw": None,
            },
            {
                "record_id": 3,
                "speaker_raw": None,
                "context_prev_raw": "Wait.",
                "context_next_raw": None,
            },
        ),
        "expected": {
            "context_record_ids": (3, 1),
            "retained_record_ids": (),
            "evidence": {
                1: (
                    ("speaker_raw",),
                    ("speaker_raw",),
                    (),
                    (1, 0, 1, 0, 0),
                ),
                3: (
                    ("context_prev_raw",),
                    ("context_prev_raw",),
                    (),
                    (1, 0, 0, 1, 0),
                ),
            },
        },
    },
)


class GoldenVectorTests(unittest.TestCase):
    def test_fixture_version_is_frozen(self) -> None:
        self.assertEqual(FIXTURE_VERSION, "tm-retrieval-vectors-v1")

    def test_golden_vectors_classify_types_strength_and_retained_variants(
        self,
    ) -> None:
        for vector in _GOLDEN_VECTORS:
            with self.subTest(vector=vector["id"]):
                query = _query(**cast("dict[str, Any]", vector["query"]))
                records = (_record(WINNER_RECORD_ID),) + tuple(
                    _variant_record(**cast("dict[str, Any]", variant))
                    for variant in cast(
                        "tuple[dict[str, Any], ...]",
                        vector["variants"],
                    )
                )
                classification = classify_exact_context(
                    resource_id="tm.primary",
                    resource_order=0,
                    query=query,
                    records=records,
                )
                self.assertIsInstance(
                    classification,
                    ExactContextClassification,
                )

                winner = classification.winner
                if winner is None:
                    self.fail("expected an exact winner")
                self.assertEqual(winner.record_id, WINNER_RECORD_ID)
                self.assertIs(winner.match_type, TMMatchType.EXACT)
                self.assertEqual(winner.similarity, 1.0)
                self.assertIsNone(winner.similarity_evidence)
                self.assertEqual(winner.context_evidence, _empty_evidence())

                self.assertEqual(
                    tuple(
                        result.record_id
                        for result in classification.context_results
                    ),
                    cast(
                        "tuple[int, ...]",
                        vector["expected"]["context_record_ids"],
                    ),
                )
                self.assertEqual(
                    tuple(
                        record.record_id
                        for record in classification.retained_only_variants
                    ),
                    cast(
                        "tuple[int, ...]",
                        vector["expected"]["retained_record_ids"],
                    ),
                )

                expected_evidence = cast(
                    "dict[int, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[int, int, int, int, int]]]",
                    vector["expected"]["evidence"],
                )
                for record_id, (
                    comparable,
                    matched,
                    mismatched,
                    strength,
                ) in expected_evidence.items():
                    with self.subTest(record_id=record_id):
                        variant_record = next(
                            record
                            for record in records
                            if record.record_id == record_id
                        )
                        evidence = compare_context_v1(
                            query=query,
                            record=variant_record,
                        )
                        self.assertEqual(
                            evidence.comparable_fields,
                            comparable,
                        )
                        self.assertEqual(evidence.matched_fields, matched)
                        self.assertEqual(
                            evidence.mismatched_fields,
                            mismatched,
                        )
                        self.assertEqual(evidence.strength_v1, strength)

                for result in classification.context_results:
                    with self.subTest(record_id=result.record_id):
                        self.assertIs(result.match_type, TMMatchType.CONTEXT)
                        self.assertEqual(result.similarity, 1.0)
                        self.assertIsNone(result.similarity_evidence)
                        self.assertEqual(
                            result.query_source,
                            QUERY_SOURCE,
                        )
                        self.assertEqual(
                            result.matched_source,
                            QUERY_SOURCE,
                        )
                        variant_record = next(
                            record
                            for record in records
                            if record.record_id == result.record_id
                        )
                        self.assertEqual(
                            result.target,
                            variant_record.target_raw,
                        )
                        self.assertEqual(
                            result.provenance,
                            variant_record.provenance,
                        )
                        self.assertEqual(
                            result.stable_tie_key,
                            (0, result.record_id),
                        )
                        self.assertTrue(
                            result.context_evidence.matched_fields
                        )

                self.assertEqual(
                    tuple(
                        result.record_id
                        for result in classification.returned_results
                    ),
                    (WINNER_RECORD_ID,)
                    + cast(
                        "tuple[int, ...]",
                        vector["expected"]["context_record_ids"],
                    ),
                )

    def test_returned_results_are_exact_then_context(self) -> None:
        query = _query(
            speaker_raw="Alice",
            context_prev_raw="Wait.",
            context_next_raw=None,
        )
        records = (
            _record(WINNER_RECORD_ID),
            _variant_record(
                record_id=1,
                speaker_raw="Alice",
                context_prev_raw=None,
                context_next_raw=None,
            ),
            _variant_record(
                record_id=3,
                speaker_raw=None,
                context_prev_raw="Wait.",
                context_next_raw=None,
            ),
        )
        classification = classify_exact_context(
            resource_id="tm.primary",
            resource_order=0,
            query=query,
            records=records,
        )
        self.assertEqual(
            [result.match_type for result in classification.returned_results],
            [TMMatchType.EXACT, TMMatchType.CONTEXT, TMMatchType.CONTEXT],
        )


class ExactWinnerTests(unittest.TestCase):
    def test_maximum_record_identity_is_the_last_valid_record_winner(
        self,
    ) -> None:
        records = (
            _record(3, target_raw="first-jsonl"),
            _record(7, target_raw="last-jsonl"),
            _record(5, target_raw="middle-jsonl"),
        )
        classification = classify_exact_context(
            resource_id="tm.primary",
            resource_order=1,
            query=_query(),
            records=records,
        )
        winner = classification.winner
        if winner is None:
            self.fail("expected an exact winner")
        self.assertEqual(winner.record_id, 7)
        self.assertEqual(winner.target, "last-jsonl")
        self.assertIs(winner.match_type, TMMatchType.EXACT)

    def test_exact_winner_carries_full_record_facts(self) -> None:
        record = _record(
            WINNER_RECORD_ID,
            target_raw="开门。",
            speaker_raw="Alice",
        )
        classification = classify_exact_context(
            resource_id="tm.primary",
            resource_order=2,
            query=_query(),
            records=(record,),
        )
        winner = classification.winner
        if winner is None:
            self.fail("expected an exact winner")
        self.assertEqual(winner.resource_id, "tm.primary")
        self.assertEqual(winner.record_id, WINNER_RECORD_ID)
        self.assertEqual(winner.query_source, QUERY_SOURCE)
        self.assertEqual(winner.matched_source, QUERY_SOURCE)
        self.assertEqual(winner.target, "开门。")
        self.assertIs(winner.match_type, TMMatchType.EXACT)
        self.assertEqual(winner.similarity, 1.0)
        self.assertIsNone(winner.similarity_evidence)
        self.assertEqual(
            winner.provenance,
            (("importer", "legacy-100"),),
        )
        self.assertEqual(winner.stable_tie_key, (2, WINNER_RECORD_ID))
        self.assertEqual(winner.context_evidence, _empty_evidence())

    def test_classification_is_deterministic_across_record_orders(self) -> None:
        records_forward = (
            _record(1, target_raw="first"),
            _record(5, target_raw="middle"),
            _record(9, target_raw="last"),
        )
        records_reversed = tuple(reversed(records_forward))
        query = _query(
            speaker_raw="Alice",
            context_prev_raw="Wait.",
            context_next_raw=None,
        )
        forward = classify_exact_context(
            resource_id="tm.primary",
            resource_order=0,
            query=query,
            records=records_forward,
        )
        reversed_order = classify_exact_context(
            resource_id="tm.primary",
            resource_order=0,
            query=query,
            records=records_reversed,
        )
        self.assertEqual(forward, reversed_order)
        forward_winner = forward.winner
        if forward_winner is None:
            self.fail("expected an exact winner")
        self.assertEqual(forward_winner.record_id, 9)

    def test_empty_records_classify_without_winner(self) -> None:
        classification = classify_exact_context(
            resource_id="tm.primary",
            resource_order=0,
            query=_query(),
            records=(),
        )
        self.assertIsNone(classification.winner)
        self.assertEqual(classification.context_results, ())
        self.assertEqual(classification.retained_only_variants, ())
        self.assertEqual(classification.returned_results, ())

    def test_retained_only_variants_preserve_full_records_and_are_omitted(
        self,
    ) -> None:
        winner = _record(WINNER_RECORD_ID)
        mismatched = _variant_record(
            record_id=1,
            target_raw="variant-mismatch",
            speaker_raw="Bob",
        )
        no_context = _variant_record(
            record_id=2,
            target_raw="variant-no-context",
        )
        query = _query(speaker_raw="Alice")
        classification = classify_exact_context(
            resource_id="tm.primary",
            resource_order=0,
            query=query,
            records=(winner, mismatched, no_context),
        )
        self.assertEqual(
            classification.retained_only_variants,
            (no_context, mismatched),
        )
        self.assertEqual(classification.context_results, ())
        self.assertEqual(
            classification.returned_results,
            (classification.winner,),
        )
        self.assertEqual(
            tuple(
                record.target_raw
                for record in classification.retained_only_variants
            ),
            ("variant-no-context", "variant-mismatch"),
        )


class ContextSemanticsTests(unittest.TestCase):
    def test_comparison_never_normalizes_folds_or_strips(self) -> None:
        cases = (
            ("Hello", "HELLO"),
            ("Hello", " hello"),
            ("Hello", "Hello "),
            ("Hello", "Hell"),
        )
        for query_value, record_value in cases:
            with self.subTest(query_value=query_value, record_value=record_value):
                query = _query(speaker_raw=query_value)
                record = _variant_record(
                    record_id=1,
                    speaker_raw=record_value,
                )
                evidence = compare_context_v1(query=query, record=record)
                self.assertEqual(
                    evidence.comparable_fields,
                    ("speaker_raw",),
                )
                self.assertEqual(evidence.matched_fields, ())
                self.assertEqual(
                    evidence.mismatched_fields,
                    ("speaker_raw",),
                )
                self.assertEqual(
                    evidence.strength_v1,
                    (0, -1, 0, 0, 0),
                )

    def test_only_builtin_strings_are_comparable(self) -> None:
        query = _query(speaker_raw="Alice")
        record = _variant_record(
            record_id=1,
            speaker_raw=_StrSubclass("Alice"),
        )
        evidence = compare_context_v1(query=query, record=record)
        self.assertEqual(evidence.comparable_fields, ())
        self.assertEqual(evidence.strength_v1, (0, 0, 0, 0, 0))


class StoreSeamTests(unittest.TestCase):
    def test_query_resource_exact_uses_only_the_leased_exact_records_port(
        self,
    ) -> None:
        records = (
            _record(1, target_raw="old"),
            _record(WINNER_RECORD_ID, target_raw="new"),
        )
        store = _StubStore(records=records)
        classification = query_resource_exact(
            handle=_handle(store),
            query=_query(),
        )
        winner = classification.winner
        if winner is None:
            self.fail("expected an exact winner")
        self.assertEqual(winner.record_id, WINNER_RECORD_ID)
        self.assertEqual(winner.target, "new")
        self.assertEqual(store.exact_records_calls, [QUERY_SOURCE])
        self.assertEqual(store.records_by_id_calls, 0)
        self.assertEqual(store.append_calls, 0)
        self.assertEqual(store.export_records_calls, 0)
        self.assertEqual(store.health_calls, 0)

    def test_store_seam_matches_pure_classification_across_orders(self) -> None:
        records = (
            _record(1, target_raw="old"),
            _record(WINNER_RECORD_ID, target_raw="new"),
        )
        query = _query(speaker_raw="Alice")
        first = query_resource_exact(
            handle=_handle(_StubStore(records)),
            query=query,
        )
        second = query_resource_exact(
            handle=_handle(_StubStore(tuple(reversed(records)))),
            query=query,
        )
        self.assertEqual(first, second)
        first_winner = first.winner
        if first_winner is None:
            self.fail("expected an exact winner")
        self.assertEqual(first_winner.record_id, WINNER_RECORD_ID)


class InputValidationTests(unittest.TestCase):
    def test_classify_rejects_non_contract_inputs(self) -> None:
        with self.assertRaises(TypeError):
            classify_exact_context(
                resource_id="tm.primary",
                resource_order=0,
                query=cast(Any, object()),
                records=(),
            )
        with self.assertRaises(TypeError):
            classify_exact_context(
                resource_id="tm.primary",
                resource_order=0,
                query=_query(),
                records=cast(Any, (object(),)),
            )
        with self.assertRaises(TypeError):
            classify_exact_context(
                resource_id="tm.primary",
                resource_order=True,
                query=_query(),
                records=(),
            )
        with self.assertRaises(TypeError):
            classify_exact_context(
                resource_id=cast(Any, 7),
                resource_order=0,
                query=_query(),
                records=(),
            )
        with self.assertRaises(TypeError):
            classify_exact_context(
                resource_id="tm.primary",
                resource_order=0,
                query=_query(),
                records=cast(Any, [_record(1)]),
            )

    def test_classify_rejects_invalid_identity_even_without_records(self) -> None:
        with self.assertRaisesRegex(ValueError, "resource_id must not be empty"):
            classify_exact_context(
                resource_id=" ",
                resource_order=0,
                query=_query(),
                records=(),
            )
        with self.assertRaisesRegex(
            ValueError,
            "resource_order must be non-negative",
        ):
            classify_exact_context(
                resource_id="tm.primary",
                resource_order=-1,
                query=_query(),
                records=(),
            )

    def test_classify_rejects_non_exact_or_ambiguous_record_groups(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "records must belong to the raw exact source",
        ):
            classify_exact_context(
                resource_id="tm.primary",
                resource_order=0,
                query=_query(),
                records=(_record(1, source_raw="Open the door. "),),
            )
        with self.assertRaisesRegex(
            ValueError,
            "records must have unique record ids",
        ):
            classify_exact_context(
                resource_id="tm.primary",
                resource_order=0,
                query=_query(),
                records=(
                    _record(1, target_raw="first"),
                    _record(1, target_raw="second"),
                ),
            )

    def test_compare_context_rejects_non_contract_inputs(self) -> None:
        with self.assertRaises(TypeError):
            compare_context_v1(
                query=cast(Any, object()),
                record=_variant_record(record_id=1),
            )
        with self.assertRaises(TypeError):
            compare_context_v1(
                query=_query(),
                record=cast(Any, object()),
            )

    def test_store_seam_rejects_non_contract_inputs(self) -> None:
        with self.assertRaises(TypeError):
            query_resource_exact(
                handle=cast(Any, object()),
                query=_query(),
            )
        with self.assertRaises(TypeError):
            query_resource_exact(
                handle=_handle(_StubStore(records=())),
                query=cast(Any, object()),
            )


FUZZY_FIXTURE_VERSION = "tm-retrieval-fuzzy-vectors-v1"


def _fuzzy_query(
    *,
    minimum_similarity: float = 0.7,
    limit: int = 10,
) -> TMQuery:
    return TMQuery(
        query_source=QUERY_SOURCE,
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        minimum_similarity=minimum_similarity,
        limit=limit,
        resource_order=("tm.primary",),
    )


def _candidate_report(
    record_ids: tuple[int, ...],
    *,
    resource_id: str = "tm.primary",
    result_limit: int = 10,
    fuzzy_available: bool = True,
) -> CandidateRetrievalReport:
    count = len(record_ids)
    if fuzzy_available:
        stages = (
            CandidateStageMetadata(
                stage=CandidateStage.FTS_TRIGRAM,
                input_count=0,
                added_unique_count=count,
                output_unique_count=count,
                dropped_count=0,
            ),
            CandidateStageMetadata(
                stage=CandidateStage.UNION,
                input_count=count,
                added_unique_count=0,
                output_unique_count=count,
                dropped_count=0,
            ),
            CandidateStageMetadata(
                stage=CandidateStage.DEDUPLICATE,
                input_count=count,
                added_unique_count=0,
                output_unique_count=count,
                dropped_count=0,
            ),
        )
        candidates = tuple(
            CandidateEvidence(
                record_id=record_id,
                recall_stages=(CandidateStage.FTS_TRIGRAM,),
                matched_grams=1,
                query_grams=1,
                overlap_ratio=1.0,
                pretruncate_rank=None,
            )
            for record_id in record_ids
        )
        metadata = CandidateRecallMetadata(
            resource_id=resource_id,
            index_kind="FTS5_TRIGRAM",
            fuzzy_available=True,
            fuzzy_unavailable_code=None,
            stages=stages,
            union_unique_count=count,
            deduplicated_count=count,
            result_limit=result_limit,
            candidate_budget_version=CANDIDATE_BUDGET_VERSION,
            candidate_budget=candidate_budget_v1(result_limit),
            truncated=False,
        )
    else:
        candidates = ()
        metadata = CandidateRecallMetadata(
            resource_id=resource_id,
            index_kind="FTS5_TRIGRAM",
            fuzzy_available=False,
            fuzzy_unavailable_code="FUZZY_GATE.CLOSED",
            stages=(),
            union_unique_count=0,
            deduplicated_count=0,
            result_limit=result_limit,
            candidate_budget_version=CANDIDATE_BUDGET_VERSION,
            candidate_budget=candidate_budget_v1(result_limit),
            truncated=False,
        )
    return CandidateRetrievalReport(candidates=candidates, metadata=metadata)


class _CountingScorer:
    """Scorer-v1 wrapper that records every query/candidate pair."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def score(self, query: str, candidate: str) -> SimilarityEvidence:
        self.calls.append((query, candidate))
        return SimilarityScorerV1().score(query, candidate)


class _BrokenScorer:
    def score(self, query: str, candidate: str) -> SimilarityEvidence:
        return cast(Any, object())


class _InvalidEvidenceScorer:
    def score(self, query: str, candidate: str) -> SimilarityEvidence:
        return SimilarityEvidence(
            levenshtein_ratio=0.5,
            dice_bigram=0.5,
            final_similarity=1.5,
        )


class _MutatingAliasScorer:
    """Scorer that mutates every caller-owned alias it can reach on first call."""

    def __init__(
        self,
        *,
        query: TMQuery,
        records: tuple[TMRecord, ...],
        report: CandidateRetrievalReport,
    ) -> None:
        self.query = query
        self.records = records
        self.report = report
        self.returned_evidences: list[SimilarityEvidence] = []
        self.mutated = False

    def score(self, query: str, candidate: str) -> SimilarityEvidence:
        if not self.mutated:
            object.__setattr__(self.query, "query_source", "MUTATED QUERY")
            object.__setattr__(self.query, "minimum_similarity", 0.99)
            object.__setattr__(self.query, "limit", 1)
            for record in self.records:
                object.__setattr__(record, "source_raw", "MUTATED SOURCE")
                object.__setattr__(record, "target_raw", "MUTATED TARGET")
                object.__setattr__(
                    record,
                    "provenance",
                    (("importer", "mutated"),),
                )
            for candidate_evidence in self.report.candidates:
                object.__setattr__(candidate_evidence, "record_id", 999)
            object.__setattr__(self.report, "candidates", ())
            object.__setattr__(self.report.metadata, "resource_id", "tm.mutated")
            object.__setattr__(self.report.metadata, "result_limit", 1)
            self.mutated = True
        evidence = SimilarityScorerV1().score(query, candidate)
        self.returned_evidences.append(evidence)
        return evidence


class _EvidenceAliasingScorer:
    """Scorer that mutates its previously returned evidence on later calls."""

    def __init__(self) -> None:
        self.returned_evidences: list[SimilarityEvidence] = []
        self.mutated_previous = False

    def score(self, query: str, candidate: str) -> SimilarityEvidence:
        if self.returned_evidences:
            object.__setattr__(self.returned_evidences[0], "final_similarity", 0.0)
            object.__setattr__(self.returned_evidences[0], "levenshtein_ratio", 0.0)
            object.__setattr__(self.returned_evidences[0], "dice_bigram", 0.0)
            self.mutated_previous = True
        evidence = SimilarityScorerV1().score(query, candidate)
        self.returned_evidences.append(evidence)
        return evidence


class _ScoreAttributeRotatingScorer:
    """Scorer whose score attribute returns a fresh callable on every access."""

    def __init__(self) -> None:
        self.score_accesses = 0
        self.calls = 0

    @property
    def score(self) -> Callable[[str, str], SimilarityEvidence]:
        self.score_accesses += 1

        def score(query: str, candidate: str) -> SimilarityEvidence:
            self.calls += 1
            return SimilarityScorerV1().score(query, candidate)

        return score


class _ScorePropertyMutatingScorer:
    """Scorer whose port lookup mutates every caller-owned input alias."""

    def __init__(
        self,
        *,
        query: TMQuery,
        records: tuple[TMRecord, ...],
        report: CandidateRetrievalReport,
    ) -> None:
        self.query = query
        self.records = records
        self.report = report
        self.score_accesses = 0

    @property
    def score(self) -> Callable[[str, str], SimilarityEvidence]:
        self.score_accesses += 1
        object.__setattr__(self.query, "query_source", "MUTATED QUERY")
        object.__setattr__(self.query, "minimum_similarity", 0.99)
        for record in self.records:
            object.__setattr__(record, "source_raw", "MUTATED SOURCE")
            object.__setattr__(record, "target_raw", "MUTATED TARGET")
            object.__setattr__(
                record,
                "provenance",
                (("importer", "mutated"),),
            )
        object.__setattr__(self.report, "candidates", ())
        object.__setattr__(self.report.metadata, "resource_id", "tm.mutated")
        return SimilarityScorerV1().score


class FuzzyScoringTests(unittest.TestCase):
    def test_fixture_version_is_frozen(self) -> None:
        self.assertEqual(FUZZY_FIXTURE_VERSION, "tm-retrieval-fuzzy-vectors-v1")

    def test_threshold_boundary_equality_is_accepted_and_below_is_excluded(
        self,
    ) -> None:
        records = (
            _record(1, source_raw="Open the door", target_raw="high"),
            _record(2, source_raw="Close the window.", target_raw="mid"),
            _record(3, source_raw="zzz zzz zzz", target_raw="low"),
        )
        report = _candidate_report((1, 2, 3))
        boundary = (
            SimilarityScorerV1()
            .score(QUERY_SOURCE, "Close the window.")
            .final_similarity
        )
        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(minimum_similarity=boundary),
            report=report,
            records=records,
        )
        self.assertEqual(
            tuple(item.record_id for item in result.accepted),
            (1, 2),
        )
        self.assertEqual(result.scored_count, 3)
        for item in result.accepted:
            self.assertGreaterEqual(item.similarity, boundary)

        strict = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(
                minimum_similarity=(
                    SimilarityScorerV1()
                    .score(QUERY_SOURCE, "Open the door")
                    .final_similarity
                )
            ),
            report=report,
            records=records,
        )
        self.assertEqual(
            tuple(item.record_id for item in strict.accepted),
            (1,),
        )

    def test_zero_threshold_accepts_all_and_one_threshold_accepts_none(
        self,
    ) -> None:
        records = (
            _record(1, source_raw="Open the door", target_raw="high"),
            _record(2, source_raw="Close the window.", target_raw="mid"),
        )
        report = _candidate_report((1, 2))
        all_results = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(minimum_similarity=0.0),
            report=report,
            records=records,
        )
        self.assertEqual(
            tuple(item.record_id for item in all_results.accepted),
            (1, 2),
        )
        none_results = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(minimum_similarity=1.0),
            report=report,
            records=records,
        )
        self.assertEqual(none_results.accepted, ())
        self.assertEqual(none_results.scored_count, 2)

    def test_accepted_results_retain_query_and_matched_sources(self) -> None:
        record = _record(7, source_raw="Open the door", target_raw="开门。")
        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=3,
            query=_fuzzy_query(),
            report=_candidate_report((7,)),
            records=(record,),
        )
        self.assertEqual(len(result.accepted), 1)
        item = result.accepted[0]
        self.assertEqual(item.resource_id, "tm.primary")
        self.assertEqual(item.record_id, 7)
        self.assertEqual(item.query_source, QUERY_SOURCE)
        self.assertEqual(item.matched_source, "Open the door")
        self.assertNotEqual(item.query_source, item.matched_source)
        self.assertEqual(item.target, "开门。")
        self.assertEqual(item.match_type, TMMatchType.FUZZY)
        self.assertEqual(item.stable_tie_key, (3, 7))
        self.assertEqual(item.context_evidence.matched_fields, ())
        self.assertEqual(
            item.provenance,
            (("importer", "legacy-7"),),
        )
        expected = SimilarityScorerV1().score(QUERY_SOURCE, "Open the door")
        self.assertIsNotNone(item.similarity_evidence)
        if item.similarity_evidence is not None:
            self.assertEqual(item.similarity_evidence, expected)
            self.assertEqual(item.similarity, expected.final_similarity)
            self.assertGreaterEqual(item.similarity, 0.0)
            self.assertLessEqual(item.similarity, 1.0)

    def test_same_source_candidates_are_excluded_without_scoring(self) -> None:
        records = (
            _record(1, source_raw=QUERY_SOURCE, target_raw="exact-target"),
            _record(2, source_raw="Open the door", target_raw="fuzzy-target"),
        )
        scorer = _CountingScorer()
        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(),
            report=_candidate_report((1, 2)),
            records=records,
            scorer=scorer,
        )
        self.assertEqual(
            tuple(item.record_id for item in result.accepted),
            (2,),
        )
        self.assertEqual(result.scored_count, 1)
        self.assertEqual(scorer.calls, [(QUERY_SOURCE, "Open the door")])

    def test_order_is_final_similarity_desc_then_record_id_desc(self) -> None:
        records = (
            _record(1, source_raw="Close the door.", target_raw="first"),
            _record(2, source_raw="Close the door.", target_raw="second"),
            _record(3, source_raw="Open the door", target_raw="third"),
        )
        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(minimum_similarity=0.0),
            report=_candidate_report((1, 2, 3)),
            records=records,
        )
        self.assertEqual(
            tuple(item.record_id for item in result.accepted),
            (3, 2, 1),
        )
        scores = tuple(item.similarity for item in result.accepted)
        self.assertEqual(scores, tuple(sorted(scores, reverse=True)))

    def test_accepted_is_unbounded_by_query_limit_for_global_slice(self) -> None:
        records = tuple(
            _record(
                record_id,
                source_raw=f"Open the door {record_id}",
                target_raw=f"target-{record_id}",
            )
            for record_id in (1, 2, 3, 4, 5)
        )
        report = _candidate_report((1, 2, 3, 4, 5), result_limit=2)
        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(minimum_similarity=0.0, limit=2),
            report=report,
            records=records,
        )
        self.assertEqual(len(result.accepted), 5)
        self.assertEqual(report.metadata.result_limit, 2)
        self.assertEqual(
            report.metadata.candidate_budget,
            candidate_budget_v1(2),
        )

    def test_empty_candidates_return_empty_accepted(self) -> None:
        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(),
            report=_candidate_report(()),
            records=(),
        )
        self.assertEqual(result.accepted, ())
        self.assertEqual(result.scored_count, 0)

    def test_fuzzy_unavailable_report_scores_nothing(self) -> None:
        scorer = _CountingScorer()
        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(),
            report=_candidate_report((), fuzzy_available=False),
            records=(),
            scorer=scorer,
        )
        self.assertEqual(result.accepted, ())
        self.assertEqual(result.scored_count, 0)
        self.assertEqual(scorer.calls, [])

    def test_repeated_scoring_is_deterministic_without_mutating_inputs(
        self,
    ) -> None:
        records = (
            _record(1, source_raw="Open the door", target_raw="first"),
            _record(2, source_raw="Close the window.", target_raw="second"),
        )
        report = _candidate_report((1, 2))
        query = _fuzzy_query(minimum_similarity=0.0)
        first = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=query,
            report=report,
            records=records,
        )
        second = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=query,
            report=report,
            records=records,
        )
        self.assertEqual(first, second)
        self.assertEqual(report, _candidate_report((1, 2)))
        self.assertEqual(query, _fuzzy_query(minimum_similarity=0.0))
        self.assertEqual(records, (records[0], records[1]))
        self.assertEqual(
            tuple(item.provenance for item in first.accepted),
            (
                (("importer", "legacy-1"),),
                (("importer", "legacy-2"),),
            ),
        )


class FuzzyIdentitySafetyTests(unittest.TestCase):
    def test_duplicate_record_identity_in_batch_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "records must have unique record ids",
        ):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=_candidate_report((1,)),
                records=(
                    _record(1, target_raw="first"),
                    _record(1, target_raw="second"),
                ),
            )

    def test_missing_candidate_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "records must correspond exactly to candidate ids",
        ):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=_candidate_report((1, 2)),
                records=(_record(1),),
            )

    def test_foreign_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "records must correspond exactly to candidate ids",
        ):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=_candidate_report((1,)),
                records=(_record(1), _record(9)),
            )

    def test_report_resource_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "report must belong to resource_id",
        ):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=_candidate_report((1,), resource_id="tm.other"),
                records=(_record(1),),
            )

    def test_report_result_limit_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "report result limit must equal query limit",
        ):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(limit=2),
                report=_candidate_report((1,), result_limit=10),
                records=(_record(1),),
            )

    def test_malformed_scorer_evidence_fails_closed(self) -> None:
        with self.assertRaises(TypeError):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=_candidate_report((1,)),
                records=(_record(1, source_raw="Open the door"),),
                scorer=_BrokenScorer(),
            )
        with self.assertRaisesRegex(
            ValueError,
            "final similarity must be between 0 and 1",
        ):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=_candidate_report((1,)),
                records=(_record(1, source_raw="Open the door"),),
                scorer=_InvalidEvidenceScorer(),
            )

    def test_non_contract_inputs_are_rejected(self) -> None:
        report = _candidate_report((1,))
        records = (_record(1, source_raw="Open the door"),)
        with self.assertRaises(TypeError):
            score_fuzzy_candidates(
                resource_id=cast(Any, 7),
                resource_order=0,
                query=_fuzzy_query(),
                report=report,
                records=records,
            )
        with self.assertRaises(TypeError):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=True,
                query=_fuzzy_query(),
                report=report,
                records=records,
            )
        with self.assertRaises(TypeError):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=cast(Any, object()),
                report=report,
                records=records,
            )
        with self.assertRaises(TypeError):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=cast(Any, object()),
                records=records,
            )
        with self.assertRaises(TypeError):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=report,
                records=cast(Any, [records[0]]),
            )
        with self.assertRaises(TypeError):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=report,
                records=cast(Any, (object(),)),
            )

    def test_scorer_without_score_port_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=_candidate_report((1,)),
                records=(_record(1, source_raw="Open the door"),),
                scorer=cast(Any, object()),
            )

    def test_fuzzy_result_rejects_empty_identity_and_negative_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "resource_id must not be empty"):
            score_fuzzy_candidates(
                resource_id=" ",
                resource_order=0,
                query=_fuzzy_query(),
                report=_candidate_report(()),
                records=(),
            )
        with self.assertRaisesRegex(
            ValueError,
            "resource_order must be non-negative",
        ):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=-1,
                query=_fuzzy_query(),
                report=_candidate_report(()),
                records=(),
            )


class FuzzyScoringResultValidationTests(unittest.TestCase):
    def _scored_result(self) -> FuzzyScoringResult:
        records = (
            _record(1, source_raw="Open the door", target_raw="first"),
            _record(2, source_raw="Close the window.", target_raw="second"),
        )
        return score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(minimum_similarity=0.0),
            report=_candidate_report((1, 2)),
            records=records,
        )

    def test_duplicate_resource_record_pair_is_rejected(self) -> None:
        scored = self._scored_result()
        duplicate = scored.accepted[:1] + scored.accepted[:1]
        with self.assertRaisesRegex(
            ValueError,
            "accepted results must be deduplicated",
        ):
            FuzzyScoringResult(
                resource_id="tm.primary",
                resource_order=0,
                accepted=duplicate,
                scored_count=2,
            )

    def test_foreign_resource_result_is_rejected(self) -> None:
        scored = self._scored_result()
        foreign = (
            TMResult(
                resource_id="tm.other",
                record_id=scored.accepted[0].record_id,
                query_source=QUERY_SOURCE,
                matched_source=scored.accepted[0].matched_source,
                target=scored.accepted[0].target,
                match_type=TMMatchType.FUZZY,
                similarity=scored.accepted[0].similarity,
                similarity_evidence=scored.accepted[0].similarity_evidence,
                context_evidence=scored.accepted[0].context_evidence,
                provenance=scored.accepted[0].provenance,
                stable_tie_key=(9, scored.accepted[0].record_id),
            ),
        )
        with self.assertRaisesRegex(
            ValueError,
            "accepted result resource id must match",
        ):
            FuzzyScoringResult(
                resource_id="tm.primary",
                resource_order=0,
                accepted=foreign,
                scored_count=1,
            )

    def test_non_fuzzy_result_is_rejected(self) -> None:
        exact = TMResult(
            resource_id="tm.primary",
            record_id=WINNER_RECORD_ID,
            query_source=QUERY_SOURCE,
            matched_source=QUERY_SOURCE,
            target="target",
            match_type=TMMatchType.EXACT,
            similarity=1.0,
            similarity_evidence=None,
            context_evidence=ContextEvidence(
                comparable_fields=(),
                matched_fields=(),
                mismatched_fields=(),
                strength_v1=(0, 0, 0, 0, 0),
            ),
            provenance=(("importer", "legacy-100"),),
            stable_tie_key=(0, WINNER_RECORD_ID),
        )
        with self.assertRaisesRegex(ValueError, "accepted results must be fuzzy"):
            FuzzyScoringResult(
                resource_id="tm.primary",
                resource_order=0,
                accepted=(exact,),
                scored_count=1,
            )

    def test_negative_scored_count_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "scored_count must be non-negative",
        ):
            FuzzyScoringResult(
                resource_id="tm.primary",
                resource_order=0,
                accepted=(),
                scored_count=-1,
            )

    def test_non_contract_fields_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            FuzzyScoringResult(
                resource_id=cast(Any, 7),
                resource_order=0,
                accepted=(),
                scored_count=0,
            )
        with self.assertRaises(TypeError):
            FuzzyScoringResult(
                resource_id="tm.primary",
                resource_order=True,
                accepted=(),
                scored_count=0,
            )
        with self.assertRaises(TypeError):
            FuzzyScoringResult(
                resource_id="tm.primary",
                resource_order=0,
                accepted=cast(Any, []),
                scored_count=0,
            )
        with self.assertRaises(TypeError):
            FuzzyScoringResult(
                resource_id="tm.primary",
                resource_order=0,
                accepted=(),
                scored_count=True,
            )


class FuzzyCallbackTOCTOUTests(unittest.TestCase):
    """A scorer mutating aliased caller-owned values must not corrupt results."""

    def _honest_result(self) -> FuzzyScoringResult:
        return score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(minimum_similarity=0.5),
            report=_candidate_report((1, 2, 3)),
            records=(
                _record(1, source_raw="Open the door", target_raw="first"),
                _record(2, source_raw=QUERY_SOURCE, target_raw="second"),
                _record(3, source_raw="Close the window.", target_raw="third"),
            ),
            scorer=SimilarityScorerV1(),
        )

    def test_result_stays_bound_to_pre_callback_values(self) -> None:
        honest = self._honest_result()
        query = _fuzzy_query(minimum_similarity=0.5)
        records = (
            _record(1, source_raw="Open the door", target_raw="first"),
            _record(2, source_raw=QUERY_SOURCE, target_raw="second"),
            _record(3, source_raw="Close the window.", target_raw="third"),
        )
        report = _candidate_report((1, 2, 3))
        scorer = _MutatingAliasScorer(
            query=query,
            records=records,
            report=report,
        )
        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=query,
            report=report,
            records=records,
            scorer=scorer,
        )

        self.assertTrue(scorer.mutated)
        self.assertEqual(query.query_source, "MUTATED QUERY")
        self.assertEqual(query.minimum_similarity, 0.99)
        self.assertEqual(records[1].source_raw, "MUTATED SOURCE")
        self.assertEqual(records[0].provenance, (("importer", "mutated"),))
        self.assertEqual(report.candidates, ())
        self.assertEqual(report.metadata.resource_id, "tm.mutated")
        self.assertEqual(result, honest)
        self.assertEqual(result.scored_count, 2)
        self.assertEqual(
            tuple(item.record_id for item in result.accepted),
            (1,),
        )
        accepted = result.accepted[0]
        self.assertEqual(accepted.query_source, QUERY_SOURCE)
        self.assertEqual(accepted.matched_source, "Open the door")
        self.assertEqual(accepted.target, "first")
        self.assertEqual(accepted.provenance, (("importer", "legacy-1"),))
        self.assertGreaterEqual(accepted.similarity, 0.5)
        self.assertLess(accepted.similarity, 0.99)

    def test_evidence_aliases_are_not_retained_in_results(self) -> None:
        honest = self._honest_result()
        scorer = _EvidenceAliasingScorer()
        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(minimum_similarity=0.5),
            report=_candidate_report((1, 2, 3)),
            records=(
                _record(1, source_raw="Open the door", target_raw="first"),
                _record(2, source_raw=QUERY_SOURCE, target_raw="second"),
                _record(3, source_raw="Close the window.", target_raw="third"),
            ),
            scorer=scorer,
        )

        self.assertTrue(scorer.mutated_previous)
        self.assertEqual(len(scorer.returned_evidences), 2)
        for item in result.accepted:
            self.assertIsNot(
                item.similarity_evidence,
                scorer.returned_evidences[0],
            )
        self.assertEqual(result, honest)
        for alias in scorer.returned_evidences:
            object.__setattr__(alias, "final_similarity", 0.0)
            object.__setattr__(alias, "levenshtein_ratio", 0.0)
            object.__setattr__(alias, "dice_bigram", 0.0)
        self.assertEqual(result, honest)

    def test_mutated_fuzzy_unavailable_with_candidates_fails_before_scoring(
        self,
    ) -> None:
        report = _candidate_report((1,))
        object.__setattr__(report.metadata, "fuzzy_available", False)
        object.__setattr__(
            report.metadata,
            "fuzzy_unavailable_code",
            "FUZZY_GATE.CLOSED",
        )
        object.__setattr__(report.metadata, "stages", ())
        object.__setattr__(report.metadata, "union_unique_count", 0)
        object.__setattr__(report.metadata, "deduplicated_count", 0)
        object.__setattr__(report.metadata, "truncated", False)
        scorer = _CountingScorer()
        with self.assertRaisesRegex(
            ValueError,
            "fuzzy unavailable recall must return empty candidates",
        ):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=report,
                records=(_record(1, source_raw="Open the door"),),
                scorer=scorer,
            )
        self.assertEqual(scorer.calls, [])

    def test_mutated_duplicate_candidate_ids_fail_before_scoring(self) -> None:
        report = _candidate_report((1, 2))
        object.__setattr__(
            report,
            "candidates",
            (
                CandidateEvidence(
                    record_id=1,
                    recall_stages=(CandidateStage.FTS_TRIGRAM,),
                    matched_grams=1,
                    query_grams=1,
                    overlap_ratio=1.0,
                    pretruncate_rank=None,
                ),
                CandidateEvidence(
                    record_id=1,
                    recall_stages=(CandidateStage.FTS_TRIGRAM,),
                    matched_grams=1,
                    query_grams=1,
                    overlap_ratio=1.0,
                    pretruncate_rank=None,
                ),
            ),
        )
        scorer = _CountingScorer()
        with self.assertRaisesRegex(
            ValueError,
            "candidate values must have unique record ids",
        ):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=report,
                records=(_record(1, source_raw="Open the door"),),
                scorer=scorer,
            )
        self.assertEqual(scorer.calls, [])

    def test_mutated_stage_counts_fail_before_scoring(self) -> None:
        report = _candidate_report((1, 2))
        object.__setattr__(
            report.metadata.stages[1],
            "output_unique_count",
            report.metadata.stages[1].output_unique_count + 1,
        )
        scorer = _CountingScorer()
        with self.assertRaisesRegex(
            ValueError,
            "candidate stage counts must conserve input and output",
        ):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=report,
                records=(
                    _record(1, source_raw="Open the door"),
                    _record(2, source_raw="Close the window."),
                ),
                scorer=scorer,
            )
        self.assertEqual(scorer.calls, [])

    def test_mutated_evidence_counts_fail_before_scoring(self) -> None:
        report = _candidate_report((1, 2))
        object.__setattr__(report.candidates[0], "matched_grams", 2)
        scorer = _CountingScorer()
        with self.assertRaisesRegex(
            ValueError,
            "matched grams must not exceed query grams",
        ):
            score_fuzzy_candidates(
                resource_id="tm.primary",
                resource_order=0,
                query=_fuzzy_query(),
                report=report,
                records=(
                    _record(1, source_raw="Open the door"),
                    _record(2, source_raw="Close the window."),
                ),
                scorer=scorer,
            )
        self.assertEqual(scorer.calls, [])

    def test_score_callable_is_captured_once_before_iteration(self) -> None:
        honest = self._honest_result()
        scorer = cast(Any, _ScoreAttributeRotatingScorer())
        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_fuzzy_query(minimum_similarity=0.5),
            report=_candidate_report((1, 2, 3)),
            records=(
                _record(1, source_raw="Open the door", target_raw="first"),
                _record(2, source_raw=QUERY_SOURCE, target_raw="second"),
                _record(3, source_raw="Close the window.", target_raw="third"),
            ),
            scorer=scorer,
        )
        self.assertEqual(scorer.score_accesses, 1)
        self.assertEqual(scorer.calls, 2)
        self.assertEqual(result, honest)

    def test_score_port_lookup_runs_only_after_private_input_snapshot(self) -> None:
        honest = self._honest_result()
        query = _fuzzy_query(minimum_similarity=0.5)
        records = (
            _record(1, source_raw="Open the door", target_raw="first"),
            _record(2, source_raw=QUERY_SOURCE, target_raw="second"),
            _record(3, source_raw="Close the window.", target_raw="third"),
        )
        report = _candidate_report((1, 2, 3))
        scorer = cast(
            Any,
            _ScorePropertyMutatingScorer(
                query=query,
                records=records,
                report=report,
            ),
        )

        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=query,
            report=report,
            records=records,
            scorer=scorer,
        )

        self.assertEqual(scorer.score_accesses, 1)
        self.assertEqual(query.query_source, "MUTATED QUERY")
        self.assertEqual(records[0].source_raw, "MUTATED SOURCE")
        self.assertEqual(report.candidates, ())
        self.assertEqual(result, honest)

    def test_post_call_mutation_of_caller_aliases_does_not_corrupt_result(
        self,
    ) -> None:
        honest = self._honest_result()
        query = _fuzzy_query(minimum_similarity=0.5)
        records = (
            _record(1, source_raw="Open the door", target_raw="first"),
            _record(2, source_raw=QUERY_SOURCE, target_raw="second"),
            _record(3, source_raw="Close the window.", target_raw="third"),
        )
        report = _candidate_report((1, 2, 3))
        scorer = _MutatingAliasScorer(
            query=query,
            records=records,
            report=report,
        )
        result = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=query,
            report=report,
            records=records,
            scorer=scorer,
        )

        for record in records:
            object.__setattr__(record, "source_raw", "MUTATED SOURCE")
            object.__setattr__(record, "target_raw", "MUTATED TARGET")
            object.__setattr__(record, "provenance", (("importer", "mutated"),))
        object.__setattr__(query, "query_source", "MUTATED QUERY")
        object.__setattr__(query, "minimum_similarity", 0.99)
        object.__setattr__(report, "candidates", ())
        for alias in scorer.returned_evidences:
            object.__setattr__(alias, "final_similarity", 0.0)
            object.__setattr__(alias, "scorer_version", "MUTATED")

        self.assertEqual(result, honest)
        for item in result.accepted:
            self.assertIsNot(
                item.similarity_evidence,
                scorer.returned_evidences[0],
            )
            self.assertIsNot(item.provenance, records[0].provenance)


if __name__ == "__main__":
    unittest.main()


# --- Task 7.3 service fixtures ----------------------------------------------

SERVICE_FIXTURE_VERSION = "tm-retrieval-service-vectors-v1"


def _service_health(
    *,
    healthy: bool = True,
    exact_available: bool = True,
    context_available: bool = False,
    fuzzy_available: bool = False,
    index_kind: str = "FTS5_TRIGRAM",
    generation: int = 0,
) -> StoreHealth:
    return StoreHealth(
        healthy=healthy,
        schema_version=1,
        generation=generation,
        record_count=0,
        index_kind=index_kind,
        snapshot_binding_digest=None,
        source_binding_state=None,
        exact_available=exact_available,
        context_available=context_available,
        fuzzy_available=fuzzy_available,
        diagnostic_codes=(),
    )


def _service_query(
    *,
    resource_order: tuple[str, ...],
    limit: int = 10,
    minimum_similarity: float = 0.7,
) -> TMQuery:
    return TMQuery(
        query_source=QUERY_SOURCE,
        speaker_raw="speaker",
        context_prev_raw="prev",
        context_next_raw="next",
        minimum_similarity=minimum_similarity,
        limit=limit,
        resource_order=resource_order,
    )


def _service_handle(
    store: TMStore,
    *,
    resource_id: str,
    order: int,
    active: bool = True,
    lookup: bool = True,
    update: bool = True,
) -> TMResourceHandle:
    return TMResourceHandle(
        resource_id=resource_id,
        store=store,
        active=active,
        lookup=lookup,
        update=update,
        order=order,
    )


def _evidence(score: float) -> SimilarityEvidence:
    return SimilarityEvidence(
        levenshtein_ratio=score,
        dice_bigram=score,
        final_similarity=score,
    )


def _result_identity(result: TMResult) -> tuple[Any, ...]:
    return (
        result.resource_id,
        result.record_id,
        result.match_type.value,
        result.similarity,
    )


class _ServiceQueryView:
    """Bounded fake of the read-only query view seam used by the service."""

    def __init__(
        self,
        store: _ServiceStore,
        *,
        health: StoreHealth,
        exact_records: tuple[TMRecord, ...] = (),
        batch_records: tuple[TMRecord, ...] = (),
        generation: int | None = None,
        fail_health: Exception | None = None,
        fail_exact: Exception | None = None,
        fail_records: Exception | None = None,
        on_health: Callable[[], None] | None = None,
        on_exact: Callable[[], None] | None = None,
        on_records: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._resource_id = store.resource_id
        self._health = health
        self._exact_records = exact_records
        self._batch_records = batch_records
        self._generation = (
            health.generation if generation is None else generation
        )
        self._fail_health = fail_health
        self._fail_exact = fail_exact
        self._fail_records = fail_records
        self._on_health = on_health
        self._on_exact = on_exact
        self._on_records = on_records
        self.expired = False
        self.health_calls = 0
        self.exact_calls = 0
        self.records_calls = 0
        self.records_requested: list[tuple[int, ...]] = []
        self.exact_sources: list[str] = []

    def _check_not_expired(self) -> None:
        if self.expired:
            raise SQLiteStoreLifecycleError(
                "STORE.QUERY_VIEW_EXPIRED",
                resource_id=self._resource_id,
                generation=0,
                retryable=False,
            )

    @property
    def resource_id(self) -> str:
        self._check_not_expired()
        return self._resource_id

    @property
    def generation(self) -> int:
        self._check_not_expired()
        return self._generation

    def health(self) -> StoreHealth:
        self._check_not_expired()
        self.health_calls += 1
        if self._fail_health is not None:
            raise self._fail_health
        if self._on_health is not None:
            self._on_health()
        return self._health

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        self._check_not_expired()
        self.exact_calls += 1
        self.exact_sources.append(source_raw)
        if self._fail_exact is not None:
            raise self._fail_exact
        if self._on_exact is not None:
            self._on_exact()
        return self._exact_records

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]:
        self._check_not_expired()
        self.records_calls += 1
        self.records_requested.append(record_ids)
        if self._fail_records is not None:
            raise self._fail_records
        if self._on_records is not None:
            self._on_records()
        return self._batch_records


class _RotatingPortsQueryView:
    """Query view whose exact/records port attributes rotate on access."""

    def __init__(
        self,
        store: _ServiceStore,
        *,
        health: StoreHealth,
        exact_records: tuple[TMRecord, ...] = (),
        batch_records: tuple[TMRecord, ...] = (),
        generation: int | None = None,
        raise_records_port: bool = False,
    ) -> None:
        self._base = _ServiceQueryView(
            store,
            health=health,
            exact_records=exact_records,
            batch_records=batch_records,
            generation=generation,
        )
        self.exact_port_accesses = 0
        self.records_port_accesses = 0
        self.exact_calls = 0
        self.records_calls = 0
        self._raise_records_port = raise_records_port

    @property
    def resource_id(self) -> str:
        return self._base.resource_id

    @property
    def generation(self) -> int:
        return self._base.generation

    def health(self) -> StoreHealth:
        return self._base.health()

    @property
    def exact_records(self) -> Callable[[str], tuple[TMRecord, ...]]:
        self.exact_port_accesses += 1

        def port(source_raw: str) -> tuple[TMRecord, ...]:
            self._base._check_not_expired()
            self.exact_calls += 1
            return self._base._exact_records

        return port

    @property
    def records_by_id(
        self,
    ) -> Callable[[tuple[int, ...]], tuple[TMRecord, ...]]:
        self.records_port_accesses += 1
        if self._raise_records_port:
            raise RuntimeError("boom records port")

        def port(
            record_ids: tuple[int, ...],
        ) -> tuple[TMRecord, ...]:
            self._base._check_not_expired()
            self.records_calls += 1
            return self._base._batch_records

        return port


class _ServiceStore:
    """Bounded fake store exposing only the query_lease retrieval seam."""

    def __init__(
        self,
        *,
        resource_id: str = "tm.primary",
        view: _ServiceQueryView | None = None,
        lease_error: Exception | None = None,
    ) -> None:
        self.resource_id = resource_id
        self._view = view
        self.lease_error = lease_error
        self.lease_entries = 0
        self.lease_exits = 0
        self.issued_views: list[_ServiceQueryView] = []
        self.public_exact_calls = 0
        self.public_records_calls = 0
        self.public_health_calls = 0
        self.append_calls = 0
        self.export_calls = 0

    @contextmanager
    def query_lease(self) -> Iterator[_ServiceQueryView]:
        self.lease_entries += 1
        if self.lease_error is not None:
            raise self.lease_error
        view = self._view
        if view is None:
            raise AssertionError("fake store has no configured view")
        try:
            self.issued_views.append(view)
            yield view
        finally:
            self.lease_exits += 1
            view.expired = True

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        self.public_exact_calls += 1
        raise AssertionError("retrieval must not call the public store port")

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]:
        self.public_records_calls += 1
        raise AssertionError("retrieval must not call the public store port")

    def append(self, draft: TMRecordDraft) -> TMRecord:
        self.append_calls += 1
        raise AssertionError("retrieval must not append")

    def export_records(self) -> Iterator[TMRecord]:
        self.export_calls += 1
        raise AssertionError("retrieval must not export")

    def health(self) -> StoreHealth:
        self.public_health_calls += 1
        raise AssertionError("retrieval must not call the public health port")


class _ServiceRetriever:
    """Bounded CandidateRetriever fake recording its consumed seam inputs."""

    def __init__(
        self,
        report: CandidateRetrievalReport | None = None,
        *,
        report_by_resource: dict[str, CandidateRetrievalReport] | None = None,
        error_by_resource: dict[str, Exception] | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.report = report
        self.report_by_resource = report_by_resource or {}
        self.error_by_resource = error_by_resource or {}
        self.on_call = on_call
        self.calls = 0
        self.resource_ids: list[str] = []
        self.view_arguments: list[object] = []
        self.folded_queries: list[str] = []
        self.result_limits: list[int] = []

    def candidates_from_view(
        self,
        resource_id: str,
        view: object,
        folded_query: str,
        *,
        result_limit: int,
    ) -> CandidateRetrievalReport:
        self.calls += 1
        self.resource_ids.append(resource_id)
        self.view_arguments.append(view)
        self.folded_queries.append(folded_query)
        self.result_limits.append(result_limit)
        if self.on_call is not None:
            self.on_call()
        error = self.error_by_resource.get(resource_id)
        if error is not None:
            raise error
        selected = self.report_by_resource.get(resource_id, self.report)
        if selected is None:
            raise AssertionError("fake retriever has no configured report")
        return selected


class _RotatingPortRetriever:
    """Retriever whose candidates_from_view port rotates on every access."""

    def __init__(
        self,
        report: CandidateRetrievalReport,
        *,
        report_by_resource: dict[str, CandidateRetrievalReport] | None = None,
        on_access: Callable[[], None] | None = None,
    ) -> None:
        self.report = report
        self.report_by_resource = report_by_resource or {}
        self.on_access = on_access
        self.accesses = 0
        self.calls = 0

    @property
    def candidates_from_view(
        self,
    ) -> Callable[..., CandidateRetrievalReport]:
        self.accesses += 1
        if self.on_access is not None:
            self.on_access()

        def port(
            resource_id: str,
            view: object,
            folded_query: str,
            *,
            result_limit: int,
        ) -> CandidateRetrievalReport:
            self.calls += 1
            return self.report_by_resource.get(resource_id, self.report)

        return port


class _FixedEvidenceScorer:
    """Scorer returning configured evidence per candidate source."""

    def __init__(self, evidence_by_source: dict[str, SimilarityEvidence]) -> None:
        self.evidence_by_source = evidence_by_source
        self.calls: list[tuple[str, str]] = []

    def score(self, query: str, candidate: str) -> SimilarityEvidence:
        self.calls.append((query, candidate))
        evidence = self.evidence_by_source.get(candidate)
        if evidence is not None:
            return evidence
        return SimilarityScorerV1().score(query, candidate)


class _RaisingScorer:
    """Scorer that raises for one configured candidate source."""

    def __init__(
        self,
        *,
        fail_candidate: str,
        evidence: SimilarityEvidence,
    ) -> None:
        self.fail_candidate = fail_candidate
        self.evidence = evidence

    def score(self, query: str, candidate: str) -> SimilarityEvidence:
        if candidate == self.fail_candidate:
            raise RuntimeError("boom score")
        return self.evidence


# --- Task 7.3 service tests -------------------------------------------------


class ServiceFixtureTests(unittest.TestCase):
    def test_fixture_version_is_frozen(self) -> None:
        self.assertEqual(SERVICE_FIXTURE_VERSION, "tm-retrieval-service-vectors-v1")


class TMRetrievalServiceAggregationTests(unittest.TestCase):
    def _build(
        self,
        limit: int,
    ) -> tuple[
        list[_ServiceStore],
        _ServiceRetriever,
        _FixedEvidenceScorer,
    ]:
        primary = _ServiceStore(resource_id="tm.primary")
        primary_view = _ServiceQueryView(
            primary,
            health=_service_health(
                context_available=True,
                fuzzy_available=True,
            ),
            exact_records=(
                _record(100, speaker_raw="speaker"),
                _record(90, target_raw="variant-a", speaker_raw="speaker"),
                _record(80, target_raw="variant-b"),
            ),
            batch_records=(
                _record(
                    501,
                    source_raw="Open the door quickly.",
                    target_raw="fuzzy-a",
                ),
                _record(
                    502,
                    source_raw="Open the door slowly.",
                    target_raw="fuzzy-b",
                ),
            ),
        )
        primary._view = primary_view
        secondary = _ServiceStore(resource_id="tm.secondary")
        secondary_view = _ServiceQueryView(
            secondary,
            health=_service_health(
                context_available=True,
                fuzzy_available=True,
            ),
            exact_records=(
                _record(200, speaker_raw="speaker"),
                _record(
                    190,
                    target_raw="variant-c",
                    speaker_raw="speaker",
                    context_prev_raw="prev",
                ),
            ),
            batch_records=(
                _record(
                    601,
                    source_raw="Open the gate widely.",
                    target_raw="fuzzy-c",
                ),
                _record(
                    602,
                    source_raw="Open the gate narrowly.",
                    target_raw="fuzzy-d",
                ),
            ),
        )
        secondary._view = secondary_view
        retriever = _ServiceRetriever(
            report_by_resource={
                "tm.primary": _candidate_report(
                    (501, 502),
                    resource_id="tm.primary",
                    result_limit=limit,
                ),
                "tm.secondary": _candidate_report(
                    (601, 602),
                    resource_id="tm.secondary",
                    result_limit=limit,
                ),
            }
        )
        scorer = _FixedEvidenceScorer(
            {
                "Open the door quickly.": _evidence(0.9),
                "Open the door slowly.": _evidence(0.8),
                "Open the gate widely.": _evidence(0.95),
                "Open the gate narrowly.": _evidence(0.85),
            }
        )
        return [primary, secondary], retriever, scorer

    def test_full_aggregation_order_is_exact_context_then_fuzzy(self) -> None:
        stores, retriever, scorer = self._build(10)
        query = _service_query(
            resource_order=("tm.primary", "tm.secondary"),
            limit=10,
        )
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=cast(Any, scorer),
        )
        report = service.query(
            (
                _service_handle(
                    stores[0],
                    resource_id="tm.primary",
                    order=0,
                ),
                _service_handle(
                    stores[1],
                    resource_id="tm.secondary",
                    order=1,
                ),
            ),
            query,
        )
        self.assertEqual(
            [_result_identity(result) for result in report.results],
            [
                ("tm.primary", 100, "EXACT", 1.0),
                ("tm.secondary", 200, "EXACT", 1.0),
                ("tm.secondary", 190, "CONTEXT", 1.0),
                ("tm.primary", 90, "CONTEXT", 1.0),
                ("tm.secondary", 601, "FUZZY", 0.95),
                ("tm.primary", 501, "FUZZY", 0.9),
                ("tm.secondary", 602, "FUZZY", 0.85),
                ("tm.primary", 502, "FUZZY", 0.8),
            ],
        )
        self.assertEqual(report.resource_failures, ())
        self.assertEqual(len(report.resource_metadata), 2)
        primary_metadata = report.resource_metadata[0]
        secondary_metadata = report.resource_metadata[1]
        self.assertEqual(primary_metadata.resource_id, "tm.primary")
        self.assertTrue(primary_metadata.context_available)
        self.assertIsNone(primary_metadata.context_unavailable_code)
        self.assertTrue(primary_metadata.recall.fuzzy_available)
        self.assertEqual(primary_metadata.scored_count, 2)
        self.assertEqual(primary_metadata.returned_count, 4)
        self.assertEqual(secondary_metadata.scored_count, 2)
        self.assertEqual(secondary_metadata.returned_count, 4)

    def test_global_limit_is_applied_only_after_cross_resource_aggregation(
        self,
    ) -> None:
        stores, retriever, scorer = self._build(5)
        query = _service_query(
            resource_order=("tm.primary", "tm.secondary"),
            limit=5,
        )
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=cast(Any, scorer),
        )
        report = service.query(
            (
                _service_handle(
                    stores[0],
                    resource_id="tm.primary",
                    order=0,
                ),
                _service_handle(
                    stores[1],
                    resource_id="tm.secondary",
                    order=1,
                ),
            ),
            query,
        )
        self.assertEqual(
            [_result_identity(result) for result in report.results],
            [
                ("tm.primary", 100, "EXACT", 1.0),
                ("tm.secondary", 200, "EXACT", 1.0),
                ("tm.secondary", 190, "CONTEXT", 1.0),
                ("tm.primary", 90, "CONTEXT", 1.0),
                ("tm.secondary", 601, "FUZZY", 0.95),
            ],
        )
        self.assertEqual(
            [
                (metadata.resource_id, metadata.returned_count)
                for metadata in report.resource_metadata
            ],
            [("tm.primary", 2), ("tm.secondary", 3)],
        )
        self.assertEqual(
            sum(
                metadata.returned_count
                for metadata in report.resource_metadata
            ),
            len(report.results),
        )


class TMRetrievalServiceLeaseTests(unittest.TestCase):
    def test_exactly_one_lease_per_resource_and_no_public_or_write_ports(
        self,
    ) -> None:
        limit = 10
        primary = _ServiceStore(resource_id="tm.primary")
        primary_view = _ServiceQueryView(
            primary,
            health=_service_health(fuzzy_available=True),
            exact_records=(_record(100),),
            batch_records=(
                _record(501, source_raw="Open the door quickly."),
            ),
        )
        primary._view = primary_view
        secondary = _ServiceStore(resource_id="tm.secondary")
        secondary_view = _ServiceQueryView(
            secondary,
            health=_service_health(fuzzy_available=True),
            exact_records=(_record(200),),
            batch_records=(
                _record(601, source_raw="Open the gate widely."),
            ),
        )
        secondary._view = secondary_view
        skipped = _ServiceStore(resource_id="tm.skipped", view=None)
        retriever = _ServiceRetriever(
            report_by_resource={
                "tm.primary": _candidate_report(
                    (501,),
                    resource_id="tm.primary",
                    result_limit=limit,
                ),
                "tm.secondary": _candidate_report(
                    (601,),
                    resource_id="tm.secondary",
                    result_limit=limit,
                ),
            }
        )
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=cast(
                Any,
                _FixedEvidenceScorer(
                    {
                        "Open the door quickly.": _evidence(0.9),
                        "Open the gate widely.": _evidence(0.95),
                    }
                ),
            ),
        )
        report = service.query(
            (
                _service_handle(
                    primary,
                    resource_id="tm.primary",
                    order=0,
                ),
                _service_handle(
                    secondary,
                    resource_id="tm.secondary",
                    order=1,
                ),
                _service_handle(
                    skipped,
                    resource_id="tm.skipped",
                    order=2,
                    lookup=False,
                ),
            ),
            _service_query(
                resource_order=("tm.primary", "tm.secondary", "tm.skipped"),
                limit=limit,
            ),
        )
        for store in (primary, secondary):
            self.assertEqual(store.lease_entries, 1)
            self.assertEqual(store.lease_exits, 1)
            self.assertEqual(store.public_exact_calls, 0)
            self.assertEqual(store.public_records_calls, 0)
            self.assertEqual(store.public_health_calls, 0)
            self.assertEqual(store.append_calls, 0)
            self.assertEqual(store.export_calls, 0)
        self.assertEqual(skipped.lease_entries, 0)
        self.assertEqual(
            retriever.view_arguments,
            [primary.issued_views[0], secondary.issued_views[0]],
        )
        self.assertEqual(retriever.result_limits, [limit, limit])
        self.assertEqual(len(set(retriever.folded_queries)), 1)
        self.assertEqual(
            retriever.folded_queries[0],
            fold_text_v1(QUERY_SOURCE).folded_text,
        )
        self.assertEqual(report.resource_failures, ())
        self.assertEqual(
            [metadata.resource_id for metadata in report.resource_metadata],
            ["tm.primary", "tm.secondary"],
        )
        self.assertEqual(
            {result.resource_id for result in report.results},
            {"tm.primary", "tm.secondary"},
        )

    def test_inactive_and_update_only_handles_are_silently_skipped(self) -> None:
        inactive = _ServiceStore(resource_id="tm.inactive", view=None)
        update_only = _ServiceStore(resource_id="tm.update_only", view=None)
        active = _ServiceStore(resource_id="tm.active")
        active_view = _ServiceQueryView(
            active,
            health=_service_health(),
            exact_records=(_record(300),),
        )
        active._view = active_view
        service = TMRetrievalService(
            retriever=cast(Any, _ServiceRetriever()),
            scorer=cast(Any, SimilarityScorerV1()),
        )
        report = service.query(
            (
                _service_handle(
                    inactive,
                    resource_id="tm.inactive",
                    order=0,
                    active=False,
                    lookup=True,
                ),
                _service_handle(
                    update_only,
                    resource_id="tm.update_only",
                    order=1,
                    active=True,
                    lookup=False,
                    update=True,
                ),
                _service_handle(
                    active,
                    resource_id="tm.active",
                    order=2,
                    active=True,
                    lookup=True,
                    update=False,
                ),
            ),
            _service_query(
                resource_order=(
                    "tm.inactive",
                    "tm.update_only",
                    "tm.active",
                )
            ),
        )
        self.assertEqual(inactive.lease_entries, 0)
        self.assertEqual(update_only.lease_entries, 0)
        self.assertEqual(active.lease_entries, 1)
        self.assertEqual(report.resource_failures, ())
        self.assertEqual(
            [metadata.resource_id for metadata in report.resource_metadata],
            ["tm.active"],
        )
        self.assertEqual(
            [_result_identity(result) for result in report.results],
            [("tm.active", 300, "EXACT", 1.0)],
        )


class TMRetrievalServicePartialFailureTests(unittest.TestCase):
    def _failing_store(
        self,
        resource_id: str,
        *,
        exact_record_id: int,
        batch_record_id: int,
        batch_source: str,
        fail_health: Exception | None = None,
        fail_exact: Exception | None = None,
        fail_records: Exception | None = None,
        lease_error: Exception | None = None,
        unhealthy: bool = False,
        exact_unavailable: bool = False,
        fuzzy_available: bool = True,
    ) -> _ServiceStore:
        store = _ServiceStore(
            resource_id=resource_id,
            lease_error=lease_error,
        )
        exact_available = not unhealthy and not exact_unavailable
        health = _service_health(
            healthy=not unhealthy,
            exact_available=exact_available,
            fuzzy_available=fuzzy_available and exact_available,
        )
        store._view = _ServiceQueryView(
            store,
            health=health,
            exact_records=(_record(exact_record_id),),
            batch_records=(
                _record(batch_record_id, source_raw=batch_source),
            ),
            fail_health=fail_health,
            fail_exact=fail_exact,
            fail_records=fail_records,
        )
        return store

    def test_partial_failures_isolate_resources_with_stable_codes(self) -> None:
        limit = 10
        lease_failed = self._failing_store(
            "tm.lease",
            exact_record_id=101,
            batch_record_id=901,
            batch_source="Open the gate widely.",
            lease_error=SQLiteStoreLifecycleError(
                "STORE.GENERATION_CHANGED",
                resource_id="tm.lease",
                generation=0,
                retryable=True,
            ),
        )
        health_failed = self._failing_store(
            "tm.health",
            exact_record_id=102,
            batch_record_id=902,
            batch_source="Open the gate widely.",
            fail_health=SQLiteStoreLifecycleError(
                "STORE.QUERY_VIEW_EXPIRED",
                resource_id="tm.health",
                generation=0,
                retryable=False,
            ),
        )
        exact_failed = self._failing_store(
            "tm.exact",
            exact_record_id=103,
            batch_record_id=903,
            batch_source="Open the gate widely.",
            fail_exact=ValueError("boom exact"),
        )
        unhealthy = self._failing_store(
            "tm.unhealthy",
            exact_record_id=104,
            batch_record_id=904,
            batch_source="Open the gate widely.",
            unhealthy=True,
        )
        exact_unavailable = self._failing_store(
            "tm.exact_unavailable",
            exact_record_id=105,
            batch_record_id=905,
            batch_source="Open the gate widely.",
            exact_unavailable=True,
        )
        recall_failed = self._failing_store(
            "tm.recall",
            exact_record_id=106,
            batch_record_id=906,
            batch_source="Open the gate widely.",
        )
        records_failed = self._failing_store(
            "tm.records",
            exact_record_id=107,
            batch_record_id=907,
            batch_source="Open the gate widely.",
            fail_records=SQLiteStoreSchemaError(
                "STORE.CANDIDATE_EVIDENCE_INVALID"
            ),
        )
        healthy = self._failing_store(
            "tm.healthy",
            exact_record_id=108,
            batch_record_id=908,
            batch_source="Open the door quickly.",
        )
        score_failed = self._failing_store(
            "tm.score",
            exact_record_id=109,
            batch_record_id=909,
            batch_source="Open the gate widely.",
        )
        stores = (
            lease_failed,
            health_failed,
            exact_failed,
            unhealthy,
            exact_unavailable,
            recall_failed,
            records_failed,
            healthy,
            score_failed,
        )
        retriever = _ServiceRetriever(
            report_by_resource={
                "tm.recall": _candidate_report(
                    (906,),
                    resource_id="tm.recall",
                    result_limit=limit,
                ),
                "tm.records": _candidate_report(
                    (907,),
                    resource_id="tm.records",
                    result_limit=limit,
                ),
                "tm.healthy": _candidate_report(
                    (908,),
                    resource_id="tm.healthy",
                    result_limit=limit,
                ),
                "tm.score": _candidate_report(
                    (909,),
                    resource_id="tm.score",
                    result_limit=limit,
                ),
            },
            error_by_resource={
                "tm.recall": RuntimeError("boom recall"),
            },
        )
        scorer = _RaisingScorer(
            fail_candidate="Open the gate widely.",
            evidence=_evidence(0.9),
        )
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=cast(Any, scorer),
        )
        report = service.query(
            tuple(
                _service_handle(store, resource_id=store.resource_id, order=index)
                for index, store in enumerate(stores)
            ),
            _service_query(
                resource_order=tuple(
                    store.resource_id for store in stores
                ),
                limit=limit,
            ),
        )
        self.assertEqual(
            [
                (
                    failure.resource_id,
                    failure.stage,
                    failure.error_code,
                    failure.retryable,
                )
                for failure in report.resource_failures
            ],
            [
                ("tm.lease", "LEASE", "STORE.GENERATION_CHANGED", True),
                ("tm.health", "HEALTH", "STORE.QUERY_VIEW_EXPIRED", False),
                ("tm.exact", "EXACT", "RETRIEVAL.QUERY_FAILED", False),
                ("tm.unhealthy", "HEALTH", "RETRIEVAL.STORE_UNHEALTHY", False),
                (
                    "tm.exact_unavailable",
                    "HEALTH",
                    "RETRIEVAL.EXACT_UNAVAILABLE",
                    False,
                ),
                ("tm.recall", "RECALL", "RETRIEVAL.QUERY_FAILED", False),
                (
                    "tm.records",
                    "RECORDS",
                    "STORE.CANDIDATE_EVIDENCE_INVALID",
                    False,
                ),
                ("tm.score", "SCORE", "RETRIEVAL.QUERY_FAILED", False),
            ],
        )
        for failure in report.resource_failures:
            self.assertNotIn("boom", failure.safe_summary)
        self.assertEqual(
            [metadata.resource_id for metadata in report.resource_metadata],
            ["tm.healthy"],
        )
        self.assertEqual(
            {result.resource_id for result in report.results},
            {"tm.healthy"},
        )
        for store in stores:
            self.assertEqual(store.lease_entries, 1)
        self.assertEqual(lease_failed.lease_exits, 0)
        for store in stores[1:]:
            self.assertEqual(store.lease_exits, 1)
        self.assertEqual(
            retriever.resource_ids,
            ["tm.recall", "tm.records", "tm.healthy", "tm.score"],
        )


class TMRetrievalServicePermutationTests(unittest.TestCase):
    def test_resource_tuple_permutation_does_not_change_the_report(self) -> None:
        def build(
        ) -> tuple[tuple[_ServiceStore, ...], TMQuery]:
            stores = []
            for resource_id, record_id in (
                ("tm.a", 100),
                ("tm.b", 200),
                ("tm.c", 300),
            ):
                store = _ServiceStore(resource_id=resource_id)
                store._view = _ServiceQueryView(
                    store,
                    health=_service_health(),
                    exact_records=(_record(record_id),),
                )
                stores.append(store)
            query = _service_query(
                resource_order=("tm.a", "tm.b", "tm.c")
            )
            return tuple(stores), query

        stores_a, query = build()
        service = TMRetrievalService(
            retriever=cast(Any, _ServiceRetriever()),
            scorer=cast(Any, SimilarityScorerV1()),
        )
        first = service.query(
            (
                _service_handle(
                    stores_a[0],
                    resource_id="tm.a",
                    order=0,
                ),
                _service_handle(
                    stores_a[1],
                    resource_id="tm.b",
                    order=1,
                ),
                _service_handle(
                    stores_a[2],
                    resource_id="tm.c",
                    order=2,
                ),
            ),
            query,
        )
        stores_b, _query = build()
        second = service.query(
            (
                _service_handle(
                    stores_b[2],
                    resource_id="tm.c",
                    order=2,
                ),
                _service_handle(
                    stores_b[0],
                    resource_id="tm.a",
                    order=0,
                ),
                _service_handle(
                    stores_b[1],
                    resource_id="tm.b",
                    order=1,
                ),
            ),
            query,
        )
        self.assertEqual(second, first)
        self.assertEqual(
            [_result_identity(result) for result in second.results],
            [
                ("tm.a", 100, "EXACT", 1.0),
                ("tm.b", 200, "EXACT", 1.0),
                ("tm.c", 300, "EXACT", 1.0),
            ],
        )
        for store in stores_a + stores_b:
            self.assertEqual(store.lease_entries, 1)


class TMRetrievalServiceAvailabilityTests(unittest.TestCase):
    def test_context_gate_closed_is_distinct_from_available_zero_hits(self) -> None:
        closed = _ServiceStore(resource_id="tm.closed")
        closed_view = _ServiceQueryView(
            closed,
            health=_service_health(context_available=False),
            exact_records=(
                _record(100, speaker_raw="speaker"),
                _record(90, target_raw="variant", speaker_raw="speaker"),
            ),
        )
        closed._view = closed_view
        open_zero = _ServiceStore(resource_id="tm.open_zero")
        open_zero_view = _ServiceQueryView(
            open_zero,
            health=_service_health(context_available=True),
            exact_records=(_record(200, speaker_raw="speaker"),),
        )
        open_zero._view = open_zero_view
        service = TMRetrievalService(
            retriever=cast(Any, _ServiceRetriever()),
            scorer=cast(Any, SimilarityScorerV1()),
        )
        report = service.query(
            (
                _service_handle(
                    closed,
                    resource_id="tm.closed",
                    order=0,
                ),
                _service_handle(
                    open_zero,
                    resource_id="tm.open_zero",
                    order=1,
                ),
            ),
            _service_query(resource_order=("tm.closed", "tm.open_zero")),
        )
        self.assertEqual(
            [_result_identity(result) for result in report.results],
            [
                ("tm.closed", 100, "EXACT", 1.0),
                ("tm.open_zero", 200, "EXACT", 1.0),
            ],
        )
        closed_metadata = report.resource_metadata[0]
        open_metadata = report.resource_metadata[1]
        self.assertFalse(closed_metadata.context_available)
        self.assertEqual(
            closed_metadata.context_unavailable_code,
            "STORE.CONTEXT_GATE_CLOSED",
        )
        self.assertEqual(closed_metadata.returned_count, 1)
        self.assertTrue(open_metadata.context_available)
        self.assertIsNone(open_metadata.context_unavailable_code)
        self.assertEqual(open_metadata.returned_count, 1)

    def test_fuzzy_gate_closed_is_distinct_from_available_zero_hits(self) -> None:
        limit = 10
        closed = _ServiceStore(resource_id="tm.closed")
        closed._view = _ServiceQueryView(
            closed,
            health=_service_health(fuzzy_available=False),
            exact_records=(_record(100),),
        )
        open_zero = _ServiceStore(resource_id="tm.open_zero")
        open_zero._view = _ServiceQueryView(
            open_zero,
            health=_service_health(fuzzy_available=True),
            exact_records=(_record(200),),
        )
        retriever = _ServiceRetriever(
            report_by_resource={
                "tm.open_zero": _candidate_report(
                    (),
                    resource_id="tm.open_zero",
                    result_limit=limit,
                    fuzzy_available=True,
                ),
            }
        )
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=cast(Any, _FixedEvidenceScorer({})),
        )
        report = service.query(
            (
                _service_handle(
                    closed,
                    resource_id="tm.closed",
                    order=0,
                ),
                _service_handle(
                    open_zero,
                    resource_id="tm.open_zero",
                    order=1,
                ),
            ),
            _service_query(
                resource_order=("tm.closed", "tm.open_zero"),
                limit=limit,
            ),
        )
        closed_metadata = report.resource_metadata[0]
        open_metadata = report.resource_metadata[1]
        self.assertFalse(closed_metadata.recall.fuzzy_available)
        self.assertEqual(
            closed_metadata.recall.fuzzy_unavailable_code,
            "STORE.FUZZY_GATE_CLOSED",
        )
        self.assertEqual(closed_metadata.scored_count, 0)
        self.assertTrue(open_metadata.recall.fuzzy_available)
        self.assertIsNone(open_metadata.recall.fuzzy_unavailable_code)
        self.assertTrue(open_metadata.recall.stages)
        self.assertEqual(open_metadata.scored_count, 0)
        self.assertEqual(retriever.resource_ids, ["tm.open_zero"])
        self.assertEqual(
            [result.match_type for result in report.results],
            [TMMatchType.EXACT, TMMatchType.EXACT],
        )


class TMRetrievalServiceCountTests(unittest.TestCase):
    def _build(
        self,
        limit: int,
    ) -> tuple[_ServiceStore, _ServiceRetriever]:
        store = _ServiceStore(resource_id="tm.primary")
        store._view = _ServiceQueryView(
            store,
            health=_service_health(fuzzy_available=True),
            exact_records=(_record(100),),
            batch_records=(
                _record(701, source_raw="Open the door quickly."),
                _record(702, source_raw="Open the door loudly."),
                _record(703, source_raw="Open the door neatly."),
            ),
        )
        retriever = _ServiceRetriever(
            _candidate_report(
                (701, 702, 703),
                resource_id="tm.primary",
                result_limit=limit,
            )
        )
        return store, retriever

    def test_scored_count_and_returned_count_balance(self) -> None:
        limited_store, limited_retriever = self._build(1)
        service = TMRetrievalService(
            retriever=cast(Any, limited_retriever),
            scorer=cast(
                Any,
                _FixedEvidenceScorer(
                    {
                        "Open the door quickly.": _evidence(0.95),
                        "Open the door loudly.": _evidence(0.6),
                        "Open the door neatly.": _evidence(0.8),
                    }
                ),
            ),
        )
        limited = service.query(
            (
                _service_handle(
                    limited_store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            _service_query(
                resource_order=("tm.primary",),
                limit=1,
                minimum_similarity=0.7,
            ),
        )
        self.assertEqual(
            [_result_identity(result) for result in limited.results],
            [("tm.primary", 100, "EXACT", 1.0)],
        )
        metadata = limited.resource_metadata[0]
        self.assertEqual(metadata.scored_count, 3)
        self.assertEqual(metadata.returned_count, 1)
        self.assertEqual(
            sum(
                entry.returned_count
                for entry in limited.resource_metadata
            ),
            len(limited.results),
        )
        self.assertEqual(
            metadata.returned_count,
            sum(
                result.resource_id == metadata.resource_id
                for result in limited.results
            ),
        )
        self.assertLessEqual(
            sum(
                entry.returned_count
                for entry in limited.resource_metadata
            ),
            1,
        )

        full_store, full_retriever = self._build(3)
        full_service = TMRetrievalService(
            retriever=cast(Any, full_retriever),
            scorer=cast(
                Any,
                _FixedEvidenceScorer(
                    {
                        "Open the door quickly.": _evidence(0.95),
                        "Open the door loudly.": _evidence(0.6),
                        "Open the door neatly.": _evidence(0.8),
                    }
                ),
            ),
        )
        full = full_service.query(
            (
                _service_handle(
                    full_store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            _service_query(
                resource_order=("tm.primary",),
                limit=3,
                minimum_similarity=0.7,
            ),
        )
        self.assertEqual(
            [_result_identity(result) for result in full.results],
            [
                ("tm.primary", 100, "EXACT", 1.0),
                ("tm.primary", 701, "FUZZY", 0.95),
                ("tm.primary", 703, "FUZZY", 0.8),
            ],
        )
        self.assertEqual(full.resource_metadata[0].scored_count, 3)
        self.assertEqual(full.resource_metadata[0].returned_count, 3)


class TMRetrievalServiceTOCTOUTests(unittest.TestCase):
    def test_mutating_query_and_handle_during_callbacks_cannot_rebind(
        self,
    ) -> None:
        rogue = _ServiceStore(resource_id="tm.rogue", view=None)
        store = _ServiceStore(resource_id="tm.primary")
        handle = _service_handle(
            store,
            resource_id="tm.primary",
            order=0,
        )
        query = _service_query(resource_order=("tm.primary",))

        def on_health() -> None:
            object.__setattr__(handle, "store", rogue)

        def on_exact() -> None:
            object.__setattr__(query, "query_source", "MUTATED QUERY")
            object.__setattr__(query, "limit", 1)
            object.__setattr__(
                query,
                "resource_order",
                ("tm.rogue",),
            )

        store._view = _ServiceQueryView(
            store,
            health=_service_health(context_available=True),
            exact_records=(
                _record(100, speaker_raw="speaker"),
                _record(90, target_raw="variant", speaker_raw="speaker"),
            ),
            on_health=on_health,
            on_exact=on_exact,
        )
        service = TMRetrievalService(
            retriever=cast(Any, _ServiceRetriever()),
            scorer=cast(Any, SimilarityScorerV1()),
        )
        report = service.query((handle,), query)
        self.assertEqual(store.lease_entries, 1)
        self.assertEqual(rogue.lease_entries, 0)
        self.assertEqual(store._view.exact_sources, [QUERY_SOURCE])
        self.assertEqual(
            [_result_identity(result) for result in report.results],
            [
                ("tm.primary", 100, "EXACT", 1.0),
                ("tm.primary", 90, "CONTEXT", 1.0),
            ],
        )
        for result in report.results:
            self.assertEqual(result.query_source, QUERY_SOURCE)

    def test_rotating_retriever_port_is_captured_once(self) -> None:
        store = _ServiceStore(resource_id="tm.primary")
        store._view = _ServiceQueryView(
            store,
            health=_service_health(fuzzy_available=True),
            exact_records=(_record(100),),
            batch_records=(
                _record(501, source_raw="Open the door quickly."),
            ),
        )
        retriever = _RotatingPortRetriever(
            _candidate_report(
                (501,),
                resource_id="tm.primary",
                result_limit=10,
            )
        )
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=cast(
                Any,
                _FixedEvidenceScorer(
                    {"Open the door quickly.": _evidence(0.9)}
                ),
            ),
        )
        report = service.query(
            (
                _service_handle(
                    store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            _service_query(resource_order=("tm.primary",)),
        )
        self.assertEqual(retriever.accesses, 1)
        self.assertEqual(retriever.calls, 1)
        self.assertEqual(
            [_result_identity(result) for result in report.results],
            [
                ("tm.primary", 100, "EXACT", 1.0),
                ("tm.primary", 501, "FUZZY", 0.9),
            ],
        )

    def test_retriever_mutating_returned_records_cannot_corrupt_results(
        self,
    ) -> None:
        store = _ServiceStore(resource_id="tm.primary")
        records = (
            _record(100, speaker_raw="speaker"),
            _record(90, target_raw="variant", speaker_raw="speaker"),
        )

        def on_call() -> None:
            for record in records:
                object.__setattr__(record, "source_raw", "MUTATED SOURCE")
                object.__setattr__(record, "target_raw", "MUTATED TARGET")
                object.__setattr__(
                    record,
                    "provenance",
                    (("importer", "mutated"),),
                )

        store._view = _ServiceQueryView(
            store,
            health=_service_health(
                context_available=True,
                fuzzy_available=True,
            ),
            exact_records=records,
        )
        retriever = _ServiceRetriever(
            _candidate_report(
                (),
                resource_id="tm.primary",
                result_limit=10,
                fuzzy_available=True,
            ),
            on_call=on_call,
        )
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=cast(Any, SimilarityScorerV1()),
        )
        report = service.query(
            (
                _service_handle(
                    store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            _service_query(resource_order=("tm.primary",)),
        )
        self.assertEqual(
            [
                (
                    result.record_id,
                    result.match_type.value,
                    result.target,
                    result.provenance,
                )
                for result in report.results
            ],
            [
                (100, "EXACT", "target", (("importer", "legacy-100"),)),
                (90, "CONTEXT", "variant", (("importer", "legacy-90"),)),
            ],
        )

    def test_scorer_mutating_inputs_and_previous_evidence_cannot_corrupt(
        self,
    ) -> None:
        service_batch = (
            _record(501, source_raw="Open the door quickly."),
            _record(502, source_raw="Open the door slowly."),
        )
        reference_batch = (
            _record(501, source_raw="Open the door quickly."),
            _record(502, source_raw="Open the door slowly."),
        )
        store = _ServiceStore(resource_id="tm.primary")
        store._view = _ServiceQueryView(
            store,
            health=_service_health(fuzzy_available=True),
            exact_records=(_record(100),),
            batch_records=service_batch,
        )
        candidate_report = _candidate_report(
            (501, 502),
            resource_id="tm.primary",
            result_limit=10,
        )
        reference_report = _candidate_report(
            (501, 502),
            resource_id="tm.primary",
            result_limit=10,
        )
        query = _service_query(resource_order=("tm.primary",))
        mutating_scorer = _MutatingAliasScorer(
            query=query,
            records=service_batch,
            report=candidate_report,
        )
        service = TMRetrievalService(
            retriever=cast(Any, _ServiceRetriever(candidate_report)),
            scorer=cast(Any, mutating_scorer),
        )
        service_result = service.query(
            (
                _service_handle(
                    store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            query,
        )
        reference = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_service_query(resource_order=("tm.primary",)),
            report=reference_report,
            records=reference_batch,
            scorer=SimilarityScorerV1(),
        )
        self.assertEqual(
            [_result_identity(result) for result in service_result.results],
            [
                ("tm.primary", 100, "EXACT", 1.0),
                *[
                    _result_identity(result)
                    for result in reference.accepted
                ],
            ],
        )
        self.assertTrue(mutating_scorer.mutated)

        aliasing_store = _ServiceStore(resource_id="tm.primary")
        aliasing_store._view = _ServiceQueryView(
            aliasing_store,
            health=_service_health(fuzzy_available=True),
            exact_records=(_record(100),),
            batch_records=reference_batch,
        )
        aliasing_service = TMRetrievalService(
            retriever=cast(Any, _ServiceRetriever(reference_report)),
            scorer=cast(Any, _EvidenceAliasingScorer()),
        )
        aliasing_report = aliasing_service.query(
            (
                _service_handle(
                    aliasing_store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            _service_query(resource_order=("tm.primary",)),
        )
        self.assertEqual(
            [_result_identity(result) for result in aliasing_report.results],
            [
                ("tm.primary", 100, "EXACT", 1.0),
                *[
                    _result_identity(result)
                    for result in reference.accepted
                ],
            ],
        )

    def test_score_property_mutation_cannot_rebind_scorer_port(self) -> None:
        store = _ServiceStore(resource_id="tm.primary")
        store._view = _ServiceQueryView(
            store,
            health=_service_health(fuzzy_available=True),
            exact_records=(_record(100),),
            batch_records=(
                _record(501, source_raw="Open the door quickly."),
            ),
        )
        report = _candidate_report(
            (501,),
            resource_id="tm.primary",
            result_limit=10,
        )
        query = _service_query(resource_order=("tm.primary",))
        batch_records = (
            _record(501, source_raw="Open the door quickly."),
        )
        scorer = _ScorePropertyMutatingScorer(
            query=query,
            records=batch_records,
            report=report,
        )
        reference_report = _candidate_report(
            (501,),
            resource_id="tm.primary",
            result_limit=10,
        )
        service = TMRetrievalService(
            retriever=cast(Any, _ServiceRetriever(report)),
            scorer=cast(Any, scorer),
        )
        service_report = service.query(
            (
                _service_handle(
                    store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            query,
        )
        self.assertEqual(scorer.score_accesses, 1)
        reference = score_fuzzy_candidates(
            resource_id="tm.primary",
            resource_order=0,
            query=_service_query(resource_order=("tm.primary",)),
            report=reference_report,
            records=batch_records,
            scorer=SimilarityScorerV1(),
        )
        self.assertEqual(
            [_result_identity(result) for result in service_report.results],
            [
                ("tm.primary", 100, "EXACT", 1.0),
                *[
                    _result_identity(result)
                    for result in reference.accepted
                ],
            ],
        )


class TMRetrievalServiceDynamicPortTests(unittest.TestCase):
    """Lazy query-scoped capture of rotating retriever/scorer/records ports."""

    def _fuzzy_store(
        self,
        resource_id: str,
        *,
        exact_record_id: int,
        batch_record_id: int,
        batch_source: str,
        on_exact: Callable[[], None] | None = None,
    ) -> _ServiceStore:
        store = _ServiceStore(resource_id=resource_id)
        store._view = _ServiceQueryView(
            store,
            health=_service_health(fuzzy_available=True),
            exact_records=(_record(exact_record_id),),
            batch_records=(
                _record(batch_record_id, source_raw=batch_source),
            ),
            on_exact=on_exact,
        )
        return store

    def test_two_fuzzy_resources_share_one_captured_scorer_port(self) -> None:
        limit = 10
        first = self._fuzzy_store(
            "tm.first",
            exact_record_id=100,
            batch_record_id=501,
            batch_source="Open the door quickly.",
        )
        second = self._fuzzy_store(
            "tm.second",
            exact_record_id=200,
            batch_record_id=601,
            batch_source="Open the gate widely.",
        )
        retriever = _ServiceRetriever(
            report_by_resource={
                "tm.first": _candidate_report(
                    (501,),
                    resource_id="tm.first",
                    result_limit=limit,
                ),
                "tm.second": _candidate_report(
                    (601,),
                    resource_id="tm.second",
                    result_limit=limit,
                ),
            }
        )
        scorer = cast(Any, _ScoreAttributeRotatingScorer())
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=scorer,
        )
        report = service.query(
            (
                _service_handle(first, resource_id="tm.first", order=0),
                _service_handle(second, resource_id="tm.second", order=1),
            ),
            _service_query(
                resource_order=("tm.first", "tm.second"),
                limit=limit,
            ),
        )
        self.assertEqual(scorer.score_accesses, 1)
        self.assertEqual(scorer.calls, 2)
        self.assertEqual(
            [metadata.scored_count for metadata in report.resource_metadata],
            [1, 1],
        )

    def test_two_fuzzy_resources_share_one_captured_retriever_port(
        self,
    ) -> None:
        limit = 10
        events: list[str] = []
        first = self._fuzzy_store(
            "tm.first",
            exact_record_id=100,
            batch_record_id=501,
            batch_source="Open the door quickly.",
            on_exact=lambda: events.append("exact.first"),
        )
        second = self._fuzzy_store(
            "tm.second",
            exact_record_id=200,
            batch_record_id=601,
            batch_source="Open the gate widely.",
        )
        retriever = _RotatingPortRetriever(
            _candidate_report(
                (501,),
                resource_id="tm.first",
                result_limit=limit,
            ),
            report_by_resource={
                "tm.second": _candidate_report(
                    (601,),
                    resource_id="tm.second",
                    result_limit=limit,
                ),
            },
            on_access=lambda: events.append("retriever.access"),
        )
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=cast(Any, SimilarityScorerV1()),
        )
        report = service.query(
            (
                _service_handle(first, resource_id="tm.first", order=0),
                _service_handle(second, resource_id="tm.second", order=1),
            ),
            _service_query(
                resource_order=("tm.first", "tm.second"),
                limit=limit,
            ),
        )
        self.assertEqual(retriever.accesses, 1)
        self.assertEqual(retriever.calls, 2)
        self.assertGreater(
            events.index("retriever.access"),
            events.index("exact.first"),
        )
        self.assertEqual(report.resource_failures, ())

    def test_fuzzy_unavailable_resources_never_touch_dynamic_ports(
        self,
    ) -> None:
        first = _ServiceStore(resource_id="tm.first")
        first._view = _ServiceQueryView(
            first,
            health=_service_health(fuzzy_available=False),
            exact_records=(_record(100),),
        )
        second = _ServiceStore(resource_id="tm.second")
        second._view = _ServiceQueryView(
            second,
            health=_service_health(fuzzy_available=False),
            exact_records=(_record(200),),
        )
        retriever = _RotatingPortRetriever(
            _candidate_report((), fuzzy_available=False)
        )
        scorer = cast(Any, _ScoreAttributeRotatingScorer())
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=scorer,
        )
        report = service.query(
            (
                _service_handle(first, resource_id="tm.first", order=0),
                _service_handle(second, resource_id="tm.second", order=1),
            ),
            _service_query(resource_order=("tm.first", "tm.second")),
        )
        self.assertEqual(retriever.accesses, 0)
        self.assertEqual(retriever.calls, 0)
        self.assertEqual(scorer.score_accesses, 0)
        self.assertEqual(scorer.calls, 0)
        self.assertEqual(report.resource_failures, ())
        self.assertEqual(
            [_result_identity(result) for result in report.results],
            [
                ("tm.first", 100, "EXACT", 1.0),
                ("tm.second", 200, "EXACT", 1.0),
            ],
        )

    def test_all_handles_skipped_never_touch_dynamic_ports(self) -> None:
        first = _ServiceStore(resource_id="tm.first", view=None)
        second = _ServiceStore(resource_id="tm.second", view=None)
        retriever = _RotatingPortRetriever(_candidate_report((),))
        scorer = cast(Any, _ScoreAttributeRotatingScorer())
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=scorer,
        )
        report = service.query(
            (
                _service_handle(
                    first,
                    resource_id="tm.first",
                    order=0,
                    active=False,
                ),
                _service_handle(
                    second,
                    resource_id="tm.second",
                    order=1,
                    lookup=False,
                ),
            ),
            _service_query(resource_order=("tm.first", "tm.second")),
        )
        self.assertEqual(retriever.accesses, 0)
        self.assertEqual(retriever.calls, 0)
        self.assertEqual(scorer.score_accesses, 0)
        self.assertEqual(scorer.calls, 0)
        self.assertEqual(report.resource_failures, ())
        self.assertEqual(report.resource_metadata, ())
        self.assertEqual(report.results, ())

    def test_fuzzy_unavailable_view_never_touches_records_by_id_port(
        self,
    ) -> None:
        for raise_records_port in (True, False):
            store = _ServiceStore(resource_id="tm.primary")
            view = _RotatingPortsQueryView(
                store,
                health=_service_health(fuzzy_available=False),
                exact_records=(_record(100),),
                raise_records_port=raise_records_port,
            )
            store._view = cast(Any, view)
            service = TMRetrievalService(
                retriever=cast(
                    Any,
                    _RotatingPortRetriever(
                        _candidate_report((), fuzzy_available=False)
                    ),
                ),
                scorer=cast(Any, SimilarityScorerV1()),
            )
            report = service.query(
                (
                    _service_handle(
                        store,
                        resource_id="tm.primary",
                        order=0,
                    ),
                ),
                _service_query(resource_order=("tm.primary",)),
            )
            self.assertEqual(report.resource_failures, ())
            self.assertEqual(
                [_result_identity(result) for result in report.results],
                [("tm.primary", 100, "EXACT", 1.0)],
            )
            self.assertEqual(view.records_port_accesses, 0)
            self.assertEqual(view.records_calls, 0)
            self.assertEqual(view.exact_port_accesses, 1)
            self.assertEqual(store.lease_entries, 1)

    def test_view_generation_mismatch_fails_health_without_later_ports(
        self,
    ) -> None:
        store = _ServiceStore(resource_id="tm.primary")
        view = _RotatingPortsQueryView(
            store,
            health=_service_health(fuzzy_available=True),
            generation=7,
            exact_records=(_record(100),),
            batch_records=(
                _record(501, source_raw="Open the door quickly."),
            ),
        )
        store._view = cast(Any, view)
        retriever = _RotatingPortRetriever(
            _candidate_report(
                (501,),
                resource_id="tm.primary",
                result_limit=10,
            )
        )
        scorer = cast(Any, _ScoreAttributeRotatingScorer())
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=scorer,
        )
        report = service.query(
            (
                _service_handle(
                    store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            _service_query(resource_order=("tm.primary",)),
        )
        self.assertEqual(
            [
                (
                    failure.resource_id,
                    failure.stage,
                    failure.error_code,
                    failure.retryable,
                )
                for failure in report.resource_failures
            ],
            [
                (
                    "tm.primary",
                    "HEALTH",
                    "STORE.GENERATION_MISMATCH",
                    False,
                )
            ],
        )
        self.assertEqual(view.exact_port_accesses, 0)
        self.assertEqual(view.records_port_accesses, 0)
        self.assertEqual(retriever.accesses, 0)
        self.assertEqual(retriever.calls, 0)
        self.assertEqual(scorer.score_accesses, 0)
        self.assertEqual(store.lease_entries, 1)
        self.assertEqual(store.lease_exits, 1)
        self.assertEqual(report.resource_metadata, ())
        self.assertEqual(report.results, ())

    def test_unhealthy_health_never_touches_later_ports(self) -> None:
        store = _ServiceStore(resource_id="tm.primary")
        view = _RotatingPortsQueryView(
            store,
            health=_service_health(healthy=False, exact_available=False),
            exact_records=(_record(100),),
            batch_records=(
                _record(501, source_raw="Open the door quickly."),
            ),
        )
        store._view = cast(Any, view)
        retriever = _RotatingPortRetriever(
            _candidate_report(
                (501,),
                resource_id="tm.primary",
                result_limit=10,
            )
        )
        scorer = cast(Any, _ScoreAttributeRotatingScorer())
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=scorer,
        )
        report = service.query(
            (
                _service_handle(
                    store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            _service_query(resource_order=("tm.primary",)),
        )
        self.assertEqual(
            [
                (
                    failure.resource_id,
                    failure.stage,
                    failure.error_code,
                )
                for failure in report.resource_failures
            ],
            [
                ("tm.primary", "HEALTH", "RETRIEVAL.STORE_UNHEALTHY"),
            ],
        )
        self.assertEqual(view.exact_port_accesses, 0)
        self.assertEqual(view.records_port_accesses, 0)
        self.assertEqual(retriever.accesses, 0)
        self.assertEqual(retriever.calls, 0)
        self.assertEqual(scorer.score_accesses, 0)
        self.assertEqual(store.lease_entries, 1)


class TMRetrievalServiceInputValidationTests(unittest.TestCase):
    def _stores(self) -> tuple[_ServiceStore, _ServiceStore]:
        first = _ServiceStore(resource_id="tm.a", view=None)
        second = _ServiceStore(resource_id="tm.b", view=None)
        return first, second

    def _assert_no_callbacks(
        self,
        stores: tuple[_ServiceStore, ...],
        retriever: _RotatingPortRetriever,
    ) -> None:
        for store in stores:
            self.assertEqual(store.lease_entries, 0)
            self.assertEqual(store.public_exact_calls, 0)
            self.assertEqual(store.public_records_calls, 0)
            self.assertEqual(store.append_calls, 0)
        self.assertEqual(retriever.accesses, 0)

    def test_invalid_resource_and_order_mappings_are_rejected_before_callbacks(
        self,
    ) -> None:
        first, second = self._stores()
        query = _service_query(
            resource_order=("tm.a", "tm.b"),
        )
        rotating = _RotatingPortRetriever(_candidate_report((),))
        service = TMRetrievalService(
            retriever=cast(Any, rotating),
            scorer=cast(Any, SimilarityScorerV1()),
        )

        duplicate_ids = (
            _service_handle(first, resource_id="tm.a", order=0),
            _service_handle(second, resource_id="tm.a", order=1),
        )
        with self.assertRaises(ValueError):
            service.query(duplicate_ids, query)
        self._assert_no_callbacks((first, second), rotating)

        duplicate_orders = (
            _service_handle(first, resource_id="tm.a", order=0),
            _service_handle(second, resource_id="tm.b", order=0),
        )
        with self.assertRaises(ValueError):
            service.query(duplicate_orders, query)
        self._assert_no_callbacks((first, second), rotating)

        missing_id = (
            _service_handle(first, resource_id="tm.a", order=0),
        )
        with self.assertRaises(ValueError):
            service.query(missing_id, query)
        self._assert_no_callbacks((first, second), rotating)

        extra_id = (
            _service_handle(first, resource_id="tm.a", order=0),
            _service_handle(second, resource_id="tm.b", order=1),
        )
        with self.assertRaises(ValueError):
            service.query(
                extra_id,
                _service_query(
                    resource_order=("tm.a", "tm.b", "tm.c")
                ),
            )
        self._assert_no_callbacks((first, second), rotating)

        order_mismatch = (
            _service_handle(first, resource_id="tm.a", order=1),
            _service_handle(second, resource_id="tm.b", order=0),
        )
        with self.assertRaises(ValueError):
            service.query(order_mismatch, query)
        self._assert_no_callbacks((first, second), rotating)

    def test_non_contract_inputs_are_rejected_before_callbacks(self) -> None:
        first, second = self._stores()
        handles = (
            _service_handle(first, resource_id="tm.a", order=0),
            _service_handle(second, resource_id="tm.b", order=1),
        )
        query = _service_query(resource_order=("tm.a", "tm.b"))
        retriever = _RotatingPortRetriever(_candidate_report((),))
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=cast(Any, SimilarityScorerV1()),
        )
        with self.assertRaises(TypeError):
            service.query(cast(Any, [handles[0], handles[1]]), query)
        with self.assertRaises(TypeError):
            service.query(cast(Any, (object(),)), query)
        with self.assertRaises(TypeError):
            service.query(handles, cast(Any, object()))
        self._assert_no_callbacks((first, second), retriever)


class TMRetrievalServiceLeaseLifecycleTests(unittest.TestCase):
    def test_expired_view_failure_preserves_code_without_a_second_lease(
        self,
    ) -> None:
        store = _ServiceStore(resource_id="tm.primary")
        view = _ServiceQueryView(
            store,
            health=_service_health(fuzzy_available=True),
            exact_records=(_record(100),),
            fail_records=SQLiteStoreLifecycleError(
                "STORE.QUERY_VIEW_EXPIRED",
                resource_id="tm.primary",
                generation=0,
                retryable=False,
            ),
        )
        store._view = view
        retriever = _ServiceRetriever(
            _candidate_report(
                (501,),
                resource_id="tm.primary",
                result_limit=10,
            )
        )
        service = TMRetrievalService(
            retriever=cast(Any, retriever),
            scorer=cast(
                Any,
                _FixedEvidenceScorer(
                    {"Open the door quickly.": _evidence(0.9)}
                ),
            ),
        )
        report = service.query(
            (
                _service_handle(
                    store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            _service_query(resource_order=("tm.primary",)),
        )
        self.assertEqual(
            [
                (
                    failure.resource_id,
                    failure.stage,
                    failure.error_code,
                    failure.retryable,
                )
                for failure in report.resource_failures
            ],
            [("tm.primary", "RECORDS", "STORE.QUERY_VIEW_EXPIRED", False)],
        )
        self.assertEqual(store.lease_entries, 1)
        self.assertEqual(store.lease_exits, 1)
        self.assertEqual(retriever.calls, 1)
        self.assertTrue(view.expired)
        self.assertEqual(report.resource_metadata, ())
        self.assertEqual(report.results, ())

    def test_mid_pipeline_failure_never_reacquires_the_lease(self) -> None:
        store = _ServiceStore(resource_id="tm.primary")
        store._view = _ServiceQueryView(
            store,
            health=_service_health(),
            exact_records=(_record(100),),
            fail_exact=RuntimeError("boom exact"),
        )
        service = TMRetrievalService(
            retriever=cast(Any, _ServiceRetriever()),
            scorer=cast(Any, SimilarityScorerV1()),
        )
        report = service.query(
            (
                _service_handle(
                    store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            _service_query(resource_order=("tm.primary",)),
        )
        self.assertEqual(
            report.resource_failures[0].safe_summary,
            "EXACT:RETRIEVAL.QUERY_FAILED:NOT_RETRYABLE",
        )
        self.assertEqual(store.lease_entries, 1)
        self.assertEqual(store.lease_exits, 1)

    def test_lease_enter_failure_uses_exactly_one_attempt(self) -> None:
        store = _ServiceStore(
            resource_id="tm.primary",
            lease_error=SQLiteStoreLifecycleError(
                "STORE.GENERATION_CHANGED",
                resource_id="tm.primary",
                generation=0,
                retryable=True,
            ),
        )
        service = TMRetrievalService(
            retriever=cast(Any, _ServiceRetriever()),
            scorer=cast(Any, SimilarityScorerV1()),
        )
        report = service.query(
            (
                _service_handle(
                    store,
                    resource_id="tm.primary",
                    order=0,
                ),
            ),
            _service_query(resource_order=("tm.primary",)),
        )
        self.assertEqual(
            [
                (
                    failure.resource_id,
                    failure.stage,
                    failure.error_code,
                    failure.retryable,
                )
                for failure in report.resource_failures
            ],
            [("tm.primary", "LEASE", "STORE.GENERATION_CHANGED", True)],
        )
        self.assertEqual(store.lease_entries, 1)
        self.assertEqual(store.lease_exits, 0)
