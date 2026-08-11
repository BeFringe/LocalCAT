"""Task 7.1/7.2 focused tests: exact/context classification and fuzzy scoring."""

from __future__ import annotations

import unittest
from collections.abc import Callable, Iterator
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
)
from tm_retrieval import (
    ExactContextClassification,
    FuzzyScoringResult,
    classify_exact_context,
    compare_context_v1,
    query_resource_exact,
    score_fuzzy_candidates,
)
from tm_similarity import SimilarityScorerV1


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
