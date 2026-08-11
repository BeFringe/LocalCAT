"""Task 7.1 focused tests: exact winner and raw context classification."""

from __future__ import annotations

import unittest
from collections.abc import Iterator
from typing import Any, cast

from tm_contracts import (
    ContextEvidence,
    StoreHealth,
    TMMatchType,
    TMQuery,
    TMRecord,
    TMRecordDraft,
    TMResourceHandle,
    TMStore,
)
from tm_retrieval import (
    ExactContextClassification,
    classify_exact_context,
    compare_context_v1,
    query_resource_exact,
)


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


if __name__ == "__main__":
    unittest.main()
