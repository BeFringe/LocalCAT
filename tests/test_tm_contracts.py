from __future__ import annotations

import json
import traceback
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

from tm_contracts import (
    AssetKind,
    AssetPreservationEvidence,
    AssetPreservationState,
    CANDIDATE_BUDGET_VERSION,
    CANDIDATE_PROOF_BLOCK_VERSION_V1,
    CANDIDATE_PROOF_INVOCATION_DOMAIN_VERSION,
    CANDIDATE_PROOF_PARTITION_VERSION,
    CANDIDATE_PROOF_QUERY_VERSION,
    CANDIDATE_PROOF_QUERY_VERSION_V2,
    CANDIDATE_PROOF_RANKING_DOMAIN_VERSION,
    CANDIDATE_PROOF_TRAVERSAL_VERSION,
    CANDIDATE_PROOF_TRAVERSAL_VERSION_V2,
    SCORER_BOUND_VERSION_V1,
    CandidateEvidence,
    CandidateProofMetadata,
    CandidateProofMetadataV2,
    CandidateProofRefinementMetadata,
    CandidateRecallMetadata,
    CandidateRetrievalReport,
    CandidateStage,
    CandidateStageMetadata,
    TM_CONTRACT_CODEC_VERSION,
    ContextEvidence,
    DiagnosticDisposition,
    ExportDiagnostic,
    ExportFailure,
    QueryReport,
    RecoveryLocator,
    ResourceQueryFailure,
    ResourceQueryMetadata,
    SimilarityEvidence,
    SourceBindingState,
    StoreHealth,
    TMContract,
    TMMatchType,
    TMQuery,
    TMRecord,
    TMRecordDraft,
    TMResourceHandle,
    TMResult,
    TMStore,
    contract_from_json,
    contract_to_json,
    candidate_budget_v1,
    export_cleanup_pending_failure,
    export_ledger_ambiguous_failure,
    validate_resource_handles,
)


class _DummyTMStore:
    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        return ()

    def records_by_id(self, record_ids: tuple[int, ...]) -> tuple[TMRecord, ...]:
        return ()

    def append(self, draft: TMRecordDraft) -> TMRecord:
        raise NotImplementedError

    def export_records(self):
        return iter(())

    def health(self):
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


def _context_evidence(*, matched: bool = False) -> ContextEvidence:
    if matched:
        return ContextEvidence(
            comparable_fields=("speaker_raw", "context_prev_raw"),
            matched_fields=("speaker_raw",),
            mismatched_fields=("context_prev_raw",),
            strength_v1=(1, -1, 1, 0, 0),
        )
    return ContextEvidence(
        comparable_fields=(),
        matched_fields=(),
        mismatched_fields=(),
        strength_v1=(0, 0, 0, 0, 0),
    )


def _similarity_evidence(score: float = 0.8) -> SimilarityEvidence:
    return SimilarityEvidence(
        levenshtein_ratio=0.75,
        dice_bigram=0.85,
        final_similarity=score,
    )


def _resource_metadata(
    *,
    resource_id: str = "tm.primary",
    returned_count: int = 1,
    result_limit: int = 10,
) -> ResourceQueryMetadata:
    return ResourceQueryMetadata(
        resource_id=resource_id,
        context_available=False,
        context_unavailable_code="CONTEXT.NOT_INDEXED",
        recall=CandidateRecallMetadata(
            resource_id=resource_id,
            index_kind="GRAM_FALLBACK",
            fuzzy_available=False,
            fuzzy_unavailable_code="FUZZY.CAPABILITY_UNAVAILABLE",
            stages=(),
            union_unique_count=0,
            deduplicated_count=0,
            result_limit=result_limit,
            candidate_budget_version=CANDIDATE_BUDGET_VERSION,
            candidate_budget=candidate_budget_v1(result_limit),
            truncated=False,
        ),
        scored_count=0,
        returned_count=returned_count,
    )


def _result(
    match_type: TMMatchType,
    *,
    resource_id: str = "tm.primary",
    record_id: int = 7,
) -> TMResult:
    query_source = "Open the door."
    if match_type is TMMatchType.FUZZY:
        matched_source = "Open that door."
        similarity = 0.8
        similarity_evidence = _similarity_evidence(similarity)
        context_evidence = _context_evidence()
    elif match_type is TMMatchType.CONTEXT:
        matched_source = query_source
        similarity = 1.0
        similarity_evidence = None
        context_evidence = _context_evidence(matched=True)
    else:
        matched_source = query_source
        similarity = 1.0
        similarity_evidence = None
        context_evidence = _context_evidence()
    return TMResult(
        resource_id=resource_id,
        record_id=record_id,
        query_source=query_source,
        matched_source=matched_source,
        target="开门。",
        match_type=match_type,
        similarity=similarity,
        similarity_evidence=similarity_evidence,
        context_evidence=context_evidence,
        provenance=(("importer", "legacy-jsonl"),),
        stable_tie_key=(0, record_id),
    )


