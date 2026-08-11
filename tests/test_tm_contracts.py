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
    CandidateRecallMetadata,
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
