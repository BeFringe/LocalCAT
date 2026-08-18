"""Retrieval gate C/D evaluator and atomic capability publisher.

Task 7.4 slice: the provider-independent sole decision and publication
boundary for retrieval capability.  It consumes only frozen ``tm_contracts``
values and immutable validation evidence, and must never import store,
candidate, retrieval, migration or benchmark runner modules.

The evaluator is the only decision point: a manifest's self-reported
``passed`` flag never grants availability by itself.  Every gate decision
also closes the approved identity/version/digest/path facts pinned by the
expectation and the recomputable report digests.  Missing, invalid,
failed or expired evidence fails closed, and an open gate may later
downgrade.  Reasons and summaries never carry source, target or query
bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from threading import Lock
from types import MemberDescriptorType
from typing import Callable, TypeVar, cast, final

from tm_contracts import (
    BenchmarkExecutionPath,
    BenchmarkReport,
    benchmark_contract_digest,
    benchmark_environment_digest,
)


RETRIEVAL_SEMANTICS_VERSION = "retrieval-v1"
RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION = (
    "retrieval-capability-evidence-v1"
)
RETRIEVAL_CAPABILITY_SUMMARY_VERSION = "retrieval-capability-summary-v1"

_CONTEXT_NAMESPACE = "RETRIEVAL.CONTEXT"
_FUZZY_CORRECTNESS_NAMESPACE = "RETRIEVAL.FUZZY_CORRECTNESS"
_FUZZY_BENCHMARK_NAMESPACE = "RETRIEVAL.FUZZY_BENCHMARK"

RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE = (
    f"{_CONTEXT_NAMESPACE}_IDENTITY_INVALID"
)
RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE = (
    f"{_CONTEXT_NAMESPACE}_EVIDENCE_MISSING"
)
RETRIEVAL_CONTEXT_EVIDENCE_FAILED_CODE = (
    f"{_CONTEXT_NAMESPACE}_EVIDENCE_FAILED"
)
RETRIEVAL_CONTEXT_EVIDENCE_EXPIRED_CODE = (
    f"{_CONTEXT_NAMESPACE}_EVIDENCE_EXPIRED"
)
RETRIEVAL_FUZZY_CORRECTNESS_IDENTITY_INVALID_CODE = (
    f"{_FUZZY_CORRECTNESS_NAMESPACE}_IDENTITY_INVALID"
)
RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE = (
    f"{_FUZZY_CORRECTNESS_NAMESPACE}_EVIDENCE_MISSING"
)
RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE = (
    f"{_FUZZY_CORRECTNESS_NAMESPACE}_EVIDENCE_FAILED"
)
RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_EXPIRED_CODE = (
    f"{_FUZZY_CORRECTNESS_NAMESPACE}_EVIDENCE_EXPIRED"
)
RETRIEVAL_FUZZY_BENCHMARK_IDENTITY_INVALID_CODE = (
    f"{_FUZZY_BENCHMARK_NAMESPACE}_IDENTITY_INVALID"
)
RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE = (
    f"{_FUZZY_BENCHMARK_NAMESPACE}_EVIDENCE_MISSING"
)
RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE = (
    f"{_FUZZY_BENCHMARK_NAMESPACE}_EVIDENCE_FAILED"
)
RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_EXPIRED_CODE = (
    f"{_FUZZY_BENCHMARK_NAMESPACE}_EVIDENCE_EXPIRED"
)

_SUPPORTED_PATHS = frozenset(
    path.value for path in BenchmarkExecutionPath
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STRICT_UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z"
)
_STABLE_CODE = re.compile(r"[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\Z")


def _require_identity(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a built-in string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _require_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a lowercase SHA-256 digest"
        )
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a built-in bool")
    return value


def _is_stable_code(value: object) -> bool:
    return (
        type(value) is str
        and value != ""
        and _STABLE_CODE.fullmatch(value) is not None
    )


def _require_stable_code(value: object, field_name: str) -> str:
    if not _is_stable_code(value):
        raise ValueError(f"{field_name} must be a stable diagnostic code")
    return cast(str, value)


def _parse_evidence_utc(value: str) -> datetime:
    if _STRICT_UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("evidence timestamp must be strict UTC")
    return datetime.strptime(
        value,
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=timezone.utc)


def _validate_validity_window(
    *,
    generated_at_utc: str,
    valid_until_utc: str,
) -> None:
    generated = _parse_evidence_utc(generated_at_utc)
    valid_until = _parse_evidence_utc(valid_until_utc)
    if not generated < valid_until:
        raise ValueError("evidence valid_until must follow generated_at")


def _is_active_window(
    *,
    generated_at_utc: str,
    valid_until_utc: str,
    evaluated_at_utc: datetime,
) -> bool:
    generated = _parse_evidence_utc(generated_at_utc)
    valid_until = _parse_evidence_utc(valid_until_utc)
    return generated <= evaluated_at_utc < valid_until


def _require_utc_instant(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(
            "evaluated_at_utc must be a timezone-aware UTC datetime"
        )
    return value


# --- capability values ------------------------------------------------------


@dataclass(frozen=True)
class RetrievalContextDecision:
    """One immutable CONTEXT sub-gate decision from Gate C evidence."""

    available: bool
    unavailable_code: str | None

    def __post_init__(self) -> None:
        available = _require_bool(self.available, "context availability")
        if available:
            if self.unavailable_code is not None:
                raise ValueError(
                    "available context must not carry an unavailable code"
                )
        else:
            if self.unavailable_code is None:
                raise ValueError(
                    "closed context requires an unavailable code"
                )
            _require_stable_code(
                self.unavailable_code,
                "context unavailable code",
            )


@dataclass(frozen=True)
class RetrievalFuzzyCoreDecision:
    """One immutable fuzzy-core correctness decision from Gate C evidence."""

    available: bool
    unavailable_code: str | None

    def __post_init__(self) -> None:
        available = _require_bool(
            self.available,
            "fuzzy-core availability",
        )
        if available:
            if self.unavailable_code is not None:
                raise ValueError(
                    "available fuzzy-core must not carry an unavailable code"
                )
        else:
            if self.unavailable_code is None:
                raise ValueError(
                    "closed fuzzy-core requires an unavailable code"
                )
            _require_stable_code(
                self.unavailable_code,
                "fuzzy-core unavailable code",
            )


@dataclass(frozen=True)
class RetrievalFuzzyPathDecision:
    """One immutable Gate D decision for one benchmark execution path."""

    path: str
    available: bool
    unavailable_code: str | None

    def __post_init__(self) -> None:
        path = _require_identity(self.path, "fuzzy path")
        if path not in _SUPPORTED_PATHS:
            raise ValueError("fuzzy path is unsupported")
        available = _require_bool(
            self.available,
            "fuzzy path availability",
        )
        if available:
            if self.unavailable_code is not None:
                raise ValueError(
                    "available fuzzy path must not carry an unavailable code"
                )
        else:
            if self.unavailable_code is None:
                raise ValueError(
                    "closed fuzzy path requires an unavailable code"
                )
            _require_stable_code(
                self.unavailable_code,
                "fuzzy path unavailable code",
            )


@dataclass(frozen=True)
class RetrievalCapabilityEvidenceSummary:
    """Opaque evidence summary: version, digest, time and stable codes only."""

    summary_version: str
    evidence_digest: str
    evaluated_at_utc: datetime
    unavailable_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.summary_version != RETRIEVAL_CAPABILITY_SUMMARY_VERSION:
            raise ValueError(
                "unsupported retrieval capability summary version"
            )
        _require_digest(self.evidence_digest, "evidence digest")
        _require_utc_instant(self.evaluated_at_utc)
        codes = _require_exact_tuple(
            self.unavailable_codes,
            "unavailable codes",
        )
        for code in codes:
            _require_stable_code(code, "unavailable code")
        validated_codes = cast(tuple[str, ...], codes)
        if len(validated_codes) != len(set(validated_codes)):
            raise ValueError("unavailable codes must be unique")
        if validated_codes != tuple(sorted(validated_codes)):
            raise ValueError("unavailable codes must use stable sorted order")


@dataclass(frozen=True)
class RetrievalCapabilitySnapshot:
    """Atomic immutable snapshot of every retrieval sub-gate decision."""

    semantics_version: str
    context: RetrievalContextDecision
    fuzzy_core: RetrievalFuzzyCoreDecision
    fts5_trigram: RetrievalFuzzyPathDecision
    gram_fallback: RetrievalFuzzyPathDecision
    summary: RetrievalCapabilityEvidenceSummary

    def __post_init__(self) -> None:
        _require_identity(self.semantics_version, "retrieval semantics version")
        if type(self.context) is not RetrievalContextDecision:
            raise TypeError("snapshot context must be RetrievalContextDecision")
        if type(self.fuzzy_core) is not RetrievalFuzzyCoreDecision:
            raise TypeError(
                "snapshot fuzzy_core must be RetrievalFuzzyCoreDecision"
            )
        if type(self.fts5_trigram) is not RetrievalFuzzyPathDecision:
            raise TypeError(
                "snapshot fts5_trigram must be RetrievalFuzzyPathDecision"
            )
        if type(self.gram_fallback) is not RetrievalFuzzyPathDecision:
            raise TypeError(
                "snapshot gram_fallback must be RetrievalFuzzyPathDecision"
            )
        if self.fts5_trigram.path != "FTS5_TRIGRAM":
            raise ValueError("fts5_trigram decision must use FTS5_TRIGRAM")
        if self.gram_fallback.path != "GRAM_FALLBACK":
            raise ValueError("gram_fallback decision must use GRAM_FALLBACK")
        if type(self.summary) is not RetrievalCapabilityEvidenceSummary:
            raise TypeError(
                "snapshot summary must be RetrievalCapabilityEvidenceSummary"
            )
        codes = tuple(
            code
            for decision in (
                self.context,
                self.fuzzy_core,
                self.fts5_trigram,
                self.gram_fallback,
            )
            for code in (decision.unavailable_code,)
            if code is not None
        )
        if tuple(sorted(set(codes))) != self.summary.unavailable_codes:
            raise ValueError(
                "snapshot summary must aggregate every closed sub-gate code"
            )

    def fuzzy_available_for(self, path: str) -> tuple[bool, str | None]:
        """Return (available, code) for one intended execution path."""

        if type(path) is not str or path not in _SUPPORTED_PATHS:
            raise ValueError("fuzzy path is unsupported")
        if not self.fuzzy_core.available:
            return False, self.fuzzy_core.unavailable_code
        path_decision = (
            self.fts5_trigram
            if path == "FTS5_TRIGRAM"
            else self.gram_fallback
        )
        if not path_decision.available:
            return False, path_decision.unavailable_code
        return True, None


def _require_exact_tuple(
    value: object,
    field_name: str,
) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a built-in tuple")
    return cast(tuple[object, ...], value)


def _require_exact_type(
    value: object,
    expected_type: type[object],
    field_name: str,
) -> None:
    if type(value) is not expected_type:
        raise TypeError(
            f"{field_name} must be an exact {expected_type.__name__}"
        )


# --- evidence and expectation ----------------------------------------------


@dataclass(frozen=True)
class RetrievalCorrectnessCohortEvidence:
    """One immutable Gate C cohort evidence row for context or fuzzy-core."""

    cohort_id: str
    cohort_digest: str
    passed: bool
    generated_at_utc: str
    valid_until_utc: str

    def __post_init__(self) -> None:
        _require_identity(self.cohort_id, "cohort id")
        _require_digest(self.cohort_digest, "cohort digest")
        _require_bool(self.passed, "cohort passed")
        _validate_validity_window(
            generated_at_utc=self.generated_at_utc,
            valid_until_utc=self.valid_until_utc,
        )


@dataclass(frozen=True)
class RetrievalBenchmarkEvidence:
    """One immutable Gate D benchmark report with its validity window."""

    report: BenchmarkReport
    generated_at_utc: str
    valid_until_utc: str

    def __post_init__(self) -> None:
        if type(self.report) is not BenchmarkReport:
            raise TypeError("benchmark evidence must contain BenchmarkReport")
        _validate_validity_window(
            generated_at_utc=self.generated_at_utc,
            valid_until_utc=self.valid_until_utc,
        )


@dataclass(frozen=True)
class RetrievalCapabilityManifest:
    """Frozen, caller-immutable Gate C/D evidence for one evaluation."""

    evidence_schema_version: str
    retrieval_artifact_digest: str
    retrieval_build_digest: str
    semantics_version: str
    fixture_digest: str
    evaluator_digest: str
    generated_at_utc: str
    valid_until_utc: str
    context_cohorts: tuple[RetrievalCorrectnessCohortEvidence, ...]
    fuzzy_core_cohorts: tuple[RetrievalCorrectnessCohortEvidence, ...]
    fts5_trigram_benchmark: RetrievalBenchmarkEvidence | None
    gram_fallback_benchmark: RetrievalBenchmarkEvidence | None

    def __post_init__(self) -> None:
        if (
            self.evidence_schema_version
            != RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION
        ):
            raise ValueError(
                "unsupported retrieval capability evidence schema"
            )
        _require_digest(
            self.retrieval_artifact_digest,
            "retrieval artifact digest",
        )
        _require_digest(
            self.retrieval_build_digest,
            "retrieval build digest",
        )
        _require_identity(
            self.semantics_version,
            "retrieval semantics version",
        )
        _require_digest(self.fixture_digest, "fixture digest")
        _require_digest(self.evaluator_digest, "evaluator digest")
        _validate_validity_window(
            generated_at_utc=self.generated_at_utc,
            valid_until_utc=self.valid_until_utc,
        )
        context = _require_evidence_tuple(
            self.context_cohorts,
            "context cohorts",
        )
        fuzzy_core = _require_evidence_tuple(
            self.fuzzy_core_cohorts,
            "fuzzy-core cohorts",
        )
        if {item.cohort_id for item in context}.intersection(
            item.cohort_id for item in fuzzy_core
        ):
            raise ValueError(
                "context and fuzzy-core cohort ids must be disjoint"
            )
        if self.fts5_trigram_benchmark is not None:
            _require_exact_type(
                self.fts5_trigram_benchmark,
                RetrievalBenchmarkEvidence,
                "fts5_trigram benchmark evidence",
            )
            if (
                self.fts5_trigram_benchmark.report.execution_path
                is not BenchmarkExecutionPath.FTS5_TRIGRAM
            ):
                raise ValueError(
                    "fts5_trigram evidence must execute FTS5_TRIGRAM"
                )
        if self.gram_fallback_benchmark is not None:
            _require_exact_type(
                self.gram_fallback_benchmark,
                RetrievalBenchmarkEvidence,
                "gram_fallback benchmark evidence",
            )
            if (
                self.gram_fallback_benchmark.report.execution_path
                is not BenchmarkExecutionPath.GRAM_FALLBACK
            ):
                raise ValueError(
                    "gram_fallback evidence must execute GRAM_FALLBACK"
                )


def _require_evidence_tuple(
    value: object,
    field_name: str,
) -> tuple[RetrievalCorrectnessCohortEvidence, ...]:
    items = _require_exact_tuple(value, field_name)
    if not items:
        raise ValueError(f"{field_name} must not be empty")
    validated: list[RetrievalCorrectnessCohortEvidence] = []
    for item in items:
        _require_exact_type(
            item,
            RetrievalCorrectnessCohortEvidence,
            field_name,
        )
        validated.append(cast(RetrievalCorrectnessCohortEvidence, item))
    result = tuple(validated)
    ids = tuple(item.cohort_id for item in result)
    if len(ids) != len(set(ids)):
        raise ValueError(f"{field_name} cohort ids must be unique")
    if ids != tuple(sorted(ids)):
        raise ValueError(f"{field_name} must use stable cohort id order")
    return result


@dataclass(frozen=True)
class RetrievalCohortExpectation:
    """Expected identity and digest for one approved Gate C cohort."""

    cohort_id: str
    cohort_digest: str

    def __post_init__(self) -> None:
        _require_identity(self.cohort_id, "cohort id")
        _require_digest(self.cohort_digest, "cohort digest")


@dataclass(frozen=True)
class RetrievalBenchmarkExpectation:
    """Approved Gate D path and frozen benchmark contract digest."""

    path: str
    contract_digest: str

    def __post_init__(self) -> None:
        path = _require_identity(self.path, "benchmark path")
        if path not in _SUPPORTED_PATHS:
            raise ValueError("benchmark path is unsupported")
        _require_digest(self.contract_digest, "benchmark contract digest")


@dataclass(frozen=True)
class RetrievalCapabilityExpectation:
    """Frozen approved identity/digest facts used for every evaluation."""

    evidence_schema_version: str
    retrieval_artifact_digest: str
    retrieval_build_digest: str
    semantics_version: str
    fixture_digest: str
    evaluator_digest: str
    context_cohorts: tuple[RetrievalCohortExpectation, ...]
    fuzzy_core_cohorts: tuple[RetrievalCohortExpectation, ...]
    fts5_trigram: RetrievalBenchmarkExpectation
    gram_fallback: RetrievalBenchmarkExpectation

    def __post_init__(self) -> None:
        if (
            self.evidence_schema_version
            != RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION
        ):
            raise ValueError(
                "unsupported retrieval capability evidence schema"
            )
        _require_digest(
            self.retrieval_artifact_digest,
            "retrieval artifact digest",
        )
        _require_digest(
            self.retrieval_build_digest,
            "retrieval build digest",
        )
        _require_identity(
            self.semantics_version,
            "retrieval semantics version",
        )
        _require_digest(self.fixture_digest, "fixture digest")
        _require_digest(self.evaluator_digest, "evaluator digest")
        context = _require_expectation_tuple(
            self.context_cohorts,
            "context cohort expectations",
        )
        fuzzy_core = _require_expectation_tuple(
            self.fuzzy_core_cohorts,
            "fuzzy-core cohort expectations",
        )
        if {item.cohort_id for item in context}.intersection(
            item.cohort_id for item in fuzzy_core
        ):
            raise ValueError(
                "context and fuzzy-core cohort ids must be disjoint"
            )
        _require_exact_type(
            self.fts5_trigram,
            RetrievalBenchmarkExpectation,
            "fts5_trigram benchmark expectation",
        )
        _require_exact_type(
            self.gram_fallback,
            RetrievalBenchmarkExpectation,
            "gram_fallback benchmark expectation",
        )
        if self.fts5_trigram.path != "FTS5_TRIGRAM":
            raise ValueError(
                "fts5_trigram expectation must use FTS5_TRIGRAM"
            )
        if self.gram_fallback.path != "GRAM_FALLBACK":
            raise ValueError(
                "gram_fallback expectation must use GRAM_FALLBACK"
            )


def _require_expectation_tuple(
    value: object,
    field_name: str,
) -> tuple[RetrievalCohortExpectation, ...]:
    items = _require_exact_tuple(value, field_name)
    if not items:
        raise ValueError(f"{field_name} must not be empty")
    validated: list[RetrievalCohortExpectation] = []
    for item in items:
        _require_exact_type(
            item,
            RetrievalCohortExpectation,
            field_name,
        )
        validated.append(cast(RetrievalCohortExpectation, item))
    result = tuple(validated)
    ids = tuple(item.cohort_id for item in result)
    if len(ids) != len(set(ids)):
        raise ValueError(f"{field_name} cohort ids must be unique")
    if ids != tuple(sorted(ids)):
        raise ValueError(f"{field_name} must use stable cohort id order")
    return result


# --- canonical payloads -----------------------------------------------------


def _cohort_evidence_payload(
    evidence: RetrievalCorrectnessCohortEvidence,
) -> dict[str, object]:
    return {
        "cohort_digest": evidence.cohort_digest,
        "cohort_id": evidence.cohort_id,
        "generated_at_utc": evidence.generated_at_utc,
        "passed": evidence.passed,
        "valid_until_utc": evidence.valid_until_utc,
    }


def _benchmark_report_payload(report: BenchmarkReport) -> dict[str, object]:
    return {
        "candidate_recall": report.candidate_recall,
        "contract_digest": report.contract_digest,
        "corpus_composition_digest": report.corpus_composition_digest,
        "corpus_composition_version": report.corpus_composition_version,
        "corpus_digest": report.corpus_digest,
        "environment_digest": report.environment_digest,
        "exact_cohort_digest": report.exact_cohort_digest,
        "exact_max_ms": report.exact_max_ms,
        "exact_p50_ms": report.exact_p50_ms,
        "exact_p95_ms": report.exact_p95_ms,
        "exact_sample_count": report.exact_sample_count,
        "execution_path": report.execution_path.value,
        "failed_gates": list(report.failed_gates),
        "fuzzy_cohort_digest": report.fuzzy_cohort_digest,
        "fuzzy_max_ms": report.fuzzy_top10_max_ms,
        "fuzzy_p50_ms": report.fuzzy_top10_p50_ms,
        "fuzzy_p95_ms": report.fuzzy_top10_p95_ms,
        "fuzzy_sample_count": report.fuzzy_sample_count,
        "migration_seconds": report.migration_seconds,
        "oracle_query_count": report.oracle_query_count,
        "oracle_subset_digest": report.oracle_subset_digest,
        "path_config_digest": report.path_config_digest,
        "peak_rss_mib": report.peak_rss_mib,
        "percentile_method": report.percentile_method,
        "scorer_config_digest": report.scorer_config_digest,
    }


def _benchmark_evidence_payload(
    evidence: RetrievalBenchmarkEvidence,
) -> dict[str, object]:
    return {
        "generated_at_utc": evidence.generated_at_utc,
        "report": _benchmark_report_payload(evidence.report),
        "valid_until_utc": evidence.valid_until_utc,
    }


def _manifest_payload(
    manifest: RetrievalCapabilityManifest,
) -> dict[str, object]:
    return {
        "context_cohorts": [
            _cohort_evidence_payload(evidence)
            for evidence in manifest.context_cohorts
        ],
        "evidence_schema_version": manifest.evidence_schema_version,
        "evaluator_digest": manifest.evaluator_digest,
        "fixture_digest": manifest.fixture_digest,
        "fts5_trigram_benchmark": (
            None
            if manifest.fts5_trigram_benchmark is None
            else _benchmark_evidence_payload(
                manifest.fts5_trigram_benchmark
            )
        ),
        "fuzzy_core_cohorts": [
            _cohort_evidence_payload(evidence)
            for evidence in manifest.fuzzy_core_cohorts
        ],
        "generated_at_utc": manifest.generated_at_utc,
        "gram_fallback_benchmark": (
            None
            if manifest.gram_fallback_benchmark is None
            else _benchmark_evidence_payload(
                manifest.gram_fallback_benchmark
            )
        ),
        "retrieval_artifact_digest": manifest.retrieval_artifact_digest,
        "retrieval_build_digest": manifest.retrieval_build_digest,
        "semantics_version": manifest.semantics_version,
        "valid_until_utc": manifest.valid_until_utc,
    }


def _canonical_digest(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _manifest_payload_json(
    manifest: RetrievalCapabilityManifest,
) -> str:
    return json.dumps(
        _manifest_payload(manifest),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _expectation_payload(
    expectation: RetrievalCapabilityExpectation,
) -> dict[str, object]:
    def encode_cohorts(
        cohorts: tuple[RetrievalCohortExpectation, ...],
    ) -> list[dict[str, str]]:
        return [
            {
                "cohort_digest": cohort.cohort_digest,
                "cohort_id": cohort.cohort_id,
            }
            for cohort in cohorts
        ]

    return {
        "context_cohorts": encode_cohorts(expectation.context_cohorts),
        "evidence_schema_version": expectation.evidence_schema_version,
        "evaluator_digest": expectation.evaluator_digest,
        "fixture_digest": expectation.fixture_digest,
        "fts5_trigram": {
            "contract_digest": expectation.fts5_trigram.contract_digest,
            "path": expectation.fts5_trigram.path,
        },
        "fuzzy_core_cohorts": encode_cohorts(
            expectation.fuzzy_core_cohorts
        ),
        "gram_fallback": {
            "contract_digest": expectation.gram_fallback.contract_digest,
            "path": expectation.gram_fallback.path,
        },
        "retrieval_artifact_digest": expectation.retrieval_artifact_digest,
        "retrieval_build_digest": expectation.retrieval_build_digest,
        "semantics_version": expectation.semantics_version,
    }


def _expectation_digest(
    expectation: RetrievalCapabilityExpectation,
) -> str:
    return _canonical_digest(_expectation_payload(expectation))


def _clone_expectation(
    expectation: RetrievalCapabilityExpectation,
) -> RetrievalCapabilityExpectation:
    """Detach publisher authority from caller-owned live object aliases."""

    def clone_cohorts(
        cohorts: tuple[RetrievalCohortExpectation, ...],
    ) -> tuple[RetrievalCohortExpectation, ...]:
        return tuple(
            RetrievalCohortExpectation(
                cohort_id=cohort.cohort_id,
                cohort_digest=cohort.cohort_digest,
            )
            for cohort in cohorts
        )

    return RetrievalCapabilityExpectation(
        evidence_schema_version=expectation.evidence_schema_version,
        retrieval_artifact_digest=expectation.retrieval_artifact_digest,
        retrieval_build_digest=expectation.retrieval_build_digest,
        semantics_version=expectation.semantics_version,
        fixture_digest=expectation.fixture_digest,
        evaluator_digest=expectation.evaluator_digest,
        context_cohorts=clone_cohorts(expectation.context_cohorts),
        fuzzy_core_cohorts=clone_cohorts(expectation.fuzzy_core_cohorts),
        fts5_trigram=RetrievalBenchmarkExpectation(
            path=expectation.fts5_trigram.path,
            contract_digest=expectation.fts5_trigram.contract_digest,
        ),
        gram_fallback=RetrievalBenchmarkExpectation(
            path=expectation.gram_fallback.path,
            contract_digest=expectation.gram_fallback.contract_digest,
        ),
    )


# --- evaluator --------------------------------------------------------------


def _closed_decision(
    *,
    context_code: str,
    fuzzy_core_code: str,
    fts5_code: str,
    gram_code: str,
) -> tuple[
    RetrievalContextDecision,
    RetrievalFuzzyCoreDecision,
    RetrievalFuzzyPathDecision,
    RetrievalFuzzyPathDecision,
]:
    return (
        RetrievalContextDecision(
            available=False,
            unavailable_code=context_code,
        ),
        RetrievalFuzzyCoreDecision(
            available=False,
            unavailable_code=fuzzy_core_code,
        ),
        RetrievalFuzzyPathDecision(
            path="FTS5_TRIGRAM",
            available=False,
            unavailable_code=fts5_code,
        ),
        RetrievalFuzzyPathDecision(
            path="GRAM_FALLBACK",
            available=False,
            unavailable_code=gram_code,
        ),
    )


def _closed_snapshot(
    expectation: RetrievalCapabilityExpectation,
    *,
    evaluated_at_utc: datetime,
    context_code: str,
    fuzzy_core_code: str,
    fts5_code: str,
    gram_code: str,
    manifest: RetrievalCapabilityManifest | None,
) -> RetrievalCapabilitySnapshot:
    decisions = _closed_decision(
        context_code=context_code,
        fuzzy_core_code=fuzzy_core_code,
        fts5_code=fts5_code,
        gram_code=gram_code,
    )
    payload: dict[str, object] = {
        "canonical_manifest": (
            None
            if manifest is None
            else json.loads(_manifest_payload_json(manifest))
        ),
        "derived_decisions": _decisions_payload(decisions),
        "frozen_expectation": _expectation_payload(expectation),
        "summary_version": RETRIEVAL_CAPABILITY_SUMMARY_VERSION,
    }
    return _snapshot_from_decisions(
        expectation,
        decisions=decisions,
        evaluated_at_utc=evaluated_at_utc,
        evidence_digest=_canonical_digest(payload),
    )


def _decisions_payload(
    decisions: tuple[
        RetrievalContextDecision,
        RetrievalFuzzyCoreDecision,
        RetrievalFuzzyPathDecision,
        RetrievalFuzzyPathDecision,
    ],
) -> dict[str, dict[str, object]]:
    return {
        "context": {
            "available": decisions[0].available,
            "unavailable_code": decisions[0].unavailable_code,
        },
        "fuzzy_core": {
            "available": decisions[1].available,
            "unavailable_code": decisions[1].unavailable_code,
        },
        "fts5_trigram": {
            "available": decisions[2].available,
            "unavailable_code": decisions[2].unavailable_code,
        },
        "gram_fallback": {
            "available": decisions[3].available,
            "unavailable_code": decisions[3].unavailable_code,
        },
    }


def _snapshot_from_decisions(
    expectation: RetrievalCapabilityExpectation,
    *,
    decisions: tuple[
        RetrievalContextDecision,
        RetrievalFuzzyCoreDecision,
        RetrievalFuzzyPathDecision,
        RetrievalFuzzyPathDecision,
    ],
    evaluated_at_utc: datetime,
    evidence_digest: str,
) -> RetrievalCapabilitySnapshot:
    return RetrievalCapabilitySnapshot(
        semantics_version=expectation.semantics_version,
        context=decisions[0],
        fuzzy_core=decisions[1],
        fts5_trigram=decisions[2],
        gram_fallback=decisions[3],
        summary=RetrievalCapabilityEvidenceSummary(
            summary_version=RETRIEVAL_CAPABILITY_SUMMARY_VERSION,
            evidence_digest=evidence_digest,
            evaluated_at_utc=evaluated_at_utc,
            unavailable_codes=tuple(
                sorted(
                    {
                        code
                        for decision in decisions
                        for code in (decision.unavailable_code,)
                        if code is not None
                    }
                )
            ),
        ),
    )


def _envelope_identity_matches(
    manifest: RetrievalCapabilityManifest,
    expectation: RetrievalCapabilityExpectation,
) -> bool:
    return (
        manifest.evidence_schema_version
        == expectation.evidence_schema_version
        and manifest.retrieval_artifact_digest
        == expectation.retrieval_artifact_digest
        and manifest.retrieval_build_digest
        == expectation.retrieval_build_digest
        and manifest.semantics_version
        == expectation.semantics_version
        and manifest.fixture_digest == expectation.fixture_digest
        and manifest.evaluator_digest == expectation.evaluator_digest
    )


def _context_decision(
    manifest: RetrievalCapabilityManifest,
    expectation: RetrievalCapabilityExpectation,
    *,
    evaluated_at_utc: datetime,
) -> RetrievalContextDecision:
    if not _envelope_identity_matches(manifest, expectation):
        return RetrievalContextDecision(
            available=False,
            unavailable_code=RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE,
        )
    if tuple(
        evidence.cohort_id for evidence in manifest.context_cohorts
    ) != tuple(cohort.cohort_id for cohort in expectation.context_cohorts):
        return RetrievalContextDecision(
            available=False,
            unavailable_code=RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE,
        )
    evidence_by_id = {
        evidence.cohort_id: evidence
        for evidence in manifest.context_cohorts
    }
    for cohort in expectation.context_cohorts:
        evidence = evidence_by_id.get(cohort.cohort_id)
        if evidence is None:
            return RetrievalContextDecision(
                available=False,
                unavailable_code=RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE,
            )
        if (
            evidence.cohort_digest != cohort.cohort_digest
            or evidence.passed is not True
        ):
            return RetrievalContextDecision(
                available=False,
                unavailable_code=RETRIEVAL_CONTEXT_EVIDENCE_FAILED_CODE,
            )
    for cohort in expectation.context_cohorts:
        evidence = evidence_by_id[cohort.cohort_id]
        if not _is_active_window(
            generated_at_utc=evidence.generated_at_utc,
            valid_until_utc=evidence.valid_until_utc,
            evaluated_at_utc=evaluated_at_utc,
        ):
            return RetrievalContextDecision(
                available=False,
                unavailable_code=RETRIEVAL_CONTEXT_EVIDENCE_EXPIRED_CODE,
            )
    if not _is_active_window(
        generated_at_utc=manifest.generated_at_utc,
        valid_until_utc=manifest.valid_until_utc,
        evaluated_at_utc=evaluated_at_utc,
    ):
        return RetrievalContextDecision(
            available=False,
            unavailable_code=RETRIEVAL_CONTEXT_EVIDENCE_EXPIRED_CODE,
        )
    return RetrievalContextDecision(
        available=True,
        unavailable_code=None,
    )


def _fuzzy_core_decision(
    manifest: RetrievalCapabilityManifest,
    expectation: RetrievalCapabilityExpectation,
    *,
    evaluated_at_utc: datetime,
) -> RetrievalFuzzyCoreDecision:
    if not _envelope_identity_matches(manifest, expectation):
        return RetrievalFuzzyCoreDecision(
            available=False,
            unavailable_code=(
                RETRIEVAL_FUZZY_CORRECTNESS_IDENTITY_INVALID_CODE
            ),
        )
    if tuple(
        evidence.cohort_id for evidence in manifest.fuzzy_core_cohorts
    ) != tuple(
        cohort.cohort_id for cohort in expectation.fuzzy_core_cohorts
    ):
        return RetrievalFuzzyCoreDecision(
            available=False,
            unavailable_code=(
                RETRIEVAL_FUZZY_CORRECTNESS_IDENTITY_INVALID_CODE
            ),
        )
    evidence_by_id = {
        evidence.cohort_id: evidence
        for evidence in manifest.fuzzy_core_cohorts
    }
    for cohort in expectation.fuzzy_core_cohorts:
        evidence = evidence_by_id.get(cohort.cohort_id)
        if evidence is None:
            return RetrievalFuzzyCoreDecision(
                available=False,
                unavailable_code=(
                    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE
                ),
            )
        if (
            evidence.cohort_digest != cohort.cohort_digest
            or evidence.passed is not True
        ):
            return RetrievalFuzzyCoreDecision(
                available=False,
                unavailable_code=(
                    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE
                ),
            )
    for cohort in expectation.fuzzy_core_cohorts:
        evidence = evidence_by_id[cohort.cohort_id]
        if not _is_active_window(
            generated_at_utc=evidence.generated_at_utc,
            valid_until_utc=evidence.valid_until_utc,
            evaluated_at_utc=evaluated_at_utc,
        ):
            return RetrievalFuzzyCoreDecision(
                available=False,
                unavailable_code=(
                    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_EXPIRED_CODE
                ),
            )
    if not _is_active_window(
        generated_at_utc=manifest.generated_at_utc,
        valid_until_utc=manifest.valid_until_utc,
        evaluated_at_utc=evaluated_at_utc,
    ):
        return RetrievalFuzzyCoreDecision(
            available=False,
            unavailable_code=(
                RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_EXPIRED_CODE
            ),
        )
    return RetrievalFuzzyCoreDecision(
        available=True,
        unavailable_code=None,
    )


def _benchmark_report_is_valid(report: BenchmarkReport) -> bool:
    try:
        recomputed_contract = benchmark_contract_digest(report.contract)
        recomputed_environment = benchmark_environment_digest(
            report.environment
        )
    except (TypeError, ValueError):
        return False
    return (
        recomputed_contract == report.contract_digest
        and recomputed_environment == report.environment_digest
    )


def _path_decision(
    *,
    identity_ok: bool,
    evidence: RetrievalBenchmarkEvidence | None,
    expectation: RetrievalBenchmarkExpectation,
    manifest: RetrievalCapabilityManifest,
    evaluated_at_utc: datetime,
    identity_invalid_code: str,
    missing_code: str,
    failed_code: str,
    expired_code: str,
) -> RetrievalFuzzyPathDecision:
    if not identity_ok:
        return RetrievalFuzzyPathDecision(
            path=expectation.path,
            available=False,
            unavailable_code=identity_invalid_code,
        )
    if evidence is None:
        return RetrievalFuzzyPathDecision(
            path=expectation.path,
            available=False,
            unavailable_code=missing_code,
        )
    report = evidence.report
    if (
        report.execution_path.value != expectation.path
        or report.contract_digest != expectation.contract_digest
        or report.passed is not True
        or report.failed_gates != ()
        or not _benchmark_report_is_valid(report)
    ):
        return RetrievalFuzzyPathDecision(
            path=expectation.path,
            available=False,
            unavailable_code=failed_code,
        )
    if not _is_active_window(
        generated_at_utc=evidence.generated_at_utc,
        valid_until_utc=evidence.valid_until_utc,
        evaluated_at_utc=evaluated_at_utc,
    ):
        return RetrievalFuzzyPathDecision(
            path=expectation.path,
            available=False,
            unavailable_code=expired_code,
        )
    if not _is_active_window(
        generated_at_utc=manifest.generated_at_utc,
        valid_until_utc=manifest.valid_until_utc,
        evaluated_at_utc=evaluated_at_utc,
    ):
        return RetrievalFuzzyPathDecision(
            path=expectation.path,
            available=False,
            unavailable_code=expired_code,
        )
    return RetrievalFuzzyPathDecision(
        path=expectation.path,
        available=True,
        unavailable_code=None,
    )


@final
class RetrievalCapabilityEvaluator:
    """The only retrieval state decision from capability evidence."""

    __slots__: tuple[str, ...] = ("__expectation",)

    def __init__(
        self,
        expectation: RetrievalCapabilityExpectation,
    ) -> None:
        self.__expectation = _require_expectation(expectation)

    @property
    def expectation(self) -> RetrievalCapabilityExpectation:
        return self.__expectation

    def evaluate(
        self,
        manifest: RetrievalCapabilityManifest | None,
        *,
        evaluated_at_utc: datetime,
    ) -> RetrievalCapabilitySnapshot:
        """Derive one fail-closed capability snapshot at an explicit instant."""

        instant = _require_utc_instant(evaluated_at_utc)
        if manifest is None:
            return _closed_snapshot(
                self.__expectation,
                evaluated_at_utc=instant,
                context_code=RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE,
                fuzzy_core_code=(
                    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE
                ),
                fts5_code=RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
                gram_code=RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
                manifest=None,
            )
        validated_manifest = _require_manifest(manifest)
        try:
            manifest_payload_json = _manifest_payload_json(
                validated_manifest
            )
        except (TypeError, ValueError):
            return _closed_snapshot(
                self.__expectation,
                evaluated_at_utc=instant,
                context_code=RETRIEVAL_CONTEXT_EVIDENCE_FAILED_CODE,
                fuzzy_core_code=(
                    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE
                ),
                fts5_code=RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
                gram_code=RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
                manifest=validated_manifest,
            )

        identity_ok = _envelope_identity_matches(
            validated_manifest,
            self.__expectation,
        )
        context = _context_decision(
            validated_manifest,
            self.__expectation,
            evaluated_at_utc=instant,
        )
        fuzzy_core = _fuzzy_core_decision(
            validated_manifest,
            self.__expectation,
            evaluated_at_utc=instant,
        )
        fts5_trigram = _path_decision(
            identity_ok=identity_ok,
            evidence=validated_manifest.fts5_trigram_benchmark,
            expectation=self.__expectation.fts5_trigram,
            manifest=validated_manifest,
            evaluated_at_utc=instant,
            identity_invalid_code=(
                RETRIEVAL_FUZZY_BENCHMARK_IDENTITY_INVALID_CODE
            ),
            missing_code=RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
            failed_code=RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
            expired_code=RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_EXPIRED_CODE,
        )
        gram_fallback = _path_decision(
            identity_ok=identity_ok,
            evidence=validated_manifest.gram_fallback_benchmark,
            expectation=self.__expectation.gram_fallback,
            manifest=validated_manifest,
            evaluated_at_utc=instant,
            identity_invalid_code=(
                RETRIEVAL_FUZZY_BENCHMARK_IDENTITY_INVALID_CODE
            ),
            missing_code=RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
            failed_code=RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE,
            expired_code=RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_EXPIRED_CODE,
        )
        decisions = (context, fuzzy_core, fts5_trigram, gram_fallback)
        payload: dict[str, object] = {
            "canonical_manifest": json.loads(manifest_payload_json),
            "derived_decisions": _decisions_payload(decisions),
            "frozen_expectation": _expectation_payload(
                self.__expectation
            ),
            "summary_version": RETRIEVAL_CAPABILITY_SUMMARY_VERSION,
        }
        return _snapshot_from_decisions(
            self.__expectation,
            decisions=decisions,
            evaluated_at_utc=instant,
            evidence_digest=_canonical_digest(payload),
        )


def _require_manifest(
    value: object,
) -> RetrievalCapabilityManifest:
    _require_exact_type(
        value,
        RetrievalCapabilityManifest,
        "manifest",
    )
    return cast(RetrievalCapabilityManifest, value)


def _require_expectation(
    value: object,
) -> RetrievalCapabilityExpectation:
    _require_exact_type(
        value,
        RetrievalCapabilityExpectation,
        "expectation",
    )
    return cast(RetrievalCapabilityExpectation, value)


def _require_evaluator(
    value: object,
) -> RetrievalCapabilityEvaluator:
    _require_exact_type(
        value,
        RetrievalCapabilityEvaluator,
        "evaluator",
    )
    return cast(RetrievalCapabilityEvaluator, value)


# --- publisher --------------------------------------------------------------


_ValidatedPublicationT = TypeVar("_ValidatedPublicationT")
_QUERY_OPERATION_RECEIPT_MINT = object()


@final
class _RetrievalCapabilityOperationReceipt:
    """One publisher-issued, service-bound, single-use snapshot receipt."""

    __slots__ = (
        "__consumed",
        "__lock",
        "__mint_identity",
        "__publisher",
        "__publisher_identity",
        "__service_identity",
        "__snapshot",
        "__snapshot_identity",
    )

    def __init__(
        self,
        *,
        publisher: RetrievalCapabilityPublisher,
        service_identity: object,
        snapshot: RetrievalCapabilitySnapshot,
        mint_identity: object,
    ) -> None:
        if mint_identity is not _QUERY_OPERATION_RECEIPT_MINT:
            raise PermissionError("query receipt mint is private")
        if type(publisher) is not RetrievalCapabilityPublisher:
            raise TypeError("query receipt publisher must be exact")
        if service_identity is None:
            raise TypeError("query receipt service identity is required")
        if type(snapshot) is not RetrievalCapabilitySnapshot:
            raise TypeError("query receipt snapshot must be exact")
        self.__consumed = False
        self.__lock = Lock()
        self.__mint_identity = mint_identity
        self.__publisher = publisher
        self.__publisher_identity = publisher
        self.__service_identity = service_identity
        self.__snapshot = snapshot
        self.__snapshot_identity = snapshot

    def _inspect(
        self,
        *,
        publisher: RetrievalCapabilityPublisher,
        service_identity: object,
        mint_identity: object,
    ) -> RetrievalCapabilitySnapshot:
        with self.__lock:
            self.__validate_binding(
                publisher=publisher,
                service_identity=service_identity,
                mint_identity=mint_identity,
            )
            if self.__consumed:
                raise ValueError("query operation receipt is already consumed")
            return self.__snapshot

    def _consume(
        self,
        *,
        publisher: RetrievalCapabilityPublisher,
        service_identity: object,
        mint_identity: object,
    ) -> RetrievalCapabilitySnapshot:
        with self.__lock:
            self.__validate_binding(
                publisher=publisher,
                service_identity=service_identity,
                mint_identity=mint_identity,
            )
            if self.__consumed:
                raise ValueError("query operation receipt is already consumed")
            self.__consumed = True
            return self.__snapshot

    def __validate_binding(
        self,
        *,
        publisher: RetrievalCapabilityPublisher,
        service_identity: object,
        mint_identity: object,
    ) -> None:
        if (
            mint_identity is not _QUERY_OPERATION_RECEIPT_MINT
            or self.__mint_identity is not _QUERY_OPERATION_RECEIPT_MINT
            or self.__publisher is not self.__publisher_identity
            or self.__publisher is not publisher
        ):
            raise ValueError("query operation receipt publisher drift")
        if self.__service_identity is not service_identity:
            raise ValueError("query operation receipt belongs to a foreign retrieval service")
        if (
            self.__snapshot is not self.__snapshot_identity
            or type(self.__snapshot) is not RetrievalCapabilitySnapshot
        ):
            raise ValueError("query operation receipt snapshot drift")


@final
class RetrievalCapabilityPublisher:
    """Atomically refresh and expose evaluator-produced snapshots only."""

    __slots__: tuple[str, ...] = (
        "__evaluator",
        "__evaluator_identity",
        "__expectation_digest",
        "__expectation_identity",
        "__lock",
        "__semantics_version",
        "__snapshot",
    )

    def __init__(
        self,
        evaluator: RetrievalCapabilityEvaluator,
        *,
        initial_manifest: RetrievalCapabilityManifest | None,
        evaluated_at_utc: datetime,
    ) -> None:
        validated_evaluator = _require_evaluator(evaluator)
        expectation = _clone_expectation(
            validated_evaluator.expectation
        )
        private_evaluator = RetrievalCapabilityEvaluator(expectation)
        self.__evaluator = private_evaluator
        self.__evaluator_identity = private_evaluator
        self.__expectation_identity = expectation
        self.__expectation_digest = _expectation_digest(expectation)
        self.__semantics_version = expectation.semantics_version
        self.__lock = Lock()
        self.__snapshot = private_evaluator.evaluate(
            initial_manifest,
            evaluated_at_utc=evaluated_at_utc,
        )

    def snapshot(self) -> RetrievalCapabilitySnapshot:
        """Return one immutable snapshot reference under the read lock."""

        with self.__lock:
            return self.__snapshot

    def _mint_query_operation_receipt(
        self,
        service_identity: object,
    ) -> object:
        """Mint one opaque receipt from exactly one atomic snapshot capture."""

        snapshot = self.snapshot()
        return _RetrievalCapabilityOperationReceipt(
            publisher=self,
            service_identity=service_identity,
            snapshot=snapshot,
            mint_identity=_QUERY_OPERATION_RECEIPT_MINT,
        )

    def _inspect_query_operation_receipt(
        self,
        receipt: object,
        service_identity: object,
    ) -> RetrievalCapabilitySnapshot:
        if type(receipt) is not _RetrievalCapabilityOperationReceipt:
            raise TypeError("query operation receipt must be publisher-issued")
        return receipt._inspect(
            publisher=self,
            service_identity=service_identity,
            mint_identity=_QUERY_OPERATION_RECEIPT_MINT,
        )

    def _consume_query_operation_receipt(
        self,
        receipt: object,
        service_identity: object,
    ) -> RetrievalCapabilitySnapshot:
        if type(receipt) is not _RetrievalCapabilityOperationReceipt:
            raise TypeError("query operation receipt must be publisher-issued")
        return receipt._consume(
            publisher=self,
            service_identity=service_identity,
            mint_identity=_QUERY_OPERATION_RECEIPT_MINT,
        )

    @property
    def semantics_version(self) -> str:
        """Return the frozen semantics identity accepted by this publisher."""

        return self.__semantics_version

    def __trusted_evaluator(
        self,
    ) -> RetrievalCapabilityEvaluator | None:
        """Return the write-once evaluator only while its identity is intact."""

        evaluator = self.__evaluator
        if evaluator is not self.__evaluator_identity:
            return None
        expectation = evaluator.expectation
        if expectation is not self.__expectation_identity:
            return None
        try:
            current_digest = _expectation_digest(expectation)
        except (TypeError, ValueError):
            return None
        if current_digest != self.__expectation_digest:
            return None
        return evaluator

    def refresh(
        self,
        manifest: RetrievalCapabilityManifest | None,
        *,
        evaluated_at_utc: datetime,
    ) -> RetrievalCapabilitySnapshot:
        """Evaluate a manifest, then atomically publish that exact result."""

        return self._validated_transition(
            manifest,
            evaluated_at_utc=evaluated_at_utc,
            expected_current=None,
            prepare=lambda candidate: candidate,
            _snapshot_descriptor=(
                _RETRIEVAL_CAPABILITY_SNAPSHOT_DESCRIPTOR
            ),
        )

    def _validated_transition(
        self,
        manifest: RetrievalCapabilityManifest | None,
        *,
        evaluated_at_utc: datetime,
        expected_current: RetrievalCapabilitySnapshot | None,
        prepare: Callable[
            [RetrievalCapabilitySnapshot],
            _ValidatedPublicationT,
        ],
        _snapshot_descriptor: object,
    ) -> _ValidatedPublicationT:
        """Commit only after an owner prepares its final return value."""

        if (
            type(_snapshot_descriptor) is not MemberDescriptorType
            or _snapshot_descriptor.__objclass__
            is not RetrievalCapabilityPublisher
            or _snapshot_descriptor.__name__
            != "_RetrievalCapabilityPublisher__snapshot"
        ):
            raise TypeError("publisher snapshot descriptor is invalid")
        snapshot_get = _snapshot_descriptor.__get__
        snapshot_set = _snapshot_descriptor.__set__
        instant = _require_utc_instant(evaluated_at_utc)
        evaluator = self.__trusted_evaluator()
        if evaluator is None:
            next_snapshot = _closed_snapshot(
                self.__expectation_identity,
                evaluated_at_utc=instant,
                context_code=RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE,
                fuzzy_core_code=(
                    RETRIEVAL_FUZZY_CORRECTNESS_IDENTITY_INVALID_CODE
                ),
                fts5_code=RETRIEVAL_FUZZY_BENCHMARK_IDENTITY_INVALID_CODE,
                gram_code=RETRIEVAL_FUZZY_BENCHMARK_IDENTITY_INVALID_CODE,
                manifest=None,
            )
        else:
            next_snapshot = evaluator.evaluate(
                manifest,
                evaluated_at_utc=instant,
            )
        with self.__lock:
            if (
                expected_current is not None
                and snapshot_get(
                    self,
                    RetrievalCapabilityPublisher,
                )
                is not expected_current
            ):
                raise ValueError(
                    "publisher current snapshot changed before commit"
                )
            trusted_at_publish = self.__trusted_evaluator()
            semantics_match = (
                next_snapshot.semantics_version
                == self.__semantics_version
            )
            if trusted_at_publish is not evaluator or not semantics_match:
                next_snapshot = _closed_snapshot(
                    self.__expectation_identity,
                    evaluated_at_utc=instant,
                    context_code=RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE,
                    fuzzy_core_code=(
                        RETRIEVAL_FUZZY_CORRECTNESS_IDENTITY_INVALID_CODE
                    ),
                    fts5_code=(
                        RETRIEVAL_FUZZY_BENCHMARK_IDENTITY_INVALID_CODE
                    ),
                    gram_code=(
                        RETRIEVAL_FUZZY_BENCHMARK_IDENTITY_INVALID_CODE
                    ),
                    manifest=None,
                )
            prepared = prepare(next_snapshot)
            snapshot_set(self, next_snapshot)
            return prepared


_RETRIEVAL_CAPABILITY_SNAPSHOT_DESCRIPTOR = (
    RetrievalCapabilityPublisher.__dict__[
        "_RetrievalCapabilityPublisher__snapshot"
    ]
)
if type(_RETRIEVAL_CAPABILITY_SNAPSHOT_DESCRIPTOR) is not MemberDescriptorType:
    raise RuntimeError("publisher snapshot descriptor is unavailable")


def _validated_refresh_retrieval_capability(
    publisher: RetrievalCapabilityPublisher,
    manifest: RetrievalCapabilityManifest,
    *,
    evaluated_at_utc: datetime,
    expected_current: RetrievalCapabilitySnapshot,
    validator: Callable[
        [RetrievalCapabilitySnapshot],
        _ValidatedPublicationT,
    ],
    _snapshot_descriptor: object,
) -> _ValidatedPublicationT:
    """Gate-owner-only validate-before-commit publisher transition."""

    if type(publisher) is not RetrievalCapabilityPublisher:
        raise TypeError("publisher must be RetrievalCapabilityPublisher")
    if type(manifest) is not RetrievalCapabilityManifest:
        raise TypeError("manifest must be RetrievalCapabilityManifest")
    if type(expected_current) is not RetrievalCapabilitySnapshot:
        raise TypeError(
            "expected_current must be RetrievalCapabilitySnapshot"
        )
    if not callable(validator):
        raise TypeError("publisher validator must be callable")
    return publisher._validated_transition(
        manifest,
        evaluated_at_utc=evaluated_at_utc,
        expected_current=expected_current,
        prepare=validator,
        _snapshot_descriptor=_snapshot_descriptor,
    )


# --- production default -----------------------------------------------------


def _identity_digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_DEFAULT_RETRIEVAL_ARTIFACT_DIGEST = _identity_digest(
    "tm-retrieval-artifact-v1"
)
_DEFAULT_RETRIEVAL_BUILD_DIGEST = _identity_digest(
    "tm-retrieval-build-v1"
)
_DEFAULT_RETRIEVAL_FIXTURE_DIGEST = _identity_digest(
    "tm-retrieval-fixture-v1"
)
_DEFAULT_RETRIEVAL_EVALUATOR_DIGEST = _identity_digest(
    "tm-retrieval-evaluator-v1"
)
_DEFAULT_CONTEXT_COHORTS = (
    RetrievalCohortExpectation(
        cohort_id="context.correctness.cohort.v1",
        cohort_digest=_identity_digest("context.correctness.cohort.v1"),
    ),
)
_DEFAULT_FUZZY_CORE_COHORTS = (
    RetrievalCohortExpectation(
        cohort_id="fuzzy.core.correctness.cohort.v1",
        cohort_digest=_identity_digest(
            "fuzzy.core.correctness.cohort.v1"
        ),
    ),
)
_DEFAULT_FTS5_BENCHMARK_CONTRACT_DIGEST = _identity_digest(
    "fts5-trigram-benchmark-v1"
)
_DEFAULT_GRAM_BENCHMARK_CONTRACT_DIGEST = _identity_digest(
    "gram-fallback-benchmark-v1"
)


def default_retrieval_capability_publisher(
    evaluated_at_utc: datetime,
) -> RetrievalCapabilityPublisher:
    """Return the production fail-closed publisher for the pinned identity.

    The approved retrieval identity is pinned by Task 7.5's manifest
    generation.  Until Gate C/D evidence is published every sub-gate stays
    closed with stable missing-evidence codes, and no evidence can grant
    availability without closing the approved identity/version/digest/path
    facts.
    """

    instant = _require_utc_instant(evaluated_at_utc)
    expectation = RetrievalCapabilityExpectation(
        evidence_schema_version=(
            RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION
        ),
        retrieval_artifact_digest=_DEFAULT_RETRIEVAL_ARTIFACT_DIGEST,
        retrieval_build_digest=_DEFAULT_RETRIEVAL_BUILD_DIGEST,
        semantics_version=RETRIEVAL_SEMANTICS_VERSION,
        fixture_digest=_DEFAULT_RETRIEVAL_FIXTURE_DIGEST,
        evaluator_digest=_DEFAULT_RETRIEVAL_EVALUATOR_DIGEST,
        context_cohorts=_DEFAULT_CONTEXT_COHORTS,
        fuzzy_core_cohorts=_DEFAULT_FUZZY_CORE_COHORTS,
        fts5_trigram=RetrievalBenchmarkExpectation(
            path="FTS5_TRIGRAM",
            contract_digest=_DEFAULT_FTS5_BENCHMARK_CONTRACT_DIGEST,
        ),
        gram_fallback=RetrievalBenchmarkExpectation(
            path="GRAM_FALLBACK",
            contract_digest=_DEFAULT_GRAM_BENCHMARK_CONTRACT_DIGEST,
        ),
    )
    return RetrievalCapabilityPublisher(
        RetrievalCapabilityEvaluator(expectation),
        initial_manifest=None,
        evaluated_at_utc=instant,
    )


__all__ = [
    "RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION",
    "RETRIEVAL_CAPABILITY_SUMMARY_VERSION",
    "RETRIEVAL_SEMANTICS_VERSION",
    "RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE",
    "RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE",
    "RETRIEVAL_CONTEXT_EVIDENCE_FAILED_CODE",
    "RETRIEVAL_CONTEXT_EVIDENCE_EXPIRED_CODE",
    "RETRIEVAL_FUZZY_CORRECTNESS_IDENTITY_INVALID_CODE",
    "RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE",
    "RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE",
    "RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_EXPIRED_CODE",
    "RETRIEVAL_FUZZY_BENCHMARK_IDENTITY_INVALID_CODE",
    "RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE",
    "RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_FAILED_CODE",
    "RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_EXPIRED_CODE",
    "RetrievalBenchmarkEvidence",
    "RetrievalBenchmarkExpectation",
    "RetrievalCapabilityEvaluator",
    "RetrievalCapabilityEvidenceSummary",
    "RetrievalCapabilityExpectation",
    "RetrievalCapabilityManifest",
    "RetrievalCapabilityPublisher",
    "RetrievalCapabilitySnapshot",
    "RetrievalContextDecision",
    "RetrievalCorrectnessCohortEvidence",
    "RetrievalCohortExpectation",
    "RetrievalFuzzyCoreDecision",
    "RetrievalFuzzyPathDecision",
    "default_retrieval_capability_publisher",
]