class TMContractTests(unittest.TestCase):
    def test_store_health_is_frozen_and_keeps_gates_independent(self) -> None:
        health = StoreHealth(
            healthy=True,
            schema_version=1,
            generation=3,
            record_count=27,
            index_kind="GRAM_FALLBACK",
            snapshot_binding_digest="a" * 64,
            source_binding_state=SourceBindingState.SOURCE_DIVERGED,
            exact_available=True,
            context_available=True,
            fuzzy_available=False,
            diagnostic_codes=("SOURCE.DIVERGED",),
        )

        self.assertTrue(health.exact_available)
        self.assertTrue(health.context_available)
        self.assertFalse(health.fuzzy_available)
        with self.assertRaises(FrozenInstanceError):
            setattr(health, "healthy", False)
        with self.assertRaisesRegex(
            ValueError,
            "requires exact availability",
        ):
            _ = StoreHealth(
                healthy=True,
                schema_version=1,
                generation=0,
                record_count=0,
                index_kind="UNBUILT",
                snapshot_binding_digest=None,
                source_binding_state=None,
                exact_available=False,
                context_available=False,
                fuzzy_available=True,
                diagnostic_codes=(),
            )
        with self.assertRaisesRegex(ValueError, "stable sorted order"):
            _ = StoreHealth(
                healthy=False,
                schema_version=1,
                generation=0,
                record_count=0,
                index_kind="UNBUILT",
                snapshot_binding_digest=None,
                source_binding_state=None,
                exact_available=False,
                context_available=False,
                fuzzy_available=False,
                diagnostic_codes=("STORE.Z", "STORE.A"),
            )

    def test_record_preserves_raw_context_origin_and_provenance(self) -> None:
        record = TMRecord(
            record_id=7,
            source_raw="  Open the door.\n",
            target_raw="  开门。\n",
            speaker_raw="eileen",
            context_prev_raw="Wait.",
            context_next_raw="Now.",
            file_source="chapter-01.json",
            provenance=(("importer", "legacy-jsonl"), ("project", "demo")),
            legacy_line_no=11,
            origin_batch_id="batch.sha256.abc",
            origin_ordinal=3,
        )

        encoded = contract_to_json(record)
        decoded = contract_from_json(encoded)

        self.assertEqual(decoded, record)
        self.assertEqual(contract_to_json(decoded), encoded)
        self.assertEqual(
            json.loads(encoded)["contract_version"],
            TM_CONTRACT_CODEC_VERSION,
        )
        with self.assertRaises(FrozenInstanceError):
            setattr(record, "source_raw", "changed")

    def test_resource_query_result_and_partial_failure_round_trip(self) -> None:
        contracts = (
            TMRecordDraft(
                source_raw="Open the door.",
                target_raw="开门。",
                speaker_raw=None,
                context_prev_raw=None,
                context_next_raw=None,
                file_source="chapter-01.json",
                provenance=(("writer", "local"),),
            ),
            TMQuery(
                query_source="Open the door.",
                speaker_raw="eileen",
                context_prev_raw="Wait.",
                context_next_raw="Now.",
                minimum_similarity=0.6,
                limit=10,
                resource_order=("tm.primary", "tm.secondary"),
            ),
            _result(TMMatchType.EXACT),
            _result(TMMatchType.CONTEXT),
            _result(TMMatchType.FUZZY),
            ResourceQueryFailure(
                resource_id="tm.secondary",
                stage="QUERY",
                error_code="RESOURCE_UNREADABLE",
                retryable=False,
            ),
            QueryReport(
                results=(_result(TMMatchType.EXACT),),
                resource_failures=(
                    ResourceQueryFailure(
                        resource_id="tm.secondary",
                        stage="QUERY",
                        error_code="RESOURCE_UNREADABLE",
                        retryable=False,
                    ),
                ),
                resource_metadata=(_resource_metadata(),),
            ),
        )

        for contract in contracts:
            with self.subTest(contract=type(contract).__name__):
                encoded = contract_to_json(contract)
                self.assertEqual(contract_from_json(encoded), contract)
                self.assertEqual(contract_to_json(contract_from_json(encoded)), encoded)

    def test_rejects_unsupported_or_missing_codec_version(self) -> None:
        payload = json.loads(contract_to_json(_result(TMMatchType.EXACT)))
        payload["contract_version"] = TM_CONTRACT_CODEC_VERSION + 1
        with self.assertRaisesRegex(ValueError, "unsupported contract version"):
            contract_from_json(json.dumps(payload))

        del payload["contract_version"]
        with self.assertRaisesRegex(ValueError, "contract_version"):
            contract_from_json(json.dumps(payload))

    def test_rejects_invalid_similarity_ranges_and_query_limits(self) -> None:
        for score in (-0.01, 1.01, float("nan"), float("inf")):
            with self.subTest(score=score):
                with self.assertRaises(ValueError):
                    SimilarityEvidence(score, 0.5, 0.5)

        with self.assertRaises(ValueError):
            TMQuery("source", None, None, None, -0.01, 10, ("tm",))
        with self.assertRaises(ValueError):
            TMQuery("source", None, None, None, 0.6, 0, ("tm",))

    def test_rejects_empty_identity_and_invalid_resource_order(self) -> None:
        with self.assertRaises(ValueError):
            TMRecord(
                record_id=0,
                source_raw="source",
                target_raw="target",
                speaker_raw=None,
                context_prev_raw=None,
                context_next_raw=None,
                file_source=None,
                provenance=(),
                legacy_line_no=None,
                origin_batch_id="batch",
                origin_ordinal=0,
            )
        with self.assertRaises(ValueError):
            TMResourceHandle(
                "",
                _DummyTMStore(),
                active=True,
                lookup=True,
                update=True,
                order=0,
            )
        with self.assertRaises(ValueError):
            TMResourceHandle(
                "tm",
                _DummyTMStore(),
                active=True,
                lookup=True,
                update=True,
                order=-1,
            )
        with self.assertRaises(ValueError):
            TMQuery("source", None, None, None, 0.6, 10, ("tm", "tm"))
        with self.assertRaises(ValueError):
            TMQuery("source", None, None, None, 0.6, 10, ("tm", " "))

    def test_resource_handle_collection_requires_unique_identity_and_order(self) -> None:
        store = _DummyTMStore()
        primary = TMResourceHandle(
            "tm.primary",
            store,
            active=True,
            lookup=True,
            update=True,
            order=0,
        )
        self.assertIs(primary.store, store)
        validate_resource_handles((primary,))

        with self.assertRaisesRegex(ValueError, "resource ids.*unique"):
            validate_resource_handles(
                (
                    primary,
                    TMResourceHandle(
                        "tm.primary",
                        _DummyTMStore(),
                        active=True,
                        lookup=True,
                        update=False,
                        order=1,
                    ),
                )
            )
        with self.assertRaisesRegex(ValueError, "resource orders.*unique"):
            validate_resource_handles(
                (
                    primary,
                    TMResourceHandle(
                        "tm.secondary",
                        _DummyTMStore(),
                        active=True,
                        lookup=True,
                        update=False,
                        order=0,
                    ),
                )
            )

    def test_resource_handle_requires_runtime_store_and_is_not_serializable(self) -> None:
        with self.assertRaises(TypeError):
            TMResourceHandle(  # pyright: ignore[reportCallIssue]
                "tm.primary",
                active=True,
                lookup=True,
                update=False,
                order=0,
            )
        with self.assertRaises(ValueError):
            TMResourceHandle(
                "tm.primary",
                cast(TMStore, cast(object, None)),
                active=True,
                lookup=True,
                update=False,
                order=0,
            )

        handle = TMResourceHandle(
            "tm.primary",
            _DummyTMStore(),
            active=True,
            lookup=True,
            update=False,
            order=0,
        )
        with self.assertRaisesRegex(TypeError, "runtime-only"):
            contract_to_json(cast(TMContract, cast(object, handle)))

    def test_match_type_requires_the_correct_evidence_shape(self) -> None:
        with self.assertRaisesRegex(ValueError, "fuzzy.*evidence"):
            TMResult(
                resource_id="tm",
                record_id=1,
                query_source="query",
                matched_source="candidate",
                target="target",
                match_type=TMMatchType.FUZZY,
                similarity=0.8,
                similarity_evidence=None,
                context_evidence=_context_evidence(),
                provenance=(),
                stable_tie_key=(0, 1),
            )

        with self.assertRaisesRegex(ValueError, "EXACT.*evidence"):
            TMResult(
                resource_id="tm",
                record_id=1,
                query_source="same",
                matched_source="same",
                target="target",
                match_type=TMMatchType.EXACT,
                similarity=1.0,
                similarity_evidence=_similarity_evidence(1.0),
                context_evidence=_context_evidence(),
                provenance=(),
                stable_tie_key=(0, 1),
            )

        with self.assertRaisesRegex(ValueError, "CONTEXT.*positive context"):
            TMResult(
                resource_id="tm",
                record_id=1,
                query_source="same",
                matched_source="same",
                target="target",
                match_type=TMMatchType.CONTEXT,
                similarity=1.0,
                similarity_evidence=None,
                context_evidence=_context_evidence(),
                provenance=(),
                stable_tie_key=(0, 1),
            )

    def test_fuzzy_result_keeps_both_sources_and_matching_score(self) -> None:
        result = _result(TMMatchType.FUZZY)

        self.assertNotEqual(result.query_source, result.matched_source)
        evidence = result.similarity_evidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(
            result.similarity,
            evidence.final_similarity,
        )

        with self.assertRaisesRegex(ValueError, "final similarity"):
            TMResult(
                resource_id="tm",
                record_id=1,
                query_source="query",
                matched_source="candidate",
                target="target",
                match_type=TMMatchType.FUZZY,
                similarity=0.8,
                similarity_evidence=_similarity_evidence(0.7),
                context_evidence=_context_evidence(),
                provenance=(),
                stable_tie_key=(0, 1),
            )

    def test_partial_failure_is_a_safe_structured_summary(self) -> None:
        failure = ResourceQueryFailure(
            resource_id="tm.secondary",
            stage="QUERY",
            error_code="RESOURCE_UNREADABLE",
            retryable=False,
        )
        self.assertEqual(
            failure.safe_summary,
            "QUERY:RESOURCE_UNREADABLE:NOT_RETRYABLE",
        )

        with self.assertRaisesRegex(ValueError, "diagnostic identifier"):
            ResourceQueryFailure(
                resource_id="tm.secondary",
                stage="QUERY",
                error_code="failed while reading source text: Open the door.",
                retryable=False,
            )

        payload = json.loads(contract_to_json(failure))
        payload["payload"]["query_source"] = "Open the secret door."
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            contract_from_json(json.dumps(payload))

    def test_context_evidence_is_internally_consistent(self) -> None:
        with self.assertRaises(ValueError):
            ContextEvidence(
                comparable_fields=("speaker_raw",),
                matched_fields=("context_prev_raw",),
                mismatched_fields=(),
                strength_v1=(1, 0, 1, 0, 0),
            )
        with self.assertRaises(ValueError):
            ContextEvidence(
                comparable_fields=("speaker_raw",),
                matched_fields=("speaker_raw",),
                mismatched_fields=("speaker_raw",),
                strength_v1=(1, -1, 1, 0, 0),
            )
        with self.assertRaisesRegex(ValueError, "unsupported context field"):
            ContextEvidence(
                comparable_fields=("display_name",),
                matched_fields=("display_name",),
                mismatched_fields=(),
                strength_v1=(1, 0, 0, 0, 0),
            )
        with self.assertRaisesRegex(ValueError, "flag.*matched fields"):
            ContextEvidence(
                comparable_fields=("speaker_raw",),
                matched_fields=("speaker_raw",),
                mismatched_fields=(),
                strength_v1=(1, 0, 0, 0, 0),
            )

    def test_similarity_evidence_rejects_unfrozen_scorer_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "scorer-v1"):
            SimilarityEvidence(
                levenshtein_ratio=0.8,
                dice_bigram=0.8,
                final_similarity=0.8,
                scorer_version="scorer-v2",
            )

    def test_query_report_requires_closed_resource_metadata(self) -> None:
        report = QueryReport(
            results=(),
            resource_failures=(),
            resource_metadata=(),
        )
        payload = json.loads(contract_to_json(report))["payload"]
        self.assertEqual(payload["resource_metadata"], [])

        envelope = json.loads(contract_to_json(report))
        del envelope["payload"]["resource_metadata"]
        with self.assertRaisesRegex(ValueError, "resource_metadata"):
            contract_from_json(json.dumps(envelope))

        with self.assertRaisesRegex(ValueError, "returned count"):
            QueryReport(
                results=(_result(TMMatchType.EXACT),),
                resource_failures=(),
                resource_metadata=(
                    _resource_metadata(returned_count=0),
                ),
            )

        two_results = (
            _result(
                TMMatchType.EXACT,
                resource_id="tm.primary",
                record_id=7,
            ),
            _result(
                TMMatchType.EXACT,
                resource_id="tm.secondary",
                record_id=8,
            ),
        )
        with self.assertRaisesRegex(ValueError, "same global result limit"):
            QueryReport(
                results=two_results,
                resource_failures=(),
                resource_metadata=(
                    _resource_metadata(
                        resource_id="tm.primary",
                        result_limit=10,
                    ),
                    _resource_metadata(
                        resource_id="tm.secondary",
                        result_limit=5,
                    ),
                ),
            )
        with self.assertRaisesRegex(ValueError, "global result limit"):
            QueryReport(
                results=two_results,
                resource_failures=(),
                resource_metadata=(
                    _resource_metadata(
                        resource_id="tm.primary",
                        result_limit=1,
                    ),
                    _resource_metadata(
                        resource_id="tm.secondary",
                        result_limit=1,
                    ),
                ),
            )

    def test_decoder_does_not_echo_untrusted_field_or_contract_type(self) -> None:
        untrusted_body = "Open the secret door."
        payload = json.loads(contract_to_json(_result(TMMatchType.EXACT)))
        payload["payload"][untrusted_body] = "leak"
        with self.assertRaises(ValueError) as unexpected_field:
            contract_from_json(json.dumps(payload))
        self.assertNotIn(untrusted_body, str(unexpected_field.exception))

        payload = json.loads(contract_to_json(_result(TMMatchType.EXACT)))
        payload["contract_type"] = untrusted_body
        with self.assertRaises(ValueError) as unknown_type:
            contract_from_json(json.dumps(payload))
        self.assertNotIn(untrusted_body, str(unknown_type.exception))

        payload = json.loads(contract_to_json(_result(TMMatchType.EXACT)))
        payload["payload"]["match_type"] = untrusted_body
        with self.assertRaises(ValueError) as unknown_match_type:
            contract_from_json(json.dumps(payload))
        self.assertIsNone(unknown_match_type.exception.__cause__)
        formatted = "".join(
            traceback.format_exception(unknown_match_type.exception)
        )
        self.assertNotIn(untrusted_body, formatted)

        malformed = '{"payload":"' + untrusted_body
        with self.assertRaises(ValueError) as malformed_json:
            contract_from_json(malformed)
        self.assertIsNone(malformed_json.exception.__cause__)
        formatted = "".join(
            traceback.format_exception(malformed_json.exception)
        )
        self.assertNotIn(untrusted_body, formatted)

    def test_result_revalidates_forged_nested_evidence(self) -> None:
        forged_context = object.__new__(ContextEvidence)
        object.__setattr__(
            forged_context,
            "comparable_fields",
            ("speaker_raw",),
        )
        object.__setattr__(
            forged_context,
            "matched_fields",
            ("speaker_raw",),
        )
        object.__setattr__(forged_context, "mismatched_fields", ())
        object.__setattr__(
            forged_context,
            "strength_v1",
            (1, 0, 0, 0, 0),
        )
        with self.assertRaisesRegex(ValueError, "flags.*matched fields"):
            TMResult(
                resource_id="tm",
                record_id=1,
                query_source="same",
                matched_source="same",
                target="target",
                match_type=TMMatchType.CONTEXT,
                similarity=1.0,
                similarity_evidence=None,
                context_evidence=forged_context,
                provenance=(),
                stable_tie_key=(0, 1),
            )

        forged_similarity = object.__new__(SimilarityEvidence)
        object.__setattr__(
            forged_similarity,
            "levenshtein_ratio",
            0.8,
        )
        object.__setattr__(forged_similarity, "dice_bigram", 0.8)
        object.__setattr__(
            forged_similarity,
            "final_similarity",
            0.8,
        )
        object.__setattr__(
            forged_similarity,
            "scorer_version",
            "scorer-v2",
        )
        with self.assertRaisesRegex(ValueError, "scorer-v1"):
            TMResult(
                resource_id="tm",
                record_id=1,
                query_source="query",
                matched_source="candidate",
                target="target",
                match_type=TMMatchType.FUZZY,
                similarity=0.8,
                similarity_evidence=forged_similarity,
                context_evidence=_context_evidence(),
                provenance=(),
                stable_tie_key=(0, 1),
            )


