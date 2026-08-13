"""Closed Feature 5 behavioral acceptance evidence registry.

This test-support module keeps matcher, retrieval, and metadata claims
separate from the Task 9.1/9.2 fault taxonomy.  Every row binds one stable
claim to exact unittest ids and the production seam whose current bytes must
remain part of fresh evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import re


ACCEPTANCE_MATRIX_SCHEMA_VERSION = "tm-acceptance-matrix-v1"

_ROW_ID = re.compile(r"9\.[34]\.[A-Z_]+\.\d{2}\Z")
_TEST_ID = re.compile(
    r"tests\.test_[a-z0-9_]+\.[A-Za-z0-9_]+\.test_[a-z0-9_]+\Z"
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class AcceptanceDomain(str, Enum):
    MATCHER = "MATCHER"
    CONTEXT = "CONTEXT"
    CANDIDATE = "CANDIDATE"
    SERVICE = "SERVICE"
    NONIMPERSONATE = "NONIMPERSONATE"
    METADATA = "METADATA"


@dataclass(frozen=True)
class AcceptanceMatrixRow:
    """One closed behavioral claim backed by exact executable tests."""

    row_id: str
    task: str
    domain: AcceptanceDomain
    claim: str
    production_seam: str
    test_ids: tuple[str, ...]
    assertion_contract: str

    def __post_init__(self) -> None:
        if type(self.row_id) is not str or _ROW_ID.fullmatch(self.row_id) is None:
            raise ValueError("acceptance row id must use the closed 9.3/9.4 format")
        if type(self.task) is not str or self.task not in {"9.3", "9.4"}:
            raise ValueError("acceptance task must be 9.3 or 9.4")
        if not self.row_id.startswith(self.task + "."):
            raise ValueError("acceptance row id must bind its task")
        if type(self.domain) is not AcceptanceDomain:
            raise TypeError("acceptance domain must be AcceptanceDomain")
        if f".{self.domain.value}." not in self.row_id:
            raise ValueError("acceptance row id must bind its domain")
        for field_name, value in (
            ("claim", self.claim),
            ("production seam", self.production_seam),
            ("assertion contract", self.assertion_contract),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        source_path = self.production_seam.partition(":")[0]
        if not source_path.endswith(".py") or Path(source_path).is_absolute():
            raise ValueError("production seam must begin with a relative .py path")
        if type(self.test_ids) is not tuple or not self.test_ids:
            raise ValueError("acceptance row must reference at least one test")
        if len(self.test_ids) != len(set(self.test_ids)):
            raise ValueError("acceptance row test ids must be unique")
        if any(
            type(test_id) is not str or _TEST_ID.fullmatch(test_id) is None
            for test_id in self.test_ids
        ):
            raise ValueError("acceptance row contains an invalid unittest id")


def _row(
    row_id: str,
    domain: AcceptanceDomain,
    claim: str,
    production_seam: str,
    test_ids: tuple[str, ...],
    assertion_contract: str,
) -> AcceptanceMatrixRow:
    return AcceptanceMatrixRow(
        row_id=row_id,
        task=".".join(row_id.split(".")[:2]),
        domain=domain,
        claim=claim,
        production_seam=production_seam,
        test_ids=test_ids,
        assertion_contract=assertion_contract,
    )


TASK_9_3_ROWS: tuple[AcceptanceMatrixRow, ...] = (
    _row(
        "9.3.MATCHER.01",
        AcceptanceDomain.MATCHER,
        "Matcher capability derives exactly three fail-safe states",
        "matcher_capability.py:state evaluator",
        (
            "tests.test_matcher_capability.MatcherCapabilityTests."
            "test_versioned_state_matrix_is_closed_and_fail_safe",
            "tests.test_matcher_capability.MatcherCapabilityTests."
            "test_available_profiles_are_fixed_by_the_derived_state",
        ),
        "Only UNAVAILABLE, BASIC_VALIDATED, or TEXT_V1_VALIDATED can be published from current evidence.",
    ),
    _row(
        "9.3.MATCHER.02",
        AcceptanceDomain.MATCHER,
        "Missing, stale, tampered, or version-mismatched evidence degrades safely",
        "matcher_capability.py:evidence validation",
        (
            "tests.test_feature5_validation.MatcherReleaseEvidenceTests."
            "test_missing_matcher_input_publishes_unavailable_snapshot",
            "tests.test_feature5_validation.MatcherReleaseEvidenceTests."
            "test_full_fixture_byte_tamper_downgrades_to_valid_basic",
            "tests.test_feature5_validation.MatcherReleaseEvidenceTests."
            "test_source_tamper_fails_closed_instead_of_self_signing",
            "tests.test_text_matcher_contracts.TextMatcherContractTests."
            "test_manifest_v1_shape_is_strictly_rejected",
        ),
        "Invalid full evidence can only downgrade to proven BASIC; invalid common evidence revokes all matcher profiles.",
    ),
    _row(
        "9.3.MATCHER.03",
        AcceptanceDomain.MATCHER,
        "Purpose, state, profile, and options form one closed authorization matrix",
        "capability_gated_text_matcher.py:gated execution port",
        (
            "tests.test_capability_gated_text_matcher.CapabilityGatedTextMatcherV1Tests."
            "test_state_profile_options_matrix_is_fail_closed",
            "tests.test_text_matcher_contracts.TextMatcherContractTests."
            "test_state_profile_options_matrix_is_mechanically_closed",
            "tests.test_text_matcher_v1.TextMatcherV1Tests."
            "test_fixture_covers_profiles_and_all_configurable_options",
        ),
        "Unsupported purpose/profile/options combinations reject without fallback or inferred permission.",
    ),
    _row(
        "9.3.MATCHER.04",
        AcceptanceDomain.MATCHER,
        "Every matcher call consumes exactly one immutable capability snapshot",
        "capability_gated_text_matcher.py:single-snapshot execution",
        (
            "tests.test_capability_gated_text_matcher.CapabilityGatedTextMatcherV1Tests."
            "test_inflight_call_uses_exactly_one_snapshot",
            "tests.test_capability_gated_text_matcher.CapabilityGatedTextMatcherV1Tests."
            "test_expectation_drift_during_refresh_fails_closed",
            "tests.test_capability_gated_text_matcher.CapabilityGatedTextMatcherV1Tests."
            "test_caller_expectation_aba_cannot_authorize_other_artifact",
        ),
        "Refresh races and caller ABA cannot mix authorization facts or change an in-flight decision.",
    ),
    _row(
        "9.3.MATCHER.05",
        AcceptanceDomain.MATCHER,
        "Matcher rejection diagnostics never execute the algorithm or expose body text",
        "capability_gated_text_matcher.py:rejection outcome",
        (
            "tests.test_capability_gated_text_matcher.CapabilityGatedTextMatcherV1Tests."
            "test_rejection_never_executes_algorithm_or_leaks_content",
            "tests.test_text_matcher_contracts.TextMatcherContractTests."
            "test_rejection_is_safe_and_code_is_closed_to_the_matrix",
        ),
        "A rejected request returns only stable codes and an opaque request digest with no hits or source body.",
    ),
    _row(
        "9.3.CONTEXT.01",
        AcceptanceDomain.CONTEXT,
        "Context vectors compare raw fields without fold, strip, or normalization",
        "tm_retrieval.py:raw context comparison",
        (
            "tests.test_tm_retrieval.ContextSemanticsTests."
            "test_comparison_never_normalizes_folds_or_strips",
        ),
        "Context evidence is emitted only from exact raw same-source field equality.",
    ),
    _row(
        "9.3.CONTEXT.02",
        AcceptanceDomain.CONTEXT,
        "Same-source variants classify as winner EXACT, evidenced CONTEXT, or retained-only",
        "tm_retrieval.py:same-source classification",
        (
            "tests.test_tm_retrieval.ExactWinnerTests."
            "test_maximum_record_identity_is_the_last_valid_record_winner",
            "tests.test_tm_retrieval.ExactWinnerTests."
            "test_retained_only_variants_preserve_full_records_and_are_omitted",
            "tests.test_tm_retrieval.GoldenVectorTests."
            "test_golden_vectors_classify_types_strength_and_retained_variants",
        ),
        "Only the compatibility winner is EXACT and only positive raw-context evidence promotes another same-source record.",
    ),
    _row(
        "9.3.CANDIDATE.01",
        AcceptanceDomain.CANDIDATE,
        "Candidate union, dedupe, and optional truncate conserve a continuous stage ledger",
        "tm_candidate_index.py:candidate retrieval stages",
        (
            "tests.test_tm_candidate_index.CandidateRetrieverTests."
            "test_pool_above_budget_truncates_only_after_stable_preorder",
            "tests.test_tm_candidate_index.CandidateRetrieverTests."
            "test_fts_empty_or_low_pool_unions_grams_with_continuous_counts",
            "tests.test_tm_candidate_index.CandidateRetrieverTests."
            "test_no_fts_long_path_is_321_and_preorder_uses_ratio_length_then_id",
            "tests.test_tm_benchmark_contract.CandidateMetadataContractTests."
            "test_union_deduplicate_truncate_and_budget_must_close",
        ),
        "Each candidate identity is counted at one real stage and truncation follows the stable pre-order only after dedupe.",
    ),
    _row(
        "9.3.CANDIDATE.02",
        AcceptanceDomain.CANDIDATE,
        "Unavailable or zero-hit recall never fabricates candidate work",
        "tm_candidate_index.py:unavailable recall outcome",
        (
            "tests.test_tm_candidate_index.CandidateRetrieverTests."
            "test_empty_query_is_explicitly_unavailable_and_does_not_fake_stages",
            "tests.test_tm_retrieval.FuzzyScoringTests."
            "test_fuzzy_unavailable_report_scores_nothing",
            "tests.test_tm_benchmark_contract.CandidateMetadataContractTests."
            "test_fuzzy_unavailable_requires_empty_recall_and_safe_code",
        ),
        "A closed recall path reports no candidates or stages and carries one stable unavailable code.",
    ),
    _row(
        "9.3.SERVICE.01",
        AcceptanceDomain.SERVICE,
        "Global limit applies only after stable cross-resource aggregation",
        "tm_retrieval.py:service aggregation",
        (
            "tests.test_tm_retrieval.TMRetrievalServiceAggregationTests."
            "test_global_limit_is_applied_only_after_cross_resource_aggregation",
            "tests.test_tm_retrieval.TMRetrievalServiceAggregationTests."
            "test_full_aggregation_order_is_exact_context_then_fuzzy",
            "tests.test_tm_retrieval_validation.RetrievalValidationReleaseTests."
            "test_service_global_limit_aggregates_before_limit",
        ),
        "The service sorts the full resource union before the one global result slice.",
    ),
    _row(
        "9.3.SERVICE.02",
        AcceptanceDomain.SERVICE,
        "One resource failure remains local and preserves healthy resource results",
        "tm_retrieval.py:partial resource failure",
        (
            "tests.test_tm_retrieval.TMRetrievalServicePartialFailureTests."
            "test_partial_failures_isolate_resources_with_stable_codes",
            "tests.test_tm_retrieval_validation.RetrievalValidationReleaseTests."
            "test_service_partial_failure_preserves_healthy_resource",
        ),
        "A failed resource yields a body-safe stable summary without aborting or altering another resource.",
    ),
    _row(
        "9.3.SERVICE.03",
        AcceptanceDomain.SERVICE,
        "Gate-closed and available-with-zero-hits remain distinguishable",
        "tm_retrieval.py:query-effective availability",
        (
            "tests.test_tm_retrieval.TMRetrievalServiceAvailabilityTests."
            "test_context_gate_closed_is_distinct_from_available_zero_hits",
            "tests.test_tm_retrieval.TMRetrievalServiceAvailabilityTests."
            "test_fuzzy_gate_closed_is_distinct_from_available_zero_hits",
        ),
        "Unavailable paths carry a code and no work; available zero-hit paths carry no unavailable code.",
    ),
    _row(
        "9.3.NONIMPERSONATE.01",
        AcceptanceDomain.NONIMPERSONATE,
        "The text matcher algorithm cannot publish or infer capability",
        "text_matcher.py:pure matcher boundary",
        (
            "tests.test_text_matcher_v1.TextMatcherV1Tests."
            "test_internal_algorithm_does_not_implement_capability_gating",
            "tests.test_matcher_capability.MatcherCapabilityTests."
            "test_module_stays_core_internal_and_does_not_execute_match",
        ),
        "Algorithm and evaluator remain separate owners; neither exposes the other's authority.",
    ),
    _row(
        "9.3.NONIMPERSONATE.02",
        AcceptanceDomain.NONIMPERSONATE,
        "CONTEXT, fuzzy-core, and each Gate D path grant independently",
        "tm_retrieval_capability.py:independent gates",
        (
            "tests.test_tm_retrieval_capability.IndependentGateTests."
            "test_closed_context_never_revokes_fuzzy_or_exact_capability",
            "tests.test_tm_retrieval_capability.IndependentGateTests."
            "test_closed_fuzzy_core_closes_both_paths_despite_valid_reports",
            "tests.test_tm_retrieval_validation.RetrievalValidationReleaseTests."
            "test_context_and_fuzzy_core_open_while_gate_d_paths_close",
        ),
        "No matcher, CONTEXT, fuzzy-core, or benchmark fact can impersonate another gate.",
    ),
    _row(
        "9.3.NONIMPERSONATE.03",
        AcceptanceDomain.NONIMPERSONATE,
        "Each result type requires its own exact evidence shape",
        "tm_contracts.py:TM result invariants",
        (
            "tests.test_tm_contracts.TMContractTests."
            "test_match_type_requires_the_correct_evidence_shape",
            "tests.test_tm_retrieval.FuzzyScoringResultValidationTests."
            "test_non_fuzzy_result_is_rejected",
        ),
        "EXACT, CONTEXT, and FUZZY cannot be relabeled without the corresponding contract evidence.",
    ),
    _row(
        "9.3.METADATA.01",
        AcceptanceDomain.METADATA,
        "Candidate stage counts conserve and match the public store view",
        "tm_retrieval_validation.py:candidate stage transcript",
        (
            "tests.test_tm_retrieval_validation.RetrievalValidationReleaseTests."
            "test_fuzzy_stage_counts_are_conserved_and_faithful",
            "tests.test_tm_retrieval_validation.RetrievalValidationReleaseTests."
            "test_store_candidate_public_view_parity_and_stage_conservation",
            "tests.test_tm_benchmark_contract.CandidateMetadataContractTests."
            "test_stage_order_and_all_counts_must_conserve_continuously",
        ),
        "Observed recall stage counts are continuous and reconcile to the candidate records exposed for scoring.",
    ),
    _row(
        "9.3.METADATA.02",
        AcceptanceDomain.METADATA,
        "Scored and returned counts cannot claim unrecalled or unreturned work",
        "tm_contracts.py:query report count closure",
        (
            "tests.test_tm_retrieval.TMRetrievalServiceCountTests."
            "test_scored_count_and_returned_count_balance",
            "tests.test_tm_benchmark_contract.CandidateMetadataContractTests."
            "test_candidate_evidence_and_final_output_must_reconcile",
            "tests.test_tm_contracts.TMContractTests."
            "test_query_report_requires_closed_resource_metadata",
        ),
        "Every resource metadata count reconciles recall, scoring, accepted results, and the final global slice.",
    ),
    _row(
        "9.3.METADATA.03",
        AcceptanceDomain.METADATA,
        "Validation transcripts and summaries remain body-safe",
        "tm_retrieval_validation.py:body-safe transcript",
        (
            "tests.test_tm_retrieval_validation.RetrievalValidationReleaseTests."
            "test_fuzzy_transcript_is_body_safe_and_shape_closed",
            "tests.test_tm_retrieval_validation.RetrievalValidationReleaseTests."
            "test_manifest_and_summary_are_body_safe",
            "tests.test_tm_retrieval_validation.RetrievalValidationReleaseTests."
            "test_service_transcript_is_body_safe_and_shape_closed",
            "tests.test_tm_retrieval_validation.RetrievalValidationReleaseTests."
            "test_store_transcript_is_body_safe_and_shape_closed",
        ),
        "Evidence publishes identities, counts, digests, versions, and stable codes but no source or target body.",
    ),
)

ACCEPTANCE_MATRIX_ROWS = TASK_9_3_ROWS


def acceptance_matrix_payload(
    rows: tuple[AcceptanceMatrixRow, ...] = ACCEPTANCE_MATRIX_ROWS,
) -> dict[str, object]:
    return {
        "schema_version": ACCEPTANCE_MATRIX_SCHEMA_VERSION,
        "rows": [
            {
                "assertion_contract": row.assertion_contract,
                "claim": row.claim,
                "domain": row.domain.value,
                "production_seam": row.production_seam,
                "row_id": row.row_id,
                "task": row.task,
                "test_ids": list(row.test_ids),
            }
            for row in rows
        ],
    }


def acceptance_matrix_registry_digest(
    rows: tuple[AcceptanceMatrixRow, ...] = ACCEPTANCE_MATRIX_ROWS,
) -> str:
    encoded = json.dumps(
        acceptance_matrix_payload(rows),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def acceptance_matrix_source_paths(
    rows: tuple[AcceptanceMatrixRow, ...] = ACCEPTANCE_MATRIX_ROWS,
) -> tuple[str, ...]:
    paths = {
        "tests/acceptance_matrix_registry.py",
        "tests/test_tm_acceptance_matrix.py",
        "tools/validate_tm_acceptance_matrix.py",
    }
    for row in rows:
        paths.add(row.production_seam.partition(":")[0])
        for test_id in row.test_ids:
            module_name = ".".join(test_id.split(".")[:2])
            paths.add(module_name.replace(".", "/") + ".py")
    return tuple(sorted(paths))


def acceptance_matrix_source_fingerprint(
    registry_digest: str,
    source_files: tuple[tuple[str, str], ...],
) -> str:
    if type(registry_digest) is not str or _SHA256.fullmatch(registry_digest) is None:
        raise ValueError("registry digest must be SHA-256")
    for path, digest in source_files:
        if type(path) is not str or not path or Path(path).is_absolute():
            raise ValueError("source file path must be relative")
        if type(digest) is not str or _SHA256.fullmatch(digest) is None:
            raise ValueError("source file digest must be SHA-256")
    encoded = json.dumps(
        {
            "registry_digest": registry_digest,
            "source_files": [list(item) for item in source_files],
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ACCEPTANCE_MATRIX_ROWS",
    "ACCEPTANCE_MATRIX_SCHEMA_VERSION",
    "AcceptanceDomain",
    "AcceptanceMatrixRow",
    "TASK_9_3_ROWS",
    "acceptance_matrix_payload",
    "acceptance_matrix_registry_digest",
    "acceptance_matrix_source_fingerprint",
    "acceptance_matrix_source_paths",
]
