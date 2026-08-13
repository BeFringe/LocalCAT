"""Closed Requirement-to-evidence registry for Feature 5 Task 9.5.

The registry deliberately separates evidence coverage from release approval.
Every acceptance criterion has an exact evidence reference, while current
benchmark failures remain first-class blockers instead of being hidden by a
complete coverage count.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re


RELEASE_CRITERIA_SCHEMA_VERSION = "tm-release-criteria-v1"

_CRITERION_ID = re.compile(r"[1-9]\.(?:[1-9]|1[0-4])\Z")
_TEST_ID = re.compile(
    r"tests\.test_[a-z0-9_]+\.[A-Za-z0-9_]+\.test_[a-z0-9_]+\Z"
)
_MATRIX_ROW_ID = re.compile(r"9\.[1-4]\.[A-Z_]+\.\d{2}\Z")
_REQUIREMENT_HEADING = re.compile(r"### Requirement ([1-9]): .+")
_CRITERION_LINE = re.compile(r"([1-9]|1[0-4])\. (.+)")

BENCHMARK_CLAIMS = (
    "CANDIDATE_RECALL",
    "ENVIRONMENT",
    "EXACT_P95",
    "FAILURE_REPORT",
    "FUZZY_P95",
    "METRICS",
    "MIGRATION",
    "PEAK_RSS",
)


@dataclass(frozen=True)
class RequirementCriterion:
    """One mechanically parsed acceptance criterion."""

    criterion_id: str
    text: str

    def __post_init__(self) -> None:
        if (
            type(self.criterion_id) is not str
            or _CRITERION_ID.fullmatch(self.criterion_id) is None
        ):
            raise ValueError("criterion id is invalid")
        if type(self.text) is not str or not self.text.strip():
            raise ValueError("criterion text must be non-empty")


@dataclass(frozen=True)
class ReleaseCriterionBinding:
    """One exact criterion-to-evidence association."""

    criterion_id: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.criterion_id) is not str
            or _CRITERION_ID.fullmatch(self.criterion_id) is None
        ):
            raise ValueError("binding criterion id is invalid")
        if type(self.evidence_refs) is not tuple or not self.evidence_refs:
            raise ValueError("criterion binding requires evidence")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("criterion evidence references must be unique")
        for evidence_ref in self.evidence_refs:
            _validate_evidence_ref(evidence_ref)


def _validate_evidence_ref(evidence_ref: str) -> None:
    if type(evidence_ref) is not str:
        raise TypeError("evidence reference must be a string")
    kind, separator, value = evidence_ref.partition(":")
    if not separator:
        raise ValueError("evidence reference requires a kind")
    if kind == "acceptance":
        if _MATRIX_ROW_ID.fullmatch(value) is None or not value.startswith(
            ("9.3.", "9.4.")
        ):
            raise ValueError("acceptance evidence row is invalid")
    elif kind == "fault":
        if _MATRIX_ROW_ID.fullmatch(value) is None or not value.startswith(
            ("9.1.", "9.2.")
        ):
            raise ValueError("fault evidence row is invalid")
    elif kind == "test":
        if _TEST_ID.fullmatch(value) is None:
            raise ValueError("direct unittest evidence id is invalid")
    elif kind == "benchmark":
        if value not in BENCHMARK_CLAIMS:
            raise ValueError("benchmark evidence claim is invalid")
    else:
        raise ValueError("evidence reference kind is invalid")


def parse_requirement_criteria(raw: str) -> tuple[RequirementCriterion, ...]:
    """Parse only numbered Acceptance Criteria from approved requirements."""

    if type(raw) is not str:
        raise TypeError("requirements source must be a string")
    active_requirement: int | None = None
    in_acceptance_criteria = False
    criteria: list[RequirementCriterion] = []
    for line in raw.splitlines():
        heading = _REQUIREMENT_HEADING.fullmatch(line)
        if heading is not None:
            active_requirement = int(heading.group(1))
            in_acceptance_criteria = False
            continue
        if line.startswith("### "):
            active_requirement = None
            in_acceptance_criteria = False
            continue
        if line == "#### Acceptance Criteria":
            if active_requirement is None:
                raise ValueError("acceptance criteria have no requirement")
            in_acceptance_criteria = True
            continue
        if line.startswith("#### "):
            in_acceptance_criteria = False
            continue
        if not in_acceptance_criteria or active_requirement is None:
            continue
        criterion = _CRITERION_LINE.fullmatch(line)
        if criterion is None:
            continue
        criteria.append(
            RequirementCriterion(
                criterion_id=f"{active_requirement}.{criterion.group(1)}",
                text=criterion.group(2),
            )
        )
    identifiers = tuple(item.criterion_id for item in criteria)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("requirements contain duplicate acceptance criteria")
    return tuple(criteria)


def _binding(criterion_id: str, *evidence_refs: str) -> ReleaseCriterionBinding:
    return ReleaseCriterionBinding(criterion_id, tuple(evidence_refs))


RELEASE_CRITERIA_BINDINGS: tuple[ReleaseCriterionBinding, ...] = (
    _binding("1.1", "acceptance:9.4.COMPAT.01"),
    _binding("1.2", "acceptance:9.4.COMPAT.01"),
    _binding("1.3", "acceptance:9.3.CONTEXT.02", "acceptance:9.3.NONIMPERSONATE.03"),
    _binding("1.4", "acceptance:9.4.COMPAT.02"),
    _binding("1.5", "acceptance:9.4.COMPAT.02"),
    _binding("1.6", "acceptance:9.4.PRIORITY.01", "acceptance:9.4.EXCEL.01"),
    _binding("1.7", "acceptance:9.4.PRIORITY.01"),
    _binding("1.8", "acceptance:9.4.EXCEL.01"),
    _binding("1.9", "acceptance:9.4.COMPAT.02", "fault:9.2.IMPORT_REBUILD.01"),
    _binding(
        "2.1",
        "test:tests.test_tm_migration.TMMigrationPreflightTests.test_preflight_streams_counts_digest_and_safe_diagnostics",
    ),
    _binding(
        "2.2",
        "test:tests.test_tm_explicit_import_rebuild.ExplicitImportRebuildSuccessTests.test_import_and_rebuild_replace_the_active_canonical",
    ),
    _binding("2.3", "acceptance:9.4.COMPAT.01", "fault:9.2.IMPORT_REBUILD.01"),
    _binding("2.4", "fault:9.1.PRESERVATION.01", "fault:9.1.WRITE_FAULT.04"),
    _binding("2.5", "fault:9.1.CORRUPT_INPUT.01", "fault:9.1.WRITE_FAULT.04"),
    _binding("2.6", "acceptance:9.4.COMPAT.04"),
    _binding(
        "2.7",
        "test:tests.test_tm_export.TMExportSuccessTests.test_export_publishes_complete_deterministic_snapshot",
    ),
    _binding("2.8", "fault:9.2.EXPORT_CRASH.01", "fault:9.2.FOREIGN_INODE.01"),
    _binding("2.9", "fault:9.1.PHASE_CRASH.02", "fault:9.1.PHASE_CRASH.03"),
    _binding("2.10", "fault:9.1.LEASE.02", "fault:9.1.PRESERVATION.03"),
    _binding("2.11", "fault:9.1.PRESERVATION.02", "fault:9.1.PHASE_CRASH.02"),
    _binding("2.12", "fault:9.1.PHASE_CRASH.01", "acceptance:9.4.COMPAT.01"),
    _binding("2.13", "fault:9.2.EXTERNAL_CHANGE.01", "fault:9.1.PRESERVATION.01"),
    _binding(
        "3.1",
        "test:tests.test_tm_sqlite_store.SQLiteTMStoreTests.test_local_append_preserves_variants_and_raw_exact_winner",
        "acceptance:9.3.CONTEXT.02",
    ),
    _binding(
        "3.2",
        "test:tests.test_tm_contracts.TMContractTests.test_record_preserves_raw_context_origin_and_provenance",
    ),
    _binding(
        "3.3",
        "test:tests.test_tm_contracts.TMContractTests.test_record_preserves_raw_context_origin_and_provenance",
        "acceptance:9.3.CONTEXT.01",
    ),
    _binding("3.4", "acceptance:9.3.CONTEXT.02"),
    _binding("3.5", "acceptance:9.3.CONTEXT.01", "acceptance:9.3.CONTEXT.02"),
    _binding("3.6", "acceptance:9.3.NONIMPERSONATE.01", "acceptance:9.3.CONTEXT.01"),
    _binding("3.7", "acceptance:9.4.ISOLATION.01", "acceptance:9.3.METADATA.02"),
    _binding("4.1", "acceptance:9.3.SERVICE.01"),
    _binding("4.2", "acceptance:9.4.PRIORITY.02", "acceptance:9.3.SERVICE.01"),
    _binding(
        "4.3",
        "test:tests.test_tm_contracts.TMContractTests.test_resource_query_result_and_partial_failure_round_trip",
        "acceptance:9.3.METADATA.02",
    ),
    _binding(
        "4.4",
        "test:tests.test_tm_retrieval.FuzzyScoringTests.test_threshold_boundary_equality_is_accepted_and_below_is_excluded",
    ),
    _binding("4.5", "acceptance:9.3.SERVICE.01"),
    _binding("4.6", "acceptance:9.3.CANDIDATE.02", "acceptance:9.3.SERVICE.03"),
    _binding("4.7", "acceptance:9.3.SERVICE.02"),
    _binding(
        "5.1",
        "test:tests.test_tm_contracts.TMContractTests.test_fuzzy_result_keeps_both_sources_and_matching_score",
    ),
    _binding(
        "5.2",
        "test:tests.test_tm_contracts.TMContractTests.test_fuzzy_result_keeps_both_sources_and_matching_score",
    ),
    _binding(
        "5.3",
        "test:tests.test_tm_retrieval.FuzzyScoringTests.test_order_is_final_similarity_desc_then_record_id_desc",
    ),
    _binding(
        "5.4",
        "test:tests.test_tm_retrieval.FuzzyScoringTests.test_repeated_scoring_is_deterministic_without_mutating_inputs",
    ),
    _binding(
        "5.5",
        "test:tests.test_editor_controller_writes.EditorControllerWritesTest.test_apply_tm_and_insert_term_never_auto_confirm",
        "acceptance:9.3.NONIMPERSONATE.01",
    ),
    _binding(
        "5.6",
        "test:tests.test_editor_controller_writes.EditorControllerWritesTest.test_apply_tm_and_insert_term_never_auto_confirm",
    ),
    _binding("5.7", "acceptance:9.3.NONIMPERSONATE.03"),
    _binding("6.1", "acceptance:9.3.MATCHER.03"),
    _binding("6.2", "acceptance:9.3.MATCHER.03"),
    _binding("6.3", "acceptance:9.3.MATCHER.03"),
    _binding("6.4", "acceptance:9.3.MATCHER.03"),
    _binding("6.5", "acceptance:9.3.MATCHER.03"),
    _binding("6.6", "acceptance:9.3.MATCHER.03"),
    _binding("6.7", "acceptance:9.3.MATCHER.03", "acceptance:9.3.MATCHER.04"),
    _binding("6.8", "acceptance:9.3.MATCHER.03"),
    _binding("6.9", "acceptance:9.3.NONIMPERSONATE.01", "acceptance:9.4.ARCH.02"),
    _binding("6.10", "acceptance:9.3.NONIMPERSONATE.01", "acceptance:9.4.ARCH.01"),
    _binding("7.1", "acceptance:9.4.PRIVACY.01"),
    _binding("7.2", "acceptance:9.4.PRIVACY.01"),
    _binding("7.3", "acceptance:9.4.PRIVACY.01"),
    _binding("7.4", "fault:9.1.CORRUPT_INPUT.03", "acceptance:9.3.SERVICE.02"),
    _binding("7.5", "fault:9.1.PRESERVATION.02", "fault:9.1.PHASE_CRASH.02"),
    _binding("7.6", "fault:9.1.WRITE_FAULT.01", "fault:9.1.BUSY.01"),
    _binding("7.7", "acceptance:9.4.ISOLATION.01", "acceptance:9.3.SERVICE.02"),
    _binding(
        "7.8",
        "test:tests.test_tm_engine_compat.SourceDivergedTests.test_diverged_canonical_query_and_save_continue_and_keep_jsonl",
        "fault:9.2.EXTERNAL_CHANGE.01",
    ),
    _binding(
        "7.9",
        "test:tests.test_tm_engine_compat.SourceDivergedTests.test_diverged_canonical_query_and_save_continue_and_keep_jsonl",
    ),
    _binding(
        "7.10",
        "test:tests.test_tm_engine_compat.SourceDivergedTests.test_diverged_canonical_query_and_save_continue_and_keep_jsonl",
        "fault:9.2.EXTERNAL_CHANGE.02",
    ),
    _binding(
        "7.11",
        "test:tests.test_tm_engine_compat.SourceDivergedTests.test_diverged_canonical_query_and_save_continue_and_keep_jsonl",
        "fault:9.1.PRESERVATION.01",
    ),
    _binding("7.12", "fault:9.2.IMPORT_REBUILD.01", "fault:9.2.IMPORT_REBUILD.03"),
    _binding("7.13", "fault:9.2.IMPORT_REBUILD.02", "fault:9.1.PRESERVATION.01"),
    _binding("7.14", "fault:9.2.MISMATCH.01", "fault:9.2.MISMATCH.02"),
    _binding("8.1", "benchmark:EXACT_P95"),
    _binding("8.2", "benchmark:FUZZY_P95"),
    _binding("8.3", "benchmark:MIGRATION"),
    _binding("8.4", "benchmark:PEAK_RSS"),
    _binding("8.5", "benchmark:ENVIRONMENT"),
    _binding("8.6", "benchmark:METRICS"),
    _binding("8.7", "benchmark:FAILURE_REPORT"),
    _binding("9.1", "acceptance:9.3.MATCHER.01"),
    _binding("9.2", "acceptance:9.3.MATCHER.01", "acceptance:9.3.MATCHER.03"),
    _binding("9.3", "acceptance:9.3.MATCHER.02"),
    _binding("9.4", "acceptance:9.3.MATCHER.02"),
    _binding("9.5", "acceptance:9.3.MATCHER.01", "acceptance:9.3.MATCHER.03"),
    _binding("9.6", "acceptance:9.3.MATCHER.03"),
    _binding("9.7", "acceptance:9.3.MATCHER.03", "acceptance:9.3.MATCHER.05"),
    _binding("9.8", "acceptance:9.3.MATCHER.04"),
    _binding("9.9", "acceptance:9.3.MATCHER.01", "acceptance:9.3.METADATA.03"),
    _binding("9.10", "acceptance:9.3.NONIMPERSONATE.01", "acceptance:9.4.ARCH.02"),
    _binding("9.11", "acceptance:9.3.MATCHER.03", "acceptance:9.3.MATCHER.05"),
    _binding("9.12", "acceptance:9.3.NONIMPERSONATE.01", "acceptance:9.4.ARCH.02"),
)


def release_criteria_registry_digest() -> str:
    payload = [
        {
            "criterion_id": binding.criterion_id,
            "evidence_refs": list(binding.evidence_refs),
        }
        for binding in RELEASE_CRITERIA_BINDINGS
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "BENCHMARK_CLAIMS",
    "RELEASE_CRITERIA_BINDINGS",
    "RELEASE_CRITERIA_SCHEMA_VERSION",
    "ReleaseCriterionBinding",
    "RequirementCriterion",
    "parse_requirement_criteria",
    "release_criteria_registry_digest",
]