def _proof_v3(
    *,
    unscored_upper: float = 0.7,
    unscored_record_id: int = 4,
    threshold_closed: bool = False,
    top_k_closed: bool = True,
    result_complete: bool = True,
) -> CandidateProofMetadata:
    return CandidateProofMetadata(
        proof_version=CANDIDATE_PROOF_QUERY_VERSION,
        bound_version=SCORER_BOUND_VERSION_V1,
        block_version=CANDIDATE_PROOF_BLOCK_VERSION_V1,
        traversal_version=CANDIDATE_PROOF_TRAVERSAL_VERSION,
        ranking_domain_version=CANDIDATE_PROOF_RANKING_DOMAIN_VERSION,
        invocation_domain_version=CANDIDATE_PROOF_INVOCATION_DOMAIN_VERSION,
        traversal_mode="SPARSE",
        total_block_count=1,
        total_record_count=4,
        scanned_block_count=1,
        opened_block_count=1,
        inspected_record_count=4,
        seed_unique_count=1,
        scorer_invocation_count=2,
        accounted_identity_count=3,
        ranked_eligible_count=3,
        unscored_identity_count=1,
        unscored_max_upper_bound=unscored_upper,
        unscored_possible_record_id=unscored_record_id,
        minimum_similarity=0.6,
        threshold_closed=threshold_closed,
        top_k=2,
        ranked_kth_score=0.8,
        ranked_kth_record_id=2,
        top_k_closed=top_k_closed,
        result_complete=result_complete,
        refinement=None,
    )


def _proof_v2_payload() -> dict[str, object]:
    return {
        "accounted_identity_count": 3,
        "block_version": CANDIDATE_PROOF_BLOCK_VERSION_V1,
        "bound_version": SCORER_BOUND_VERSION_V1,
        "inspected_record_count": 3,
        "kth_record_id": 2,
        "kth_score": 0.8,
        "minimum_similarity": 0.6,
        "opened_block_count": 1,
        "proof_version": CANDIDATE_PROOF_QUERY_VERSION_V2,
        "refinement": None,
        "scanned_block_count": 1,
        "scorer_invocation_count": 2,
        "seed_unique_count": 1,
        "threshold_closed": True,
        "top_k": 2,
        "top_k_closed": True,
        "total_block_count": 1,
        "total_record_count": 3,
        "traversal_mode": "SPARSE",
        "traversal_version": CANDIDATE_PROOF_TRAVERSAL_VERSION_V2,
        "unscored_identity_count": 0,
        "unscored_max_upper_bound": None,
        "unscored_possible_record_id": None,
    }


def _legacy_recall_payload() -> dict[str, object]:
    stages = (
        ("GRAM_2", 0, 1, 1, 0),
        ("BOUND_PROOF", 1, 2, 3, 0),
        ("UNION", 3, 0, 3, 0),
        ("DEDUPLICATE", 3, 0, 3, 0),
    )
    return {
        "candidate_budget": candidate_budget_v1(2),
        "candidate_budget_version": CANDIDATE_BUDGET_VERSION,
        "deduplicated_count": 3,
        "fuzzy_available": True,
        "fuzzy_unavailable_code": None,
        "index_kind": "GRAM_FALLBACK",
        "proof": _proof_v2_payload(),
        "resource_id": "tm.primary",
        "result_limit": 2,
        "stages": [
            {
                "added_unique_count": added,
                "dropped_count": dropped,
                "input_count": input_count,
                "output_unique_count": output,
                "stage": stage,
            }
            for stage, input_count, added, output, dropped in stages
        ],
        "truncated": False,
        "union_unique_count": 3,
    }


def _legacy_report_payload() -> dict[str, object]:
    return {
        "candidates": [
            {
                "matched_grams": 1,
                "overlap_ratio": 1.0,
                "pretruncate_rank": None,
                "query_grams": 1,
                "recall_stages": ["GRAM_2", "BOUND_PROOF"],
                "record_id": record_id,
            }
            for record_id in range(1, 4)
        ],
        "metadata": _legacy_recall_payload(),
    }


class CandidateProofV3ContractTests(unittest.TestCase):
    def _decode_payload(self, payload: dict[str, object]):
        return contract_from_json(json.dumps({
            "contract_version": TM_CONTRACT_CODEC_VERSION,
            "contract_type": "CandidateProofMetadata",
            "payload": payload,
        }))

    def test_v3_conditional_completion_round_trips_without_global_codec_bump(
        self,
    ) -> None:
        proof = _proof_v3()
        self.assertFalse(proof.threshold_closed)
        self.assertTrue(proof.top_k_closed)
        self.assertTrue(proof.result_complete)

        serialized = contract_to_json(proof)
        envelope = json.loads(serialized)
        self.assertEqual(
            envelope["contract_version"],
            TM_CONTRACT_CODEC_VERSION,
        )
        self.assertEqual(contract_from_json(serialized), proof)

        payload_keys = set(envelope["payload"])
        self.assertTrue({
            "invocation_domain_version",
            "ranking_domain_version",
            "ranked_eligible_count",
            "ranked_kth_score",
            "ranked_kth_record_id",
            "result_complete",
        }.issubset(payload_keys))
        self.assertTrue({
            "source_raw",
            "target_raw",
            "query_source",
            "matched_source",
            "source_fold_v1",
            "lcs_length",
            "gram",
            "equivalence_key",
        }.isdisjoint(payload_keys))

    def test_v2_is_strict_historical_decode_only(self) -> None:
        decoded = cast(
            CandidateProofMetadataV2,
            self._decode_payload(_proof_v2_payload()),
        )
        self.assertIs(type(decoded), CandidateProofMetadataV2)
        self.assertEqual(decoded.proof_version, CANDIDATE_PROOF_QUERY_VERSION_V2)
        with self.assertRaisesRegex(TypeError, "decode-only"):
            contract_to_json(cast(TMContract, decoded))

        payload = _proof_v2_payload()
        payload["ranked_eligible_count"] = 3
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            self._decode_payload(payload)

        dense = _proof_v2_payload()
        dense.update({
            "accounted_identity_count": 2,
            "inspected_record_count": 5,
            "opened_block_count": 0,
            "scanned_block_count": 1,
            "total_record_count": 5,
            "traversal_mode": "DENSE",
            "unscored_identity_count": 3,
            "unscored_max_upper_bound": 0.4,
            "unscored_possible_record_id": 2,
            "refinement": {
                "a0_accounted_identity_count": 1,
                "a1_accounted_identity_count": 1,
                "k0_record_id": 1,
                "k0_score": 0.7,
                "p1_max_upper_bound": 0.4,
                "p1_possible_record_id": 2,
                "p1_unscored_identity_count": 2,
                "p2_max_upper_bound": 0.3,
                "p2_possible_record_id": 3,
                "p2_unscored_identity_count": 1,
                "phase": "PHASE_2_COMPLETE",
                "r_refinement_identity_count": 2,
                "refined": True,
                "refinement_request_count": 2,
                "refinement_returned_count": 2,
            },
        })
        dense_decoded = cast(
            CandidateProofMetadataV2,
            self._decode_payload(dense),
        )
        self.assertIsNotNone(dense_decoded.refinement)
        dense_refinement = cast(dict[str, object], dense["refinement"])
        dense_refinement["p3_unscored_identity_count"] = 0
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            self._decode_payload(dense)

    def test_nested_v2_recall_and_report_are_strictly_decode_only(self) -> None:
        recall_serialized = json.dumps({
            "contract_version": TM_CONTRACT_CODEC_VERSION,
            "contract_type": "CandidateRecallMetadata",
            "payload": _legacy_recall_payload(),
        })
        recall = cast(
            CandidateRecallMetadata,
            contract_from_json(recall_serialized),
        )
        self.assertIs(type(recall), CandidateRecallMetadata)
        self.assertIs(type(recall.proof), CandidateProofMetadataV2)
        with self.assertRaisesRegex(TypeError, "decode-only"):
            contract_to_json(recall)

        report_serialized = json.dumps({
            "contract_version": TM_CONTRACT_CODEC_VERSION,
            "contract_type": "CandidateRetrievalReport",
            "payload": _legacy_report_payload(),
        })
        report = cast(
            CandidateRetrievalReport,
            contract_from_json(report_serialized),
        )
        self.assertIs(type(report), CandidateRetrievalReport)
        self.assertTrue(
            all(type(candidate) is CandidateEvidence for candidate in report.candidates)
        )
        self.assertIs(type(report.metadata.proof), CandidateProofMetadataV2)
        with self.assertRaisesRegex(TypeError, "decode-only"):
            contract_to_json(report)

    def test_nested_v2_open_truncated_or_forged_proof_fails_closed(self) -> None:
        for field in ("threshold_closed", "top_k_closed"):
            with self.subTest(open_field=field):
                recall = _legacy_recall_payload()
                proof = cast(dict[str, object], recall["proof"])
                proof[field] = False
                if field == "top_k_closed":
                    proof["kth_score"] = None
                    proof["kth_record_id"] = None
                serialized = json.dumps({
                    "contract_version": TM_CONTRACT_CODEC_VERSION,
                    "contract_type": "CandidateRecallMetadata",
                    "payload": recall,
                })
                with self.assertRaisesRegex(ValueError, "close threshold and top-k"):
                    contract_from_json(serialized)

        truncated = _legacy_recall_payload()
        truncated["truncated"] = True
        with self.assertRaisesRegex(ValueError, "must not truncate"):
            contract_from_json(json.dumps({
                "contract_version": TM_CONTRACT_CODEC_VERSION,
                "contract_type": "CandidateRecallMetadata",
                "payload": truncated,
            }))

        forged = _legacy_recall_payload()
        forged_proof = cast(dict[str, object], forged["proof"])
        forged_proof["ranked_eligible_count"] = 3
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            contract_from_json(json.dumps({
                "contract_version": TM_CONTRACT_CODEC_VERSION,
                "contract_type": "CandidateRecallMetadata",
                "payload": forged,
            }))

    def test_cross_version_fields_and_unknown_version_fail_closed(self) -> None:
        payload = json.loads(contract_to_json(_proof_v3()))["payload"]
        payload["kth_score"] = payload.pop("ranked_kth_score")
        payload["kth_record_id"] = payload.pop("ranked_kth_record_id")
        with self.assertRaises((TypeError, ValueError)):
            self._decode_payload(payload)

        unknown = json.loads(contract_to_json(_proof_v3()))["payload"]
        unknown["proof_version"] = "proof-query-v999"
        with self.assertRaisesRegex(ValueError, "unsupported candidate proof version"):
            self._decode_payload(unknown)

    def test_domain_counts_and_closure_facts_are_derived(self) -> None:
        separated = json.loads(contract_to_json(_proof_v3()))["payload"]
        separated.update({
            "ranked_eligible_count": 1,
            "ranked_kth_score": None,
            "ranked_kth_record_id": None,
            "unscored_max_upper_bound": 0.5,
            "threshold_closed": True,
            "top_k_closed": False,
            "result_complete": True,
        })
        separated_proof = cast(
            CandidateProofMetadata,
            self._decode_payload(separated),
        )
        self.assertGreater(
            separated_proof.scorer_invocation_count,
            separated_proof.ranked_eligible_count,
        )

        for field, value in (
            ("ranking_domain_version", "raw-distinct-v999"),
            ("invocation_domain_version", "exact-fold-v999"),
            ("ranked_eligible_count", 4),
            ("scorer_invocation_count", 4),
            ("threshold_closed", True),
            ("top_k_closed", False),
            ("result_complete", False),
        ):
            with self.subTest(field=field):
                payload = json.loads(contract_to_json(_proof_v3()))["payload"]
                payload[field] = value
                with self.assertRaises(ValueError):
                    self._decode_payload(payload)

    def test_ranked_kth_shape_and_tie_equality_are_strict(self) -> None:
        payload = json.loads(contract_to_json(_proof_v3()))["payload"]
        payload["ranked_kth_record_id"] = None
        with self.assertRaises((TypeError, ValueError)):
            self._decode_payload(payload)

        tie_open = _proof_v3(
            unscored_upper=0.8,
            unscored_record_id=2,
            top_k_closed=False,
            result_complete=False,
        )
        self.assertFalse(tie_open.threshold_closed)
        self.assertFalse(tie_open.top_k_closed)
        self.assertFalse(tie_open.result_complete)
        with self.assertRaisesRegex(ValueError, "result must be complete"):
            CandidateRecallMetadata(
                resource_id="tm.primary",
                index_kind="GRAM_FALLBACK",
                fuzzy_available=True,
                fuzzy_unavailable_code=None,
                stages=(
                    CandidateStageMetadata(CandidateStage.GRAM_2, 0, 1, 1, 0),
                    CandidateStageMetadata(CandidateStage.BOUND_PROOF, 1, 2, 3, 0),
                    CandidateStageMetadata(CandidateStage.UNION, 3, 0, 3, 0),
                    CandidateStageMetadata(CandidateStage.DEDUPLICATE, 3, 0, 3, 0),
                ),
                union_unique_count=3,
                deduplicated_count=3,
                result_limit=2,
                candidate_budget_version=CANDIDATE_BUDGET_VERSION,
                candidate_budget=candidate_budget_v1(2),
                truncated=False,
                proof=tie_open,
            )

    def test_dense_u4_partitions_and_frontier_round_trip(self) -> None:
        refinement = CandidateProofRefinementMetadata(
            phase="DENSE_COMPLETE",
            refined=True,
            partition_version=CANDIDATE_PROOF_PARTITION_VERSION,
            a0_accounted_identity_count=1,
            p1_unscored_identity_count=1,
            r_refinement_identity_count=3,
            p2_unscored_identity_count=1,
            s_post_u3_identity_count=2,
            u4_evaluated_identity_count=2,
            a1_accounted_identity_count=1,
            p3_unscored_identity_count=1,
            refinement_request_count=3,
            refinement_returned_count=3,
            k0_score=0.7,
            k0_record_id=1,
            p1_max_upper_bound=0.1,
            p1_possible_record_id=2,
            p2_max_upper_bound=0.2,
            p2_possible_record_id=3,
            p3_max_upper_bound=0.3,
            p3_possible_record_id=4,
        )
        proof = CandidateProofMetadata(
            proof_version=CANDIDATE_PROOF_QUERY_VERSION,
            bound_version=SCORER_BOUND_VERSION_V1,
            block_version=CANDIDATE_PROOF_BLOCK_VERSION_V1,
            traversal_version=CANDIDATE_PROOF_TRAVERSAL_VERSION,
            ranking_domain_version=CANDIDATE_PROOF_RANKING_DOMAIN_VERSION,
            invocation_domain_version=CANDIDATE_PROOF_INVOCATION_DOMAIN_VERSION,
            traversal_mode="DENSE",
            total_block_count=1,
            total_record_count=5,
            scanned_block_count=1,
            opened_block_count=0,
            inspected_record_count=5,
            seed_unique_count=1,
            scorer_invocation_count=2,
            accounted_identity_count=2,
            ranked_eligible_count=2,
            unscored_identity_count=3,
            unscored_max_upper_bound=0.3,
            unscored_possible_record_id=4,
            minimum_similarity=0.6,
            threshold_closed=True,
            top_k=2,
            ranked_kth_score=0.8,
            ranked_kth_record_id=1,
            top_k_closed=True,
            result_complete=True,
            refinement=refinement,
        )
        self.assertEqual(contract_from_json(contract_to_json(proof)), proof)

        for field, value, error in (
            ("refinement_request_count", 2, "request/response"),
            ("p2_unscored_identity_count", 2, "R/P2/S"),
            ("p3_unscored_identity_count", 0, "S/A1/P3"),
            ("u4_evaluated_identity_count", 3, "exceeds post-U3"),
            ("u4_evaluated_identity_count", 0, "P3 count exceeds"),
            ("partition_version", "proof-partition-v999", "partition version"),
            ("p3_possible_record_id", 6, "outside the proof universe"),
        ):
            with self.subTest(field=field):
                payload = json.loads(contract_to_json(proof))["payload"]
                payload["refinement"][field] = value
                with self.assertRaisesRegex(ValueError, error):
                    self._decode_payload(payload)

        payload = json.loads(contract_to_json(proof))["payload"]
        payload["refinement"]["p3_max_upper_bound"] = 0.4
        with self.assertRaisesRegex(ValueError, "mixed frontier"):
            self._decode_payload(payload)

    def test_dense_lazy_u4_can_close_from_u3_without_u4_evaluation(self) -> None:
        refinement = CandidateProofRefinementMetadata(
            phase="DENSE_COMPLETE",
            refined=True,
            partition_version=CANDIDATE_PROOF_PARTITION_VERSION,
            a0_accounted_identity_count=1,
            p1_unscored_identity_count=1,
            r_refinement_identity_count=3,
            p2_unscored_identity_count=2,
            s_post_u3_identity_count=1,
            u4_evaluated_identity_count=0,
            a1_accounted_identity_count=1,
            p3_unscored_identity_count=0,
            refinement_request_count=3,
            refinement_returned_count=3,
            k0_score=0.7,
            k0_record_id=1,
            p1_max_upper_bound=0.1,
            p1_possible_record_id=2,
            p2_max_upper_bound=0.3,
            p2_possible_record_id=4,
            p3_max_upper_bound=None,
            p3_possible_record_id=None,
        )
        proof = CandidateProofMetadata(
            proof_version=CANDIDATE_PROOF_QUERY_VERSION,
            bound_version=SCORER_BOUND_VERSION_V1,
            block_version=CANDIDATE_PROOF_BLOCK_VERSION_V1,
            traversal_version=CANDIDATE_PROOF_TRAVERSAL_VERSION,
            ranking_domain_version=CANDIDATE_PROOF_RANKING_DOMAIN_VERSION,
            invocation_domain_version=CANDIDATE_PROOF_INVOCATION_DOMAIN_VERSION,
            traversal_mode="DENSE",
            total_block_count=1,
            total_record_count=5,
            scanned_block_count=1,
            opened_block_count=0,
            inspected_record_count=5,
            seed_unique_count=1,
            scorer_invocation_count=2,
            accounted_identity_count=2,
            ranked_eligible_count=2,
            unscored_identity_count=3,
            unscored_max_upper_bound=0.3,
            unscored_possible_record_id=4,
            minimum_similarity=0.6,
            threshold_closed=True,
            top_k=2,
            ranked_kth_score=0.8,
            ranked_kth_record_id=1,
            top_k_closed=True,
            result_complete=True,
            refinement=refinement,
        )
        serialized = contract_to_json(proof)
        self.assertEqual(contract_from_json(serialized), proof)
        payload = json.loads(serialized)["payload"]
        self.assertNotIn("s_u4_identity_count", payload["refinement"])

        for field, value in (
            ("phase", "PHASE_2_COMPLETE"),
            ("s_u4_identity_count", 1),
        ):
            with self.subTest(field=field):
                forged = json.loads(serialized)["payload"]
                forged["refinement"][field] = value
                with self.assertRaises((TypeError, ValueError)):
                    self._decode_payload(forged)


_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _export_diagnostic() -> ExportDiagnostic:
    return ExportDiagnostic(
        code="EXPORT.CLEANUP_PENDING",
        record_id=None,
        disposition=DiagnosticDisposition.WARNING,
        safe_summary="EXPORT_ARTIFACTS_REMAIN",
    )


def _destination_evidence(
    state: AssetPreservationState,
    *,
    before_digest: str | None,
    observed_digest: str | None,
) -> AssetPreservationEvidence:
    return AssetPreservationEvidence(
        asset_kind=AssetKind.EXPORT_DESTINATION,
        state=state,
        before_digest=before_digest,
        observed_digest=observed_digest,
    )


class TMExportFailurePublicationCommittedTests(unittest.TestCase):
    """ExportFailure publication_committed contract and codec tests."""

    def test_default_false_preserves_original_invariant(self) -> None:
        changed_with_locator = ExportFailure(
            stage="EXPORT.PUBLISH",
            error_code="EXPORT.FAILED",
            retryable=False,
            diagnostics=(),
            previous_destination_preservation=_destination_evidence(
                AssetPreservationState.VERIFIED_CHANGED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_B,
            ),
            recovery_locators=(
                RecoveryLocator(
                    path=Path("/catalog/recovery/out.jsonl"),
                    asset_kind=AssetKind.EXPORT_DESTINATION,
                    expected_digest=_DIGEST_A,
                ),
            ),
        )
        self.assertFalse(changed_with_locator.publication_committed)
        with self.assertRaisesRegex(ValueError, "exactly match assets"):
            ExportFailure(
                stage="EXPORT.PUBLISH",
                error_code="EXPORT.FAILED",
                retryable=False,
                diagnostics=(),
                previous_destination_preservation=_destination_evidence(
                    AssetPreservationState.VERIFIED_CHANGED,
                    before_digest=_DIGEST_A,
                    observed_digest=_DIGEST_B,
                ),
                recovery_locators=(),
            )
        with self.assertRaisesRegex(ValueError, "exactly match assets"):
            ExportFailure(
                stage="EXPORT.PUBLISH",
                error_code="EXPORT.FAILED",
                retryable=False,
                diagnostics=(),
                previous_destination_preservation=_destination_evidence(
                    AssetPreservationState.UNVERIFIED,
                    before_digest=_DIGEST_A,
                    observed_digest=None,
                ),
                recovery_locators=(),
            )
        with self.assertRaises(FrozenInstanceError):
            changed_with_locator.publication_committed = True  # pyright: ignore[reportAttributeAccessIssue]

    def test_committed_requires_fail_stop_and_forbids_rollback(self) -> None:
        changed = ExportFailure(
            stage="EXPORT.LEDGER",
            error_code="EXPORT.CLEANUP_PENDING",
            retryable=False,
            diagnostics=(),
            previous_destination_preservation=_destination_evidence(
                AssetPreservationState.VERIFIED_CHANGED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_B,
            ),
            recovery_locators=(),
            publication_committed=True,
        )
        self.assertTrue(changed.publication_committed)
        with self.assertRaisesRegex(ValueError, "not retryable"):
            ExportFailure(
                stage="EXPORT.LEDGER",
                error_code="EXPORT.CLEANUP_PENDING",
                retryable=True,
                diagnostics=(),
                previous_destination_preservation=_destination_evidence(
                    AssetPreservationState.VERIFIED_CHANGED,
                    before_digest=_DIGEST_A,
                    observed_digest=_DIGEST_B,
                ),
                recovery_locators=(),
                publication_committed=True,
            )
        with self.assertRaisesRegex(ValueError, "cannot restore"):
            ExportFailure(
                stage="EXPORT.LEDGER",
                error_code="EXPORT.CLEANUP_PENDING",
                retryable=False,
                diagnostics=(),
                previous_destination_preservation=_destination_evidence(
                    AssetPreservationState.VERIFIED_CHANGED,
                    before_digest=_DIGEST_A,
                    observed_digest=_DIGEST_B,
                ),
                recovery_locators=(
                    RecoveryLocator(
                        path=Path("/catalog/recovery/out.jsonl"),
                        asset_kind=AssetKind.EXPORT_DESTINATION,
                        expected_digest=_DIGEST_A,
                    ),
                ),
                publication_committed=True,
            )

    def test_committed_evidence_allows_truthful_states_without_locator(
        self,
    ) -> None:
        for evidence in (
            _destination_evidence(
                AssetPreservationState.NOT_APPLICABLE,
                before_digest=None,
                observed_digest=None,
            ),
            _destination_evidence(
                AssetPreservationState.VERIFIED_UNCHANGED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_A,
            ),
            _destination_evidence(
                AssetPreservationState.VERIFIED_CHANGED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_B,
            ),
            _destination_evidence(
                AssetPreservationState.UNVERIFIED,
                before_digest=_DIGEST_A,
                observed_digest=None,
            ),
        ):
            with self.subTest(state=evidence.state):
                failure = ExportFailure(
                    stage="EXPORT.LEDGER",
                    error_code="EXPORT.CLEANUP_PENDING",
                    retryable=False,
                    diagnostics=(),
                    previous_destination_preservation=evidence,
                    recovery_locators=(),
                    publication_committed=True,
                )
                self.assertTrue(failure.publication_committed)
                self.assertEqual(failure.recovery_locators, ())

    def test_committed_not_applicable_requires_no_prior_destination(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "not-applicable"):
            ExportFailure(
                stage="EXPORT.LEDGER",
                error_code="EXPORT.CLEANUP_PENDING",
                retryable=False,
                diagnostics=(),
                previous_destination_preservation=_destination_evidence(
                    AssetPreservationState.NOT_APPLICABLE,
                    before_digest=_DIGEST_A,
                    observed_digest=None,
                ),
                recovery_locators=(),
                publication_committed=True,
            )

    def test_cleanup_pending_builder_contract(self) -> None:
        fresh = export_cleanup_pending_failure(
            stage="EXPORT.LEDGER",
            destination_before=None,
            destination_observed=_DIGEST_B,
        )
        self.assertEqual(fresh.error_code, "EXPORT.CLEANUP_PENDING")
        self.assertFalse(fresh.retryable)
        self.assertTrue(fresh.publication_committed)
        self.assertEqual(fresh.recovery_locators, ())
        self.assertEqual(
            fresh.previous_destination_preservation.state,
            AssetPreservationState.NOT_APPLICABLE,
        )

        changed = export_cleanup_pending_failure(
            stage="EXPORT.LEDGER",
            destination_before=_DIGEST_A,
            destination_observed=_DIGEST_B,
            diagnostics=(_export_diagnostic(),),
        )
        self.assertTrue(changed.publication_committed)
        self.assertEqual(
            changed.previous_destination_preservation.state,
            AssetPreservationState.VERIFIED_CHANGED,
        )
        self.assertEqual(
            changed.previous_destination_preservation.before_digest,
            _DIGEST_A,
        )
        self.assertEqual(
            changed.previous_destination_preservation.observed_digest,
            _DIGEST_B,
        )

        unverified = export_cleanup_pending_failure(
            stage="EXPORT.LEDGER",
            destination_before=_DIGEST_A,
            destination_observed=None,
        )
        self.assertEqual(
            unverified.previous_destination_preservation.state,
            AssetPreservationState.UNVERIFIED,
        )
        self.assertEqual(
            unverified.previous_destination_preservation.before_digest,
            _DIGEST_A,
        )
        self.assertIsNone(
            unverified.previous_destination_preservation.observed_digest
        )

    def test_publication_committed_round_trips_and_old_payload_decodes(
        self,
    ) -> None:
        failure = ExportFailure(
            stage="EXPORT.LEDGER",
            error_code="EXPORT.CLEANUP_PENDING",
            retryable=False,
            diagnostics=(_export_diagnostic(),),
            previous_destination_preservation=_destination_evidence(
                AssetPreservationState.VERIFIED_CHANGED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_B,
            ),
            recovery_locators=(),
            publication_committed=True,
        )
        encoded = contract_to_json(failure)
        self.assertTrue(
            json.loads(encoded)["payload"]["publication_committed"]
        )
        self.assertEqual(contract_from_json(encoded), failure)
        self.assertEqual(contract_to_json(contract_from_json(encoded)), encoded)

        legacy_encoded = contract_to_json(
            ExportFailure(
                stage="EXPORT.LEDGER",
                error_code="EXPORT.FAILED",
                retryable=False,
                diagnostics=(),
                previous_destination_preservation=_destination_evidence(
                    AssetPreservationState.VERIFIED_UNCHANGED,
                    before_digest=_DIGEST_A,
                    observed_digest=_DIGEST_A,
                ),
                recovery_locators=(),
            )
        )
        legacy = json.loads(legacy_encoded)
        del legacy["payload"]["publication_committed"]
        decoded = contract_from_json(json.dumps(legacy, sort_keys=True))
        self.assertIsInstance(decoded, ExportFailure)
        assert isinstance(decoded, ExportFailure)
        self.assertEqual(decoded.publication_committed, False)
        self.assertEqual(decoded.error_code, "EXPORT.FAILED")
        self.assertEqual(
            decoded.previous_destination_preservation.state,
            AssetPreservationState.VERIFIED_UNCHANGED,
        )

        default_failure = ExportFailure(
            stage="EXPORT.PUBLISH",
            error_code="EXPORT.FAILED",
            retryable=True,
            diagnostics=(),
            previous_destination_preservation=_destination_evidence(
                AssetPreservationState.VERIFIED_UNCHANGED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_A,
            ),
            recovery_locators=(),
        )
        self.assertFalse(default_failure.publication_committed)
        self.assertEqual(
            contract_from_json(contract_to_json(default_failure)),
            default_failure,
        )


class TMExportFailurePublicationAmbiguousTests(unittest.TestCase):
    """ExportFailure publication_commit_ambiguous contract and codec tests."""

    def _ambiguous_evidence(
        self,
        state: AssetPreservationState,
        *,
        before_digest: str | None,
        observed_digest: str | None,
    ) -> AssetPreservationEvidence:
        return _destination_evidence(
            state,
            before_digest=before_digest,
            observed_digest=observed_digest,
        )

    def test_default_false_preserves_original_invariant(self) -> None:
        failure = ExportFailure(
            stage="EXPORT.PUBLISH",
            error_code="EXPORT.FAILED",
            retryable=False,
            diagnostics=(),
            previous_destination_preservation=_destination_evidence(
                AssetPreservationState.VERIFIED_CHANGED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_B,
            ),
            recovery_locators=(
                RecoveryLocator(
                    path=Path("/catalog/recovery/out.jsonl"),
                    asset_kind=AssetKind.EXPORT_DESTINATION,
                    expected_digest=_DIGEST_A,
                ),
            ),
        )
        self.assertFalse(failure.publication_commit_ambiguous)
        self.assertFalse(failure.publication_committed)
        with self.assertRaises(FrozenInstanceError):
            failure.publication_commit_ambiguous = True  # pyright: ignore[reportAttributeAccessIssue]

    def test_ambiguous_requires_fail_stop_and_forbids_locator(self) -> None:
        changed = ExportFailure(
            stage="EXPORT.LEDGER",
            error_code="EXPORT.LEDGER_AMBIGUOUS",
            retryable=False,
            diagnostics=(),
            previous_destination_preservation=_destination_evidence(
                AssetPreservationState.VERIFIED_CHANGED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_B,
            ),
            recovery_locators=(),
            publication_commit_ambiguous=True,
        )
        self.assertTrue(changed.publication_commit_ambiguous)
        self.assertFalse(changed.publication_committed)
        with self.assertRaisesRegex(ValueError, "not retryable"):
            ExportFailure(
                stage="EXPORT.LEDGER",
                error_code="EXPORT.LEDGER_AMBIGUOUS",
                retryable=True,
                diagnostics=(),
                previous_destination_preservation=_destination_evidence(
                    AssetPreservationState.VERIFIED_CHANGED,
                    before_digest=_DIGEST_A,
                    observed_digest=_DIGEST_B,
                ),
                recovery_locators=(),
                publication_commit_ambiguous=True,
            )
        with self.assertRaisesRegex(ValueError, "cannot fabricate"):
            ExportFailure(
                stage="EXPORT.LEDGER",
                error_code="EXPORT.LEDGER_AMBIGUOUS",
                retryable=False,
                diagnostics=(),
                previous_destination_preservation=_destination_evidence(
                    AssetPreservationState.VERIFIED_CHANGED,
                    before_digest=_DIGEST_A,
                    observed_digest=_DIGEST_B,
                ),
                recovery_locators=(
                    RecoveryLocator(
                        path=Path("/catalog/recovery/out.jsonl"),
                        asset_kind=AssetKind.EXPORT_DESTINATION,
                        expected_digest=_DIGEST_A,
                    ),
                ),
                publication_commit_ambiguous=True,
            )

    def test_ambiguous_is_mutually_exclusive_with_committed(self) -> None:
        with self.assertRaisesRegex(ValueError, "contradictory"):
            ExportFailure(
                stage="EXPORT.LEDGER",
                error_code="EXPORT.LEDGER_AMBIGUOUS",
                retryable=False,
                diagnostics=(),
                previous_destination_preservation=_destination_evidence(
                    AssetPreservationState.VERIFIED_CHANGED,
                    before_digest=_DIGEST_A,
                    observed_digest=_DIGEST_B,
                ),
                recovery_locators=(),
                publication_committed=True,
                publication_commit_ambiguous=True,
            )

    def test_ambiguous_allows_truthful_states_without_locator(self) -> None:
        for evidence in (
            _destination_evidence(
                AssetPreservationState.NOT_APPLICABLE,
                before_digest=None,
                observed_digest=None,
            ),
            _destination_evidence(
                AssetPreservationState.VERIFIED_UNCHANGED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_A,
            ),
            _destination_evidence(
                AssetPreservationState.VERIFIED_CHANGED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_B,
            ),
            _destination_evidence(
                AssetPreservationState.UNVERIFIED,
                before_digest=_DIGEST_A,
                observed_digest=None,
            ),
        ):
            with self.subTest(state=evidence.state):
                failure = ExportFailure(
                    stage="EXPORT.LEDGER",
                    error_code="EXPORT.LEDGER_AMBIGUOUS",
                    retryable=False,
                    diagnostics=(),
                    previous_destination_preservation=evidence,
                    recovery_locators=(),
                    publication_commit_ambiguous=True,
                )
                self.assertTrue(failure.publication_commit_ambiguous)
                self.assertFalse(failure.publication_committed)
                self.assertEqual(failure.recovery_locators, ())

    def test_ambiguous_builder_contract(self) -> None:
        fresh = export_ledger_ambiguous_failure(
            stage="EXPORT.LEDGER",
            error_code="STORE.PROBE_UNAVAILABLE",
            destination_before=None,
            destination_observed=_DIGEST_B,
        )
        self.assertEqual(fresh.error_code, "STORE.PROBE_UNAVAILABLE")
        self.assertFalse(fresh.retryable)
        self.assertFalse(fresh.publication_committed)
        self.assertTrue(fresh.publication_commit_ambiguous)
        self.assertEqual(fresh.recovery_locators, ())
        self.assertEqual(
            fresh.previous_destination_preservation.state,
            AssetPreservationState.NOT_APPLICABLE,
        )
        self.assertIsNone(fresh.previous_destination_preservation.before_digest)
        self.assertIsNone(
            fresh.previous_destination_preservation.observed_digest
        )

        changed = export_ledger_ambiguous_failure(
            stage="EXPORT.LEDGER",
            error_code="STORE.PROBE_UNAVAILABLE",
            destination_before=_DIGEST_A,
            destination_observed=_DIGEST_B,
            diagnostics=(_export_diagnostic(),),
        )
        self.assertTrue(changed.publication_commit_ambiguous)
        self.assertFalse(changed.publication_committed)
        self.assertEqual(
            changed.previous_destination_preservation.state,
            AssetPreservationState.VERIFIED_CHANGED,
        )
        self.assertEqual(
            changed.previous_destination_preservation.before_digest,
            _DIGEST_A,
        )
        self.assertEqual(
            changed.previous_destination_preservation.observed_digest,
            _DIGEST_B,
        )

        unchanged = export_ledger_ambiguous_failure(
            stage="EXPORT.LEDGER",
            error_code="STORE.PROBE_UNAVAILABLE",
            destination_before=_DIGEST_A,
            destination_observed=_DIGEST_A,
        )
        self.assertEqual(
            unchanged.previous_destination_preservation.state,
            AssetPreservationState.VERIFIED_UNCHANGED,
        )

        unverified = export_ledger_ambiguous_failure(
            stage="EXPORT.LEDGER",
            error_code="STORE.PROBE_UNAVAILABLE",
            destination_before=_DIGEST_A,
            destination_observed=None,
        )
        self.assertEqual(
            unverified.previous_destination_preservation.state,
            AssetPreservationState.UNVERIFIED,
        )
        self.assertEqual(
            unverified.previous_destination_preservation.before_digest,
            _DIGEST_A,
        )
        self.assertIsNone(
            unverified.previous_destination_preservation.observed_digest
        )

    def test_ambiguous_round_trips_and_legacy_payloads_decode_false(
        self,
    ) -> None:
        failure = export_ledger_ambiguous_failure(
            stage="EXPORT.LEDGER",
            error_code="STORE.PROBE_UNAVAILABLE",
            destination_before=_DIGEST_A,
            destination_observed=_DIGEST_B,
            diagnostics=(_export_diagnostic(),),
        )
        encoded = contract_to_json(failure)
        self.assertTrue(
            json.loads(encoded)["payload"]["publication_commit_ambiguous"]
        )
        self.assertFalse(
            json.loads(encoded)["payload"]["publication_committed"]
        )
        self.assertEqual(contract_from_json(encoded), failure)
        self.assertEqual(contract_to_json(contract_from_json(encoded)), encoded)

        legacy_candidate = export_ledger_ambiguous_failure(
            stage="EXPORT.LEDGER",
            error_code="STORE.PROBE_UNAVAILABLE",
            destination_before=_DIGEST_A,
            destination_observed=_DIGEST_A,
        )
        legacy_payload = json.loads(contract_to_json(legacy_candidate))
        del legacy_payload["payload"]["publication_commit_ambiguous"]
        decoded = contract_from_json(
            json.dumps(legacy_payload, sort_keys=True)
        )
        self.assertIsInstance(decoded, ExportFailure)
        assert isinstance(decoded, ExportFailure)
        self.assertFalse(decoded.publication_commit_ambiguous)
        self.assertFalse(decoded.publication_committed)
        self.assertEqual(decoded.error_code, "STORE.PROBE_UNAVAILABLE")
        self.assertEqual(
            decoded.previous_destination_preservation.state,
            AssetPreservationState.VERIFIED_UNCHANGED,
        )

        payload = json.loads(encoded)
        del payload["payload"]["publication_committed"]
        decoded = contract_from_json(json.dumps(payload, sort_keys=True))
        assert isinstance(decoded, ExportFailure)
        self.assertTrue(decoded.publication_commit_ambiguous)
        self.assertFalse(decoded.publication_committed)

        payload = json.loads(contract_to_json(legacy_candidate))
        del payload["payload"]["publication_commit_ambiguous"]
        del payload["payload"]["publication_committed"]
        decoded = contract_from_json(json.dumps(payload, sort_keys=True))
        assert isinstance(decoded, ExportFailure)
        self.assertFalse(decoded.publication_commit_ambiguous)
        self.assertFalse(decoded.publication_committed)
        self.assertEqual(decoded.error_code, "STORE.PROBE_UNAVAILABLE")

        with self.assertRaises(ValueError):
            contract_from_json(
                contract_to_json(
                    ExportFailure(
                        stage="EXPORT.LEDGER",
                        error_code="EXPORT.LEDGER_AMBIGUOUS",
                        retryable=False,
                        diagnostics=(),
                        previous_destination_preservation=(
                            _destination_evidence(
                                AssetPreservationState.VERIFIED_CHANGED,
                                before_digest=_DIGEST_A,
                                observed_digest=_DIGEST_B,
                            )
                        ),
                        recovery_locators=(),
                        publication_committed=True,
                        publication_commit_ambiguous=True,
                    )
                )
            )


if __name__ == "__main__":
    unittest.main()
