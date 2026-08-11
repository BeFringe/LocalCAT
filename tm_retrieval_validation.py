"""Offline Gate C retrieval validation leaf.

Task 7.5 slice: the only module that reruns the frozen context-v1 vectors
against production ``classify_exact_context``, the closed fuzzy scoring
vectors against production ``score_fuzzy_candidates``, one fixed real
temporary-store journey against public ``SQLiteTMStore``,
``CandidateRetriever`` and query-view ports, and a harness-scoped
CONTEXT-open/fuzzy-closed multi-resource service journey against real
temporary ``SQLiteTMStore`` stages and ``TMRetrievalService`` with
cross-resource global-limit and resource-local lease-failure evidence,
then recomputes observed Gate C digests from an approved closed-field
roots file.  It returns one immutable ``RetrievalValidationRelease``
carrying the approved expectation and a short-lived, non-persisted
manifest.  The fuzzy scoring and store transcripts are observed and
verified but are not yet folded into the fuzzy-core cohort digest:
fuzzy-core correctness stays on the deterministic IMPLEMENTATION_PENDING
digest and never passes; the FTS5_TRIGRAM and GRAM_FALLBACK benchmark
evidence rows stay empty until Task 8.

This leaf is never imported by a production runtime module and never
publishes capability.  The service journey builds one non-returned,
non-persisted harness ``RetrievalCapabilityPublisher`` from the approved
expectation and the independently recomputed CONTEXT evidence with fuzzy
correctness pending and both Gate D rows empty; the harness snapshot is
captured once per query and the final fuzzy-core manifest digest is never
used to authorize the same run.  The manifest ``passed`` flags are derived only from
observed digest equality with the approved roots; no caller-supplied
``passed`` value or callback exists.  Transcripts, manifests and summaries
carry only vector ids, record ids, candidate stage names, counts, match
types, field names, strength tuples, similarities, stable tie keys,
runtime capability, generation/revision, before/after counts, exception
types and rollback booleans - never source, target, speaker, previous or
next bodies, provenance, paths or exception text.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, cast

from tm_candidate_index import CandidateRetriever
from tm_contracts import (
    CANDIDATE_BUDGET_VERSION,
    SCORER_VERSION_V1,
    CanonicalResourceIdentity,
    CandidateEvidence,
    CandidateRecallMetadata,
    CandidateRetrievalReport,
    CandidateStage,
    CandidateStageMetadata,
    ContextEvidence,
    MutableStageRef,
    QueryReport,
    SimilarityScorer,
    StoreHealth,
    TMStore,
    TMMatchType,
    TMQuery,
    TMRecord,
    TMRecordDraft,
    TMResourceHandle,
    TMResult,
    candidate_budget_v1,
)
from tm_gate_a import (
    aggregate_paths_digest,
    canonical_digest,
    require_digest,
    require_paths,
    require_string,
)
from tm_retrieval import (
    ExactContextClassification,
    FuzzyScoringResult,
    TMRetrievalService,
    classify_exact_context,
    score_fuzzy_candidates,
)
from tm_retrieval_capability import (
    RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION,
    RETRIEVAL_CONTEXT_EVIDENCE_FAILED_CODE,
    RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE,
    RETRIEVAL_SEMANTICS_VERSION,
    RetrievalBenchmarkExpectation,
    RetrievalCapabilityEvaluator,
    RetrievalCapabilityExpectation,
    RetrievalCapabilityManifest,
    RetrievalCapabilityPublisher,
    RetrievalCapabilitySnapshot,
    RetrievalCorrectnessCohortEvidence,
    RetrievalCohortExpectation,
)
from tm_sqlite_store import (
    CanonicalRevisionSnapshot,
    SQLiteStoreLifecycleError,
    SQLiteTMStore,
    SQLiteTMQueryView,
    initialize_stage_schema,
)


RETRIEVAL_GATE_C_ROOTS_SCHEMA_VERSION = "retrieval-gate-c-roots-v1"
RETRIEVAL_CONTEXT_COHORT_ID = "context.correctness.cohort.v1"
RETRIEVAL_FUZZY_CORE_COHORT_ID = "fuzzy.core.correctness.cohort.v1"

_FIXTURE_VERSION = "retrieval-gate-c-context-v1"
_CONTEXT_VECTORS_FIXTURE = "tests/fixtures/retrieval_gate_c_vectors_v1.json"
_IMPLEMENTATION_PENDING = "IMPLEMENTATION_PENDING"
_MAX_EVIDENCE_TTL = timedelta(days=30)
_RESOURCE_ID = "tm.gate-c"
_ORIGIN_BATCH_ID = "retrieval-gate-c-context-v1"
_QUERY_SOURCE = "Open the door."
_MINIMUM_SIMILARITY = 0.7
_LIMIT = 10
_DEFAULT_APPROVED_ROOTS = (
    Path(__file__).parent
    / "tests"
    / "fixtures"
    / "retrieval_gate_c_roots_v1.json"
)
_CONTEXT_FIELDS = (
    "speaker_raw",
    "context_prev_raw",
    "context_next_raw",
)
_FUZZY_SCORING_SECTION_VERSION = "retrieval-gate-c-fuzzy-scoring-v1"
_FUZZY_ORIGIN_BATCH_ID = "retrieval-gate-c-fuzzy-scoring-v1"
_FUZZY_SOURCE_STAGES = (
    CandidateStage.FTS_TRIGRAM,
    CandidateStage.GRAM_3,
    CandidateStage.GRAM_2,
    CandidateStage.GRAM_1,
)
_FUZZY_INDEX_KINDS = ("FTS5_TRIGRAM", "GRAM_FALLBACK")
_STORE_SECTION_VERSION = "retrieval-gate-c-store-v1"
_STORE_ORIGIN_PROVENANCE = ("origin", _STORE_SECTION_VERSION)
_SERVICE_SECTION_VERSION = "retrieval-gate-c-service-v1"
_SERVICE_ORIGIN_PROVENANCE = ("origin", _SERVICE_SECTION_VERSION)

type ValidationJsonValue = (
    None
    | bool
    | int
    | float
    | str
    | list["ValidationJsonValue"]
    | dict[str, "ValidationJsonValue"]
)


@dataclass(frozen=True)
class RetrievalValidationRelease:
    """Observed Gate C manifest plus its immutable approved expectation."""

    expectation: RetrievalCapabilityExpectation
    manifest: RetrievalCapabilityManifest | None


@dataclass(frozen=True)
class _StoreDraftConfig:
    ordinal: int
    source_raw: str
    target_raw: str


@dataclass(frozen=True)
class _ServiceDraftConfig:
    ordinal: int
    source_raw: str
    target_raw: str
    speaker_raw: str | None
    context_prev_raw: str | None
    context_next_raw: str | None


@dataclass(frozen=True)
class _ServiceResourceConfig:
    id: str
    resource_id: str
    canonical_store_id: str
    stage_id: str
    batch_id: str
    batch_kind: str
    source_digest: str
    source_name: str
    drafts: tuple[_ServiceDraftConfig, ...]
    expected: dict[str, object]


@dataclass(frozen=True)
class _ServiceQueryConfig:
    query_source: str
    speaker_raw: str | None
    context_prev_raw: str | None
    context_next_raw: str | None
    minimum_similarity: float
    limit: int
    resource_order: tuple[str, ...]


@dataclass(frozen=True)
class _ServiceScenarioConfig:
    id: str
    kind: str
    failed_resource_id: str | None
    failure: dict[str, object] | None
    expected: dict[str, object]


class _NoFuzzyAccessPort:
    """Harness-only sentinel proving closed fuzzy never touches ports."""

    def candidates_from_view(
        self,
        *args: object,
        **kwargs: object,
    ) -> object:
        raise AssertionError("candidate recall must stay closed")

    def score(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("fuzzy scoring must stay closed")


class _GenerationChangedQueryLeaseStore:
    """Harness-only wrapper failing the public ``query_lease`` port.

    Delegates the five ``TMStore`` protocol ports to the real store and
    raises one stable ``SQLiteStoreLifecycleError`` from ``query_lease``
    with code ``STORE.GENERATION_CHANGED`` and ``retryable=True``.  The
    wrapper never leaks the exception message or record bodies.
    """

    def __init__(
        self,
        store: SQLiteTMStore,
        *,
        resource_id: str,
        generation: int,
    ) -> None:
        self._store = store
        self._resource_id = resource_id
        self._generation = generation

    def query_lease(self) -> Iterator[object]:
        raise SQLiteStoreLifecycleError(
            "STORE.GENERATION_CHANGED",
            resource_id=self._resource_id,
            generation=self._generation,
            retryable=True,
        )

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        return self._store.exact_records(source_raw)

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]:
        return self._store.records_by_id(record_ids)

    def append(self, draft: TMRecordDraft) -> TMRecord:
        return self._store.append(draft)

    def export_records(self) -> Iterator[TMRecord]:
        return self._store.export_records()

    def health(self) -> StoreHealth:
        return self._store.health()


class _HarnessServiceView:
    """Read-only view adapter carrying harness-verified exact health.

    The real ``SQLiteTMQueryView`` is an unpublished stage whose physical
    ``health()`` reports ``exact_available=False`` with the
    ``STORE.CANONICAL_NOT_ACTIVE`` diagnostic.  Activation requires the
    migration/sealing pipeline that this offline leaf is not allowed to
    import, so the harness independently proves exact availability on the
    real stage through ``_service_resource_observed`` before the service
    runs and adapts the real view with the harness health snapshot.  Every
    read still flows through the real ``SQLiteTMQueryView`` lease.
    """

    def __init__(
        self,
        view: SQLiteTMQueryView,
        health: StoreHealth,
    ) -> None:
        self._view = view
        self._health = health

    @property
    def resource_id(self) -> str:
        return self._view.resource_id

    @property
    def generation(self) -> int:
        return self._view.generation

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        return self._view.exact_records(source_raw)

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]:
        return self._view.records_by_id(record_ids)

    def health(self) -> StoreHealth:
        return self._health


class _HarnessServiceStore:
    """Harness adapter handing one real store to ``TMRetrievalService``.

    Delegates the public ``query_lease`` port to the real ``SQLiteTMStore``
    so the service consumes the genuine read-only query view and records;
    the yielded view reports the harness-verified exact health.  The other
    ``TMStore`` protocol ports stay delegated to the real store and are
    never called by the service.
    """

    def __init__(
        self,
        store: SQLiteTMStore,
        *,
        health: StoreHealth,
    ) -> None:
        self._store = store
        self._health = health

    @contextmanager
    def query_lease(self) -> Iterator[_HarnessServiceView]:
        with self._store.query_lease() as view:
            yield _HarnessServiceView(view, self._health)

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        return self._store.exact_records(source_raw)

    def records_by_id(
        self,
        record_ids: tuple[int, ...],
    ) -> tuple[TMRecord, ...]:
        return self._store.records_by_id(record_ids)

    def append(self, draft: TMRecordDraft) -> TMRecord:
        return self._store.append(draft)

    def export_records(self) -> Iterator[TMRecord]:
        return self._store.export_records()

    def health(self) -> StoreHealth:
        return self._store.health()


@dataclass(frozen=True)
class _StoreConfig:
    id: str
    resource_id: str
    canonical_store_id: str
    stage_id: str
    batch_id: str
    batch_kind: str
    source_digest: str
    source_name: str
    duplicate_source_digest: str
    duplicate_source_name: str
    query_source: str
    folded_query: str
    minimum_similarity: float
    result_limit: int
    drafts: tuple[_StoreDraftConfig, ...]
    duplicate_draft: _StoreDraftConfig


def recompute_retrieval_validation(
    *,
    repository_root: Path,
    approved_roots_path: Path = _DEFAULT_APPROVED_ROOTS,
    generated_at_utc: datetime,
    valid_until_utc: datetime,
) -> RetrievalValidationRelease:
    """Execute the frozen context vectors and build one ephemeral manifest.

    Malformed approved roots raise.  A failure to observe current source or
    fixture bytes returns the approved expectation with ``manifest=None`` so
    the evaluator stays fail-closed.
    """

    if not isinstance(repository_root, Path) or not repository_root.is_dir():
        raise ValueError("repository_root must be an existing directory")
    generated = _require_utc(generated_at_utc, "generated_at_utc")
    valid_until = _require_utc(valid_until_utc, "valid_until_utc")
    lifetime = valid_until - generated
    if lifetime <= timedelta(0):
        raise ValueError("valid_until_utc must be later than generated_at_utc")
    if lifetime > _MAX_EVIDENCE_TTL:
        raise ValueError("retrieval evidence TTL must not exceed 30 days")

    approved = _load_approved_roots(approved_roots_path)
    expectation = _expectation_from_approved(approved)
    artifact_paths = require_paths(
        approved.get("artifact_paths"),
        "retrieval artifact_paths",
    )
    build_paths = require_paths(
        approved.get("build_paths"),
        "retrieval build_paths",
    )
    fixture_paths = require_paths(
        approved.get("fixture_paths"),
        "retrieval fixture_paths",
    )
    evaluator_path = require_string(
        approved.get("evaluator_path"),
        "retrieval evaluator_path",
    )
    if fixture_paths != (_CONTEXT_VECTORS_FIXTURE,):
        raise ValueError("retrieval fixture paths are not closed")

    try:
        observed_artifact_digest = aggregate_paths_digest(
            repository_root,
            artifact_paths,
        )
        observed_build_digest = aggregate_paths_digest(
            repository_root,
            build_paths,
        )
        observed_fixture_digest = aggregate_paths_digest(
            repository_root,
            fixture_paths,
        )
        observed_evaluator_digest = aggregate_paths_digest(
            repository_root,
            (evaluator_path,),
        )
        transcript = _observe_context_transcript(
            repository_root / _CONTEXT_VECTORS_FIXTURE,
        )
        fuzzy_transcript = _observe_fuzzy_scoring_transcript(
            repository_root / _CONTEXT_VECTORS_FIXTURE,
        )
        if not fuzzy_transcript:
            raise ValueError("fuzzy scoring transcript must not be empty")
        store_transcript = _observe_store_transcript(
            repository_root / _CONTEXT_VECTORS_FIXTURE,
        )
        if not store_transcript:
            raise ValueError("store transcript must not be empty")
        observed_context_digest = canonical_digest(
            {
                "fixture_digest": observed_fixture_digest,
                "transcript": transcript,
            }
        )
        observed_fuzzy_core_digest = canonical_digest(
            {"implementation": _IMPLEMENTATION_PENDING}
        )
        if (
            observed_fuzzy_core_digest
            == expectation.fuzzy_core_cohorts[0].cohort_digest
        ):
            raise ValueError(
                "approved fuzzy-core digest must differ from the pending marker"
            )
        service_transcript = _observe_service_transcript(
            repository_root / _CONTEXT_VECTORS_FIXTURE,
            expectation=expectation,
            observed_context_digest=observed_context_digest,
            observed_fuzzy_core_digest=observed_fuzzy_core_digest,
            generated_at_utc=generated,
            valid_until_utc=valid_until,
        )
        if not service_transcript:
            raise ValueError("service transcript must not be empty")
    except Exception:
        # Approved roots remain authoritative and validated above.  Failure
        # to observe current source or fixture bytes cannot mint evidence.
        return RetrievalValidationRelease(
            expectation=expectation,
            manifest=None,
        )

    generated_text = _format_utc(generated)
    valid_until_text = _format_utc(valid_until)
    try:
        manifest = RetrievalCapabilityManifest(
            evidence_schema_version=(
                RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION
            ),
            retrieval_artifact_digest=observed_artifact_digest,
            retrieval_build_digest=observed_build_digest,
            semantics_version=RETRIEVAL_SEMANTICS_VERSION,
            fixture_digest=observed_fixture_digest,
            evaluator_digest=observed_evaluator_digest,
            generated_at_utc=generated_text,
            valid_until_utc=valid_until_text,
            context_cohorts=(
                RetrievalCorrectnessCohortEvidence(
                    cohort_id=expectation.context_cohorts[0].cohort_id,
                    cohort_digest=observed_context_digest,
                    passed=(
                        observed_context_digest
                        == expectation.context_cohorts[0].cohort_digest
                    ),
                    generated_at_utc=generated_text,
                    valid_until_utc=valid_until_text,
                ),
            ),
            fuzzy_core_cohorts=(
                RetrievalCorrectnessCohortEvidence(
                    cohort_id=expectation.fuzzy_core_cohorts[0].cohort_id,
                    cohort_digest=observed_fuzzy_core_digest,
                    passed=(
                        observed_fuzzy_core_digest
                        == expectation.fuzzy_core_cohorts[0].cohort_digest
                    ),
                    generated_at_utc=generated_text,
                    valid_until_utc=valid_until_text,
                ),
            ),
            fts5_trigram_benchmark=None,
            gram_fallback_benchmark=None,
        )
    except Exception:
        # Invalid evidence cannot be partially minted.
        manifest = None
    return RetrievalValidationRelease(
        expectation=expectation,
        manifest=manifest,
    )


def _expectation_from_approved(
    roots: Mapping[str, ValidationJsonValue],
) -> RetrievalCapabilityExpectation:
    if (
        roots.get("schema_version")
        != RETRIEVAL_GATE_C_ROOTS_SCHEMA_VERSION
    ):
        raise ValueError("unsupported retrieval roots schema")
    if set(roots) != {
        "artifact_digest",
        "artifact_paths",
        "build_digest",
        "build_paths",
        "context_cohorts",
        "evaluator_digest",
        "evaluator_path",
        "fixture_digest",
        "fixture_paths",
        "fts5_trigram",
        "fuzzy_core_cohorts",
        "gram_fallback",
        "schema_version",
        "semantics_version",
    }:
        raise ValueError("retrieval roots fields are not closed")
    if (
        require_string(
            roots.get("semantics_version"),
            "retrieval semantics_version",
        )
        != RETRIEVAL_SEMANTICS_VERSION
    ):
        raise ValueError("approved retrieval semantics version is unsupported")
    return RetrievalCapabilityExpectation(
        evidence_schema_version=(
            RETRIEVAL_CAPABILITY_EVIDENCE_SCHEMA_VERSION
        ),
        retrieval_artifact_digest=require_digest(
            roots.get("artifact_digest"),
            "retrieval artifact_digest",
        ),
        retrieval_build_digest=require_digest(
            roots.get("build_digest"),
            "retrieval build_digest",
        ),
        semantics_version=RETRIEVAL_SEMANTICS_VERSION,
        fixture_digest=require_digest(
            roots.get("fixture_digest"),
            "retrieval fixture_digest",
        ),
        evaluator_digest=require_digest(
            roots.get("evaluator_digest"),
            "retrieval evaluator_digest",
        ),
        context_cohorts=_cohort_expectations(
            roots.get("context_cohorts"),
            "retrieval context_cohorts",
            expected_id=RETRIEVAL_CONTEXT_COHORT_ID,
        ),
        fuzzy_core_cohorts=_cohort_expectations(
            roots.get("fuzzy_core_cohorts"),
            "retrieval fuzzy_core_cohorts",
            expected_id=RETRIEVAL_FUZZY_CORE_COHORT_ID,
        ),
        fts5_trigram=_benchmark_expectation(
            roots.get("fts5_trigram"),
            "retrieval fts5_trigram",
            expected_path="FTS5_TRIGRAM",
        ),
        gram_fallback=_benchmark_expectation(
            roots.get("gram_fallback"),
            "retrieval gram_fallback",
            expected_path="GRAM_FALLBACK",
        ),
    )


def _cohort_expectations(
    value: object,
    field_name: str,
    *,
    expected_id: str,
) -> tuple[RetrievalCohortExpectation, ...]:
    raw = _require_mapping(value, field_name)
    if tuple(raw) != (expected_id,):
        raise ValueError(
            f"{field_name} must contain exactly {expected_id}"
        )
    return (
        RetrievalCohortExpectation(
            cohort_id=expected_id,
            cohort_digest=require_digest(
                raw[expected_id],
                f"{field_name} cohort digest",
            ),
        ),
    )


def _benchmark_expectation(
    value: object,
    field_name: str,
    *,
    expected_path: str,
) -> RetrievalBenchmarkExpectation:
    raw = _require_mapping(value, field_name)
    if set(raw) != {"contract_digest", "path"}:
        raise ValueError(f"{field_name} fields are not closed")
    path = _require_string(raw.get("path"), f"{field_name} path")
    if path != expected_path:
        raise ValueError(f"{field_name} path is unsupported")
    return RetrievalBenchmarkExpectation(
        path=expected_path,
        contract_digest=require_digest(
            raw.get("contract_digest"),
            f"{field_name} contract_digest",
        ),
    )


def _load_approved_roots(path: Path) -> dict[str, ValidationJsonValue]:
    if not isinstance(path, Path) or not path.is_file():
        raise ValueError("approved_roots_path must be an existing file")
    return _load_json(path, "retrieval roots")


def _load_json(path: Path, label: str) -> dict[str, ValidationJsonValue]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant in {label}: {value}")

    def reject_duplicates(
        pairs: list[tuple[str, ValidationJsonValue]],
    ) -> dict[str, ValidationJsonValue]:
        result: dict[str, ValidationJsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in {label}")
            result[key] = value
        return result

    try:
        loaded: object = cast(
            object,
            json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=reject_constant,
                object_pairs_hook=reject_duplicates,
            ),
        )
    except json.JSONDecodeError:
        raise ValueError(f"{label} JSON is invalid") from None
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} JSON root must be an object")
    return cast(dict[str, ValidationJsonValue], loaded)


def _observe_context_transcript(
    fixture_path: Path,
) -> list[dict[str, object]]:
    vectors = _load_context_vectors(fixture_path)
    transcript: list[dict[str, object]] = []
    for vector in vectors:
        vector_id = cast(str, vector["id"])
        query = _vector_query(
            _require_mapping(vector.get("query"), "vector query")
        )
        records = tuple(
            _vector_record(raw)
            for raw in _require_list(
                vector.get("records"),
                "vector records",
            )
        )
        classification = classify_exact_context(
            resource_id=_RESOURCE_ID,
            resource_order=0,
            query=query,
            records=records,
        )
        _verify_classification(
            classification,
            _require_mapping(
                vector.get("expected"),
                "vector expected",
            ),
            vector_id,
        )
        transcript.append(_transcript_entry(classification, vector_id))
    return transcript


def _observe_fuzzy_scoring_transcript(
    fixture_path: Path,
) -> list[dict[str, object]]:
    """Score the frozen fuzzy vectors and emit one body-safe transcript.

    Each vector constructs a coherent ``CandidateRetrievalReport`` from the
    fixture's conserved stage counts, runs only public
    ``score_fuzzy_candidates``, and verifies the observed scored count,
    accepted count, accepted order, similarity evidence and stable tie keys
    against the fixture expectations.  The transcript carries only vector
    ids, index kind, candidate stage names and counts, scored/accepted
    counts, accepted record ids, numeric similarity evidence and stable tie
    keys; it is observed here but is not yet the fuzzy-core cohort digest.
    """

    vectors = _load_fuzzy_scoring_vectors(fixture_path)
    transcript: list[dict[str, object]] = []
    for vector in vectors:
        vector_id = cast(str, vector["id"])
        query = _fuzzy_query(
            _require_mapping(
                vector.get("query"),
                "fuzzy vector query",
            ),
            vector_id,
        )
        records = tuple(
            _fuzzy_vector_record(raw, vector_id)
            for raw in _require_list(
                vector.get("records"),
                "fuzzy vector records",
            )
        )
        expected = _fuzzy_expected(
            _require_mapping(
                vector.get("expected"),
                "fuzzy vector expected",
            ),
            vector_id,
            record_ids=tuple(record.record_id for record in records),
        )
        report = _fuzzy_candidate_report(
            vector_id,
            query,
            records,
            expected,
        )
        result = score_fuzzy_candidates(
            resource_id=_RESOURCE_ID,
            resource_order=0,
            query=query,
            report=report,
            records=records,
        )
        _verify_fuzzy_scoring(result, expected, vector_id)
        transcript.append(_fuzzy_transcript_entry(result, report, vector_id))
    return transcript


def _load_context_vectors(
    path: Path,
) -> list[Mapping[str, ValidationJsonValue]]:
    payload = _load_fixture_payload(path)
    raw_vectors = _require_list(payload.get("vectors"), "context vectors")
    if not raw_vectors:
        raise ValueError("context vectors must not be empty")
    vectors: list[Mapping[str, ValidationJsonValue]] = []
    seen_ids: set[str] = set()
    for raw_vector in raw_vectors:
        vector = _require_mapping(raw_vector, "context vector")
        if set(vector) != {"expected", "id", "query", "records"}:
            raise ValueError("context vector fields are not closed")
        vector_id = _require_string(
            vector.get("id"),
            "context vector id",
        )
        if vector_id in seen_ids:
            raise ValueError("context vector ids must be unique")
        seen_ids.add(vector_id)
        vectors.append(vector)
    return vectors


def _load_fixture_payload(
    path: Path,
) -> dict[str, ValidationJsonValue]:
    payload = _load_json(path, "retrieval vectors")
    if set(payload) != {
        "fixture_version",
        "fuzzy",
        "semantics_version",
        "service",
        "store",
        "vectors",
    }:
        raise ValueError("retrieval vector fixture fields are not closed")
    if (
        _require_string(
            payload.get("fixture_version"),
            "retrieval vectors fixture_version",
        )
        != _FIXTURE_VERSION
    ):
        raise ValueError("unsupported retrieval vectors fixture version")
    if (
        _require_string(
            payload.get("semantics_version"),
            "retrieval vectors semantics_version",
        )
        != RETRIEVAL_SEMANTICS_VERSION
    ):
        raise ValueError("unsupported retrieval vectors semantics version")
    return payload


def _load_fuzzy_scoring_vectors(
    path: Path,
) -> list[Mapping[str, ValidationJsonValue]]:
    payload = _load_fixture_payload(path)
    fuzzy = _require_mapping(
        payload.get("fuzzy"),
        "fuzzy scoring section",
    )
    if set(fuzzy) != {"vectors", "version"}:
        raise ValueError("fuzzy scoring section fields are not closed")
    if (
        _require_string(
            fuzzy.get("version"),
            "fuzzy scoring section version",
        )
        != _FUZZY_SCORING_SECTION_VERSION
    ):
        raise ValueError("unsupported fuzzy scoring section version")
    raw_vectors = _require_list(
        fuzzy.get("vectors"),
        "fuzzy scoring vectors",
    )
    if not raw_vectors:
        raise ValueError("fuzzy scoring vectors must not be empty")
    vectors: list[Mapping[str, ValidationJsonValue]] = []
    seen_ids: set[str] = set()
    for raw_vector in raw_vectors:
        vector = _require_mapping(raw_vector, "fuzzy scoring vector")
        if set(vector) != {"expected", "id", "query", "records"}:
            raise ValueError("fuzzy scoring vector fields are not closed")
        vector_id = _require_string(
            vector.get("id"),
            "fuzzy scoring vector id",
        )
        if vector_id in seen_ids:
            raise ValueError("fuzzy scoring vector ids must be unique")
        seen_ids.add(vector_id)
        vectors.append(vector)
    return vectors


def _vector_query(
    raw: Mapping[str, ValidationJsonValue],
) -> TMQuery:
    if set(raw) != {
        "query_source",
        "speaker_raw",
        "context_prev_raw",
        "context_next_raw",
    }:
        raise ValueError("vector query fields are not closed")
    query_source = _require_string(
        raw.get("query_source"),
        "vector query_source",
    )
    if query_source != _QUERY_SOURCE:
        raise ValueError("vector query_source is unsupported")
    return TMQuery(
        query_source=query_source,
        speaker_raw=_require_optional_string(
            raw.get("speaker_raw"),
            "vector query speaker_raw",
        ),
        context_prev_raw=_require_optional_string(
            raw.get("context_prev_raw"),
            "vector query context_prev_raw",
        ),
        context_next_raw=_require_optional_string(
            raw.get("context_next_raw"),
            "vector query context_next_raw",
        ),
        minimum_similarity=_MINIMUM_SIMILARITY,
        limit=_LIMIT,
        resource_order=(_RESOURCE_ID,),
    )


def _vector_record(raw: object) -> TMRecord:
    record = _require_mapping(raw, "vector record")
    if set(record) != {
        "record_id",
        "source_raw",
        "target_raw",
        "speaker_raw",
        "context_prev_raw",
        "context_next_raw",
    }:
        raise ValueError("vector record fields are not closed")
    record_id = _require_integer(
        record.get("record_id"),
        "vector record_id",
    )
    if record_id < 1:
        raise ValueError("vector record_id must be positive")
    source_raw = _require_string(
        record.get("source_raw"),
        "vector source_raw",
    )
    if source_raw != _QUERY_SOURCE:
        raise ValueError("vector source_raw must match the query source")
    return TMRecord(
        record_id=record_id,
        source_raw=source_raw,
        target_raw=_require_string(
            record.get("target_raw"),
            "vector target_raw",
        ),
        speaker_raw=_require_optional_string(
            record.get("speaker_raw"),
            "vector speaker_raw",
        ),
        context_prev_raw=_require_optional_string(
            record.get("context_prev_raw"),
            "vector context_prev_raw",
        ),
        context_next_raw=_require_optional_string(
            record.get("context_next_raw"),
            "vector context_next_raw",
        ),
        file_source=None,
        provenance=(("origin", _ORIGIN_BATCH_ID),),
        legacy_line_no=None,
        origin_batch_id=_ORIGIN_BATCH_ID,
        origin_ordinal=record_id,
    )


def _verify_classification(
    classification: ExactContextClassification,
    expected: Mapping[str, ValidationJsonValue],
    vector_id: str,
) -> None:
    if set(expected) != {
        "winner_record_id",
        "context",
        "context_count",
        "retained_record_ids",
        "retained_count",
    }:
        raise ValueError("vector expected fields are not closed")
    winner = classification.winner
    if winner is None or winner.match_type is not TMMatchType.EXACT:
        raise ValueError(f"{vector_id}: winner diverged from approved fixture")
    if (
        winner.record_id
        != _require_integer(
            expected.get("winner_record_id"),
            "expected winner_record_id",
        )
    ):
        raise ValueError(f"{vector_id}: winner record id diverged")
    observed_context = classification.context_results
    expected_context = _require_list(
        expected.get("context"),
        "expected context",
    )
    if len(observed_context) != len(expected_context):
        raise ValueError(
            f"{vector_id}: context results diverged from approved fixture"
        )
    for result, raw_expected in zip(observed_context, expected_context):
        _verify_context_result(result, raw_expected, vector_id)
    expected_retained = _require_int_list(
        expected.get("retained_record_ids"),
        "expected retained_record_ids",
    )
    if expected_retained != tuple(
        record.record_id
        for record in classification.retained_only_variants
    ):
        raise ValueError(
            f"{vector_id}: retained records diverged from approved fixture"
        )
    if _require_integer(
        expected.get("context_count"),
        "expected context_count",
    ) != len(observed_context):
        raise ValueError(f"{vector_id}: context count diverged")
    if _require_integer(
        expected.get("retained_count"),
        "expected retained_count",
    ) != len(classification.retained_only_variants):
        raise ValueError(f"{vector_id}: retained count diverged")


def _verify_context_result(
    result: TMResult,
    raw_expected: object,
    vector_id: str,
) -> None:
    entry = _require_mapping(raw_expected, "expected context entry")
    if set(entry) != {
        "record_id",
        "match_type",
        "comparable_fields",
        "matched_fields",
        "mismatched_fields",
        "strength_v1",
    }:
        raise ValueError("expected context entry fields are not closed")
    if (
        result.record_id
        != _require_integer(entry.get("record_id"), "context record_id")
    ):
        raise ValueError(
            f"{vector_id}: context record order diverged from approved fixture"
        )
    if result.match_type is not TMMatchType.CONTEXT:
        raise ValueError(f"{vector_id}: context match type diverged")
    if (
        _require_string(
            entry.get("match_type"),
            "context match_type",
        )
        != "CONTEXT"
    ):
        raise ValueError(f"{vector_id}: context match type label diverged")
    evidence = result.context_evidence
    if _require_string_list(
        entry.get("comparable_fields"),
        "context comparable_fields",
    ) != evidence.comparable_fields:
        raise ValueError(f"{vector_id}: comparable fields diverged")
    if _require_string_list(
        entry.get("matched_fields"),
        "context matched_fields",
    ) != evidence.matched_fields:
        raise ValueError(f"{vector_id}: matched fields diverged")
    if _require_string_list(
        entry.get("mismatched_fields"),
        "context mismatched_fields",
    ) != evidence.mismatched_fields:
        raise ValueError(f"{vector_id}: mismatched fields diverged")
    if _require_int_list(
        entry.get("strength_v1"),
        "context strength_v1",
    ) != evidence.strength_v1:
        raise ValueError(f"{vector_id}: context strength diverged")


def _transcript_entry(
    classification: ExactContextClassification,
    vector_id: str,
) -> dict[str, object]:
    winner = classification.winner
    if winner is None or winner.match_type is not TMMatchType.EXACT:
        raise ValueError(f"{vector_id}: expected an exact winner")
    return {
        "id": vector_id,
        "winner_record_id": winner.record_id,
        "winner_match_type": winner.match_type.value,
        "context": [
            {
                "record_id": result.record_id,
                "match_type": result.match_type.value,
                "comparable_fields": list(
                    result.context_evidence.comparable_fields
                ),
                "matched_fields": list(
                    result.context_evidence.matched_fields
                ),
                "mismatched_fields": list(
                    result.context_evidence.mismatched_fields
                ),
                "strength_v1": list(result.context_evidence.strength_v1),
            }
            for result in classification.context_results
        ],
        "context_count": len(classification.context_results),
        "retained_record_ids": [
            record.record_id
            for record in classification.retained_only_variants
        ],
        "retained_count": len(classification.retained_only_variants),
    }


def _fuzzy_query(
    raw: Mapping[str, ValidationJsonValue],
    vector_id: str,
) -> TMQuery:
    if set(raw) != {
        "limit",
        "minimum_similarity",
        "query_source",
    }:
        raise ValueError("fuzzy vector query fields are not closed")
    return TMQuery(
        query_source=_require_string(
            raw.get("query_source"),
            "fuzzy vector query_source",
        ),
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        minimum_similarity=_require_ratio_number(
            raw.get("minimum_similarity"),
            "fuzzy vector minimum_similarity",
        ),
        limit=_require_integer(
            raw.get("limit"),
            "fuzzy vector query limit",
        ),
        resource_order=(_RESOURCE_ID,),
    )


def _fuzzy_vector_record(raw: object, vector_id: str) -> TMRecord:
    record = _require_mapping(raw, "fuzzy vector record")
    if set(record) != {
        "record_id",
        "source_raw",
        "target_raw",
        "speaker_raw",
        "context_prev_raw",
        "context_next_raw",
    }:
        raise ValueError("fuzzy vector record fields are not closed")
    record_id = _require_integer(
        record.get("record_id"),
        "fuzzy vector record_id",
    )
    if record_id < 1:
        raise ValueError("fuzzy vector record_id must be positive")
    return TMRecord(
        record_id=record_id,
        source_raw=_require_string(
            record.get("source_raw"),
            "fuzzy vector source_raw",
        ),
        target_raw=_require_string(
            record.get("target_raw"),
            "fuzzy vector target_raw",
        ),
        speaker_raw=_require_optional_string(
            record.get("speaker_raw"),
            "fuzzy vector speaker_raw",
        ),
        context_prev_raw=_require_optional_string(
            record.get("context_prev_raw"),
            "fuzzy vector context_prev_raw",
        ),
        context_next_raw=_require_optional_string(
            record.get("context_next_raw"),
            "fuzzy vector context_next_raw",
        ),
        file_source=None,
        provenance=(("origin", _FUZZY_ORIGIN_BATCH_ID),),
        legacy_line_no=None,
        origin_batch_id=_FUZZY_ORIGIN_BATCH_ID,
        origin_ordinal=record_id,
    )


def _fuzzy_expected(
    raw: Mapping[str, ValidationJsonValue],
    vector_id: str,
    *,
    record_ids: tuple[int, ...],
) -> dict[str, object]:
    if set(raw) != {
        "accepted",
        "accepted_count",
        "index_kind",
        "scored_count",
        "stages",
    }:
        raise ValueError("fuzzy vector expected fields are not closed")
    index_kind = _require_string(
        raw.get("index_kind"),
        "fuzzy expected index_kind",
    )
    if index_kind not in _FUZZY_INDEX_KINDS:
        raise ValueError("fuzzy expected index_kind is unsupported")
    stages = _fuzzy_stages(
        raw.get("stages"),
        vector_id,
    )
    scored_count = _require_integer(
        raw.get("scored_count"),
        "fuzzy expected scored_count",
    )
    if scored_count < 0:
        raise ValueError("fuzzy expected scored_count must be non-negative")
    accepted_count = _require_integer(
        raw.get("accepted_count"),
        "fuzzy expected accepted_count",
    )
    if accepted_count < 0:
        raise ValueError(
            "fuzzy expected accepted_count must be non-negative"
        )
    accepted = _fuzzy_accepted(
        raw.get("accepted"),
        vector_id,
        record_ids=record_ids,
    )
    if len(accepted) != accepted_count:
        raise ValueError(f"{vector_id}: fuzzy accepted count diverged")
    return {
        "index_kind": index_kind,
        "stages": stages,
        "scored_count": scored_count,
        "accepted_count": accepted_count,
        "accepted": accepted,
    }


def _fuzzy_stages(
    raw: object,
    vector_id: str,
) -> tuple[CandidateStageMetadata, ...]:
    entries = _require_list(raw, "fuzzy expected stages")
    if not entries:
        raise ValueError("fuzzy expected stages must not be empty")
    stages: list[CandidateStageMetadata] = []
    seen: set[CandidateStage] = set()
    for entry_raw in entries:
        entry = _require_mapping(entry_raw, "fuzzy expected stage")
        if set(entry) != {
            "stage",
            "input_count",
            "added_unique_count",
            "output_unique_count",
            "dropped_count",
        }:
            raise ValueError("fuzzy expected stage fields are not closed")
        stage_name = _require_string(
            entry.get("stage"),
            "fuzzy expected stage name",
        )
        try:
            stage = CandidateStage(stage_name)
        except ValueError:
            raise ValueError(
                f"{vector_id}: fuzzy stage name is unsupported"
            ) from None
        if stage in seen:
            raise ValueError(f"{vector_id}: fuzzy stages must not repeat")
        seen.add(stage)
        stages.append(
            CandidateStageMetadata(
                stage=stage,
                input_count=_require_integer(
                    entry.get("input_count"),
                    "fuzzy stage input_count",
                ),
                added_unique_count=_require_integer(
                    entry.get("added_unique_count"),
                    "fuzzy stage added_unique_count",
                ),
                output_unique_count=_require_integer(
                    entry.get("output_unique_count"),
                    "fuzzy stage output_unique_count",
                ),
                dropped_count=_require_integer(
                    entry.get("dropped_count"),
                    "fuzzy stage dropped_count",
                ),
            )
        )
    return tuple(stages)


def _fuzzy_accepted(
    raw: object,
    vector_id: str,
    *,
    record_ids: tuple[int, ...],
) -> tuple[dict[str, object], ...]:
    entries = _require_list(raw, "fuzzy accepted")
    known = set(record_ids)
    accepted: list[dict[str, object]] = []
    seen: set[int] = set()
    for entry_raw in entries:
        entry = _require_mapping(entry_raw, "fuzzy accepted entry")
        if set(entry) != {
            "record_id",
            "similarity",
            "similarity_evidence",
        }:
            raise ValueError("fuzzy accepted entry fields are not closed")
        record_id = _require_integer(
            entry.get("record_id"),
            "fuzzy accepted record_id",
        )
        if record_id in seen:
            raise ValueError(
                f"{vector_id}: fuzzy accepted record ids must be unique"
            )
        seen.add(record_id)
        if record_id not in known:
            raise ValueError(
                f"{vector_id}: fuzzy accepted record id is unknown"
            )
        similarity = _require_ratio_number(
            entry.get("similarity"),
            "fuzzy accepted similarity",
        )
        evidence = _require_mapping(
            entry.get("similarity_evidence"),
            "fuzzy accepted similarity_evidence",
        )
        if set(evidence) != {
            "levenshtein_ratio",
            "dice_bigram",
            "final_similarity",
            "scorer_version",
        }:
            raise ValueError(
                "fuzzy accepted similarity_evidence fields are not closed"
            )
        levenshtein_ratio = _require_ratio_number(
            evidence.get("levenshtein_ratio"),
            "fuzzy accepted levenshtein_ratio",
        )
        dice_bigram = _require_ratio_number(
            evidence.get("dice_bigram"),
            "fuzzy accepted dice_bigram",
        )
        final_similarity = _require_ratio_number(
            evidence.get("final_similarity"),
            "fuzzy accepted final_similarity",
        )
        if final_similarity != similarity:
            raise ValueError(
                f"{vector_id}: fuzzy accepted evidence diverged"
            )
        scorer_version = _require_string(
            evidence.get("scorer_version"),
            "fuzzy accepted scorer_version",
        )
        if scorer_version != SCORER_VERSION_V1:
            raise ValueError(
                f"{vector_id}: fuzzy accepted scorer version is unsupported"
            )
        accepted.append(
            {
                "record_id": record_id,
                "similarity": similarity,
                "levenshtein_ratio": levenshtein_ratio,
                "dice_bigram": dice_bigram,
                "final_similarity": final_similarity,
                "scorer_version": scorer_version,
            }
        )
    return tuple(accepted)


def _fuzzy_candidate_report(
    vector_id: str,
    query: TMQuery,
    records: tuple[TMRecord, ...],
    expected: Mapping[str, object],
) -> CandidateRetrievalReport:
    stages = cast(
        "tuple[CandidateStageMetadata, ...]",
        expected["stages"],
    )
    if stages[-1].output_unique_count != len(records):
        raise ValueError(
            f"{vector_id}: fuzzy final stage count must equal candidate count"
        )
    union_output = _fuzzy_stage_output(
        stages,
        CandidateStage.UNION,
        vector_id,
    )
    deduplicated_output = _fuzzy_stage_output(
        stages,
        CandidateStage.DEDUPLICATE,
        vector_id,
    )
    source_stages = tuple(
        stage_metadata.stage
        for stage_metadata in stages
        if stage_metadata.stage in _FUZZY_SOURCE_STAGES
    )
    metadata = CandidateRecallMetadata(
        resource_id=_RESOURCE_ID,
        index_kind=cast(str, expected["index_kind"]),
        fuzzy_available=True,
        fuzzy_unavailable_code=None,
        stages=stages,
        union_unique_count=union_output,
        deduplicated_count=deduplicated_output,
        result_limit=query.limit,
        candidate_budget_version=CANDIDATE_BUDGET_VERSION,
        candidate_budget=candidate_budget_v1(query.limit),
        truncated=False,
    )
    candidates = tuple(
        CandidateEvidence(
            record_id=record.record_id,
            recall_stages=source_stages,
            matched_grams=1,
            query_grams=1,
            overlap_ratio=1.0,
            pretruncate_rank=None,
        )
        for record in records
    )
    return CandidateRetrievalReport(
        candidates=candidates,
        metadata=metadata,
    )


def _fuzzy_stage_output(
    stages: tuple[CandidateStageMetadata, ...],
    stage: CandidateStage,
    vector_id: str,
) -> int:
    for stage_metadata in stages:
        if stage_metadata.stage is stage:
            return stage_metadata.output_unique_count
    raise ValueError(
        f"{vector_id}: fuzzy stages must include {stage.value}"
    )


def _verify_fuzzy_scoring(
    result: FuzzyScoringResult,
    expected: Mapping[str, object],
    vector_id: str,
) -> None:
    if result.scored_count != expected["scored_count"]:
        raise ValueError(f"{vector_id}: fuzzy scored count diverged")
    expected_accepted = cast(
        "tuple[dict[str, object], ...]",
        expected["accepted"],
    )
    if len(result.accepted) != len(expected_accepted):
        raise ValueError(f"{vector_id}: fuzzy accepted order diverged")
    if len(result.accepted) != expected["accepted_count"]:
        raise ValueError(f"{vector_id}: fuzzy accepted count diverged")
    for accepted, raw_expected in zip(result.accepted, expected_accepted):
        if (
            accepted.record_id
            != _require_integer(
                raw_expected.get("record_id"),
                "fuzzy accepted record_id",
            )
        ):
            raise ValueError(f"{vector_id}: fuzzy accepted order diverged")
        if (
            accepted.similarity
            != _require_ratio_number(
                raw_expected.get("similarity"),
                "fuzzy accepted similarity",
            )
        ):
            raise ValueError(
                f"{vector_id}: fuzzy accepted similarity diverged"
            )
        evidence = accepted.similarity_evidence
        if evidence is None:
            raise ValueError(
                f"{vector_id}: fuzzy accepted evidence is missing"
            )
        if (
            evidence.levenshtein_ratio
            != _require_ratio_number(
                raw_expected.get("levenshtein_ratio"),
                "fuzzy accepted levenshtein_ratio",
            )
        ):
            raise ValueError(
                f"{vector_id}: fuzzy accepted evidence diverged"
            )
        if (
            evidence.dice_bigram
            != _require_ratio_number(
                raw_expected.get("dice_bigram"),
                "fuzzy accepted dice_bigram",
            )
        ):
            raise ValueError(
                f"{vector_id}: fuzzy accepted evidence diverged"
            )
        if (
            evidence.final_similarity
            != _require_ratio_number(
                raw_expected.get("final_similarity"),
                "fuzzy accepted final_similarity",
            )
        ):
            raise ValueError(
                f"{vector_id}: fuzzy accepted evidence diverged"
            )
        if evidence.scorer_version != raw_expected.get("scorer_version"):
            raise ValueError(
                f"{vector_id}: fuzzy accepted scorer version diverged"
            )
        if accepted.stable_tie_key != (0, accepted.record_id):
            raise ValueError(
                f"{vector_id}: fuzzy accepted tie key diverged"
            )


def _fuzzy_transcript_entry(
    result: FuzzyScoringResult,
    report: CandidateRetrievalReport,
    vector_id: str,
) -> dict[str, object]:
    accepted_entries: list[dict[str, object]] = []
    for accepted in result.accepted:
        evidence = accepted.similarity_evidence
        if evidence is None:
            raise ValueError(
                f"{vector_id}: fuzzy accepted evidence is missing"
            )
        accepted_entries.append(
            {
                "record_id": accepted.record_id,
                "similarity": accepted.similarity,
                "similarity_evidence": {
                    "levenshtein_ratio": evidence.levenshtein_ratio,
                    "dice_bigram": evidence.dice_bigram,
                    "final_similarity": evidence.final_similarity,
                    "scorer_version": evidence.scorer_version,
                },
                "stable_tie_key": list(accepted.stable_tie_key),
            }
        )
    return {
        "id": vector_id,
        "index_kind": report.metadata.index_kind,
        "stages": [
            {
                "stage": stage_metadata.stage.value,
                "input_count": stage_metadata.input_count,
                "added_unique_count": stage_metadata.added_unique_count,
                "output_unique_count": stage_metadata.output_unique_count,
                "dropped_count": stage_metadata.dropped_count,
            }
            for stage_metadata in report.metadata.stages
        ],
        "scored_count": result.scored_count,
        "accepted_count": len(result.accepted),
        "accepted": accepted_entries,
    }


def _observe_store_transcript(
    fixture_path: Path,
) -> list[dict[str, object]]:
    """Execute one real temporary-store journey and emit one body-safe entry.

    The journey creates an isolated ``TemporaryDirectory``, initializes a
    public ``SQLiteTMStore`` stage, appends one fixed origin batch with an
    exact-source record, two near-source fuzzy records and one unrelated
    record, reads the public ``CandidateRetriever`` report and the
    read-only query-view report from the same store, scores the actual
    candidate report with public ``score_fuzzy_candidates``, duplicates the
    fixed batch id with a different draft so the public append transaction
    raises ``sqlite3.IntegrityError``, and proves through public revision,
    export, health, exact and candidate APIs that revision, exported
    records, record count and the candidate report are unchanged and the
    would-be new record is absent.  The emitted entry carries only fixture,
    resource, batch and record/candidate ids, runtime capability, revision,
    recall stages/counts/evidence, scoring counts and numeric values,
    before/after counts, the exact exception type and rollback equality
    booleans - never bodies, provenance, paths or exception text.
    """

    payload = _load_fixture_payload(fixture_path)
    section = _require_mapping(payload.get("store"), "store section")
    config = _store_config(section)
    expected = _store_expected(
        _require_mapping(section.get("expected"), "store expected"),
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        configured = (root / f"{config.resource_id}.jsonl").resolve()
        identity = CanonicalResourceIdentity.from_configured_jsonl(
            config.resource_id,
            configured,
        )
        stage = MutableStageRef(
            stage_id=config.stage_id,
            resource_identity=identity,
            staged_db_path=(root / ".stage.sqlite3").resolve(),
            manifest_temp_path=(root / ".stage.snapshot.tmp").resolve(),
        )
        snapshot = initialize_stage_schema(
            stage,
            canonical_store_id=config.canonical_store_id,
        )
        store = SQLiteTMStore(
            stage,
            canonical_store_id=config.canonical_store_id,
        )
        store.append_batch(
            batch_id=config.batch_id,
            kind=config.batch_kind,
            drafts=tuple(_store_draft(draft) for draft in config.drafts),
            source_digest=config.source_digest,
            source_path=(root / config.source_name).resolve(),
        )
        revision = store.canonical_revision()
        exported = tuple(store.export_records())
        health = store.health()
        branch = _store_branch(expected, health.index_kind)
        retriever = CandidateRetriever()
        report = retriever.candidates(
            config.resource_id,
            store,
            config.folded_query,
            result_limit=config.result_limit,
        )
        _verify_store_revision(revision, expected)
        _verify_store_exported(exported, expected)
        _verify_store_report(report, branch)
        with store.query_lease() as view:
            view_report = retriever.candidates_from_view(
                config.resource_id,
                view,
                config.folded_query,
                result_limit=config.result_limit,
            )
            parity = _store_parity(report, view_report)
            records = view.records_by_id(
                tuple(
                    candidate.record_id
                    for candidate in report.candidates
                )
            )
            scoring = score_fuzzy_candidates(
                resource_id=config.resource_id,
                resource_order=0,
                query=_store_query(config),
                report=report,
                records=records,
            )
            _verify_store_scoring(scoring, branch)
        before_revision = store.canonical_revision()
        before_exported = tuple(store.export_records())
        before_health = store.health()
        before_report = retriever.candidates(
            config.resource_id,
            store,
            config.folded_query,
            result_limit=config.result_limit,
        )
        try:
            store.append_batch(
                batch_id=config.batch_id,
                kind=config.batch_kind,
                drafts=(_store_draft(config.duplicate_draft),),
                source_digest=config.duplicate_source_digest,
                source_path=(
                    root / config.duplicate_source_name
                ).resolve(),
            )
        except Exception as error:
            exception_type = (
                f"{type(error).__module__}.{type(error).__qualname__}"
            )
        else:
            raise ValueError(
                "store duplicate batch must raise sqlite3.IntegrityError"
            )
        after_revision = store.canonical_revision()
        after_exported = tuple(store.export_records())
        after_health = store.health()
        after_report = retriever.candidates(
            config.resource_id,
            store,
            config.folded_query,
            result_limit=config.result_limit,
        )
        with store.query_lease() as view:
            exact_absent = (
                view.exact_records(config.duplicate_draft.source_raw)
                == ()
            )
        facts = _store_rollback_facts(
            config=config,
            exception_type=exception_type,
            before_revision=before_revision,
            before_exported=before_exported,
            before_health=before_health,
            before_report=before_report,
            after_revision=after_revision,
            after_exported=after_exported,
            after_health=after_health,
            after_report=after_report,
            exact_absent=exact_absent,
        )
        _verify_store_rollback(facts, expected)
        transcript = _store_transcript_entry(
            config=config,
            fts5_available=snapshot.fts5_available,
            schema_version=health.schema_version,
            revision=revision,
            exported=exported,
            report=report,
            parity=parity,
            scoring=scoring,
            facts=facts,
        )
    return [transcript]


def _store_config(
    section: Mapping[str, ValidationJsonValue],
) -> _StoreConfig:
    if set(section) != {
        "batch_id",
        "batch_kind",
        "canonical_store_id",
        "duplicate_draft",
        "duplicate_source_digest",
        "duplicate_source_name",
        "drafts",
        "expected",
        "folded_query",
        "id",
        "minimum_similarity",
        "query_source",
        "resource_id",
        "result_limit",
        "source_digest",
        "source_name",
        "stage_id",
        "version",
    }:
        raise ValueError("store section fields are not closed")
    if (
        _require_string(
            section.get("version"),
            "store version",
        )
        != _STORE_SECTION_VERSION
    ):
        raise ValueError("unsupported store section version")
    section_id = _require_string(section.get("id"), "store id")
    if not section_id.strip():
        raise ValueError("store id must not be empty")
    resource_id = _require_string(
        section.get("resource_id"),
        "store resource_id",
    )
    if not resource_id.strip():
        raise ValueError("store resource_id must not be empty")
    canonical_store_id = _require_string(
        section.get("canonical_store_id"),
        "store canonical_store_id",
    )
    if not canonical_store_id.strip():
        raise ValueError("store canonical_store_id must not be empty")
    stage_id = _require_string(section.get("stage_id"), "store stage_id")
    if not stage_id.strip():
        raise ValueError("store stage_id must not be empty")
    batch_id = _require_string(section.get("batch_id"), "store batch_id")
    if not batch_id.strip():
        raise ValueError("store batch_id must not be empty")
    batch_kind = _require_string(
        section.get("batch_kind"),
        "store batch_kind",
    )
    if batch_kind != "import":
        raise ValueError("store batch_kind is unsupported")
    source_digest = require_digest(
        section.get("source_digest"),
        "store source_digest",
    )
    duplicate_source_digest = require_digest(
        section.get("duplicate_source_digest"),
        "store duplicate_source_digest",
    )
    source_name = _require_string(
        section.get("source_name"),
        "store source_name",
    )
    if not source_name.strip():
        raise ValueError("store source_name must not be empty")
    duplicate_source_name = _require_string(
        section.get("duplicate_source_name"),
        "store duplicate_source_name",
    )
    if not duplicate_source_name.strip():
        raise ValueError("store duplicate_source_name must not be empty")
    query_source = _require_string(
        section.get("query_source"),
        "store query_source",
    )
    if not query_source.strip():
        raise ValueError("store query_source must not be empty")
    folded_query = _require_string(
        section.get("folded_query"),
        "store folded_query",
    )
    if not folded_query.strip():
        raise ValueError("store folded_query must not be empty")
    minimum_similarity = _require_ratio_number(
        section.get("minimum_similarity"),
        "store minimum_similarity",
    )
    result_limit = _require_integer(
        section.get("result_limit"),
        "store result_limit",
    )
    if result_limit < 1:
        raise ValueError("store result_limit must be positive")
    drafts = _store_drafts(
        _require_list(section.get("drafts"), "store drafts"),
    )
    duplicate_draft = _store_draft_config(
        _require_mapping(
            section.get("duplicate_draft"),
            "store duplicate_draft",
        ),
    )
    if duplicate_draft.ordinal != len(drafts):
        raise ValueError(
            "store duplicate_draft ordinal must follow the fixed batch"
        )
    return _StoreConfig(
        id=section_id,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        stage_id=stage_id,
        batch_id=batch_id,
        batch_kind=batch_kind,
        source_digest=source_digest,
        source_name=source_name,
        duplicate_source_digest=duplicate_source_digest,
        duplicate_source_name=duplicate_source_name,
        query_source=query_source,
        folded_query=folded_query,
        minimum_similarity=minimum_similarity,
        result_limit=result_limit,
        drafts=drafts,
        duplicate_draft=duplicate_draft,
    )


def _store_drafts(
    raw: list[ValidationJsonValue],
) -> tuple[_StoreDraftConfig, ...]:
    if not raw:
        raise ValueError("store drafts must not be empty")
    drafts: list[_StoreDraftConfig] = []
    seen_ordinals: set[int] = set()
    for index, entry_raw in enumerate(raw):
        entry = _store_draft_config(
            _require_mapping(entry_raw, "store draft")
        )
        if entry.ordinal in seen_ordinals:
            raise ValueError("store draft ordinals must be unique")
        seen_ordinals.add(entry.ordinal)
        if entry.ordinal != index:
            raise ValueError("store draft ordinals must be contiguous")
        drafts.append(entry)
    return tuple(drafts)


def _store_draft_config(
    raw: Mapping[str, ValidationJsonValue],
) -> _StoreDraftConfig:
    if set(raw) != {"ordinal", "source_raw", "target_raw"}:
        raise ValueError("store draft fields are not closed")
    ordinal = _require_integer(raw.get("ordinal"), "store draft ordinal")
    if ordinal < 0:
        raise ValueError("store draft ordinal must be non-negative")
    source_raw = _require_string(
        raw.get("source_raw"),
        "store draft source_raw",
    )
    if not source_raw:
        raise ValueError("store draft source_raw must not be empty")
    target_raw = _require_string(
        raw.get("target_raw"),
        "store draft target_raw",
    )
    if not target_raw:
        raise ValueError("store draft target_raw must not be empty")
    return _StoreDraftConfig(
        ordinal=ordinal,
        source_raw=source_raw,
        target_raw=target_raw,
    )


def _store_draft(config: _StoreDraftConfig) -> TMRecordDraft:
    return TMRecordDraft(
        source_raw=config.source_raw,
        target_raw=config.target_raw,
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        file_source=None,
        provenance=(_STORE_ORIGIN_PROVENANCE,),
    )


def _store_expected(
    raw: Mapping[str, ValidationJsonValue],
) -> dict[str, object]:
    if set(raw) != {
        "by_runtime",
        "exported_record_ids",
        "generation",
        "head_revision",
        "record_count",
        "rollback",
    }:
        raise ValueError("store expected fields are not closed")
    generation = _require_integer(
        raw.get("generation"),
        "store expected generation",
    )
    if generation < 0:
        raise ValueError("store expected generation must be non-negative")
    head_revision = _require_integer(
        raw.get("head_revision"),
        "store expected head_revision",
    )
    if head_revision < 0:
        raise ValueError("store expected head_revision must be non-negative")
    record_count = _require_integer(
        raw.get("record_count"),
        "store expected record_count",
    )
    if record_count < 0:
        raise ValueError("store expected record_count must be non-negative")
    exported_record_ids = _require_int_list(
        raw.get("exported_record_ids"),
        "store expected exported_record_ids",
    )
    if any(record_id < 1 for record_id in exported_record_ids):
        raise ValueError(
            "store expected exported_record_ids must be positive"
        )
    if tuple(exported_record_ids) != tuple(sorted(exported_record_ids)):
        raise ValueError(
            "store expected exported_record_ids must be sorted"
        )
    if len(exported_record_ids) != len(set(exported_record_ids)):
        raise ValueError(
            "store expected exported_record_ids must be unique"
        )
    by_runtime_raw = _require_mapping(
        raw.get("by_runtime"),
        "store expected by_runtime",
    )
    if tuple(by_runtime_raw) != _FUZZY_INDEX_KINDS:
        raise ValueError(
            "store expected by_runtime must contain both index kinds"
        )
    by_runtime = {
        index_kind: _store_expected_branch(
            _require_mapping(
                by_runtime_raw.get(index_kind),
                f"store expected {index_kind} branch",
            ),
        )
        for index_kind in _FUZZY_INDEX_KINDS
    }
    rollback = _store_expected_rollback(
        _require_mapping(
            raw.get("rollback"),
            "store expected rollback",
        )
    )
    return {
        "generation": generation,
        "head_revision": head_revision,
        "record_count": record_count,
        "exported_record_ids": exported_record_ids,
        "by_runtime": by_runtime,
        "rollback": rollback,
    }


def _store_expected_branch(
    raw: Mapping[str, ValidationJsonValue],
) -> dict[str, object]:
    if set(raw) != {
        "candidates",
        "deduplicated_count",
        "scoring",
        "stages",
        "truncated",
        "union_unique_count",
    }:
        raise ValueError("store expected branch fields are not closed")
    stages = _store_expected_stages(
        _require_list(raw.get("stages"), "store expected stages")
    )
    union_unique_count = _require_integer(
        raw.get("union_unique_count"),
        "store expected union_unique_count",
    )
    if union_unique_count < 0:
        raise ValueError(
            "store expected union_unique_count must be non-negative"
        )
    deduplicated_count = _require_integer(
        raw.get("deduplicated_count"),
        "store expected deduplicated_count",
    )
    if deduplicated_count < 0:
        raise ValueError(
            "store expected deduplicated_count must be non-negative"
        )
    truncated = _require_bool(
        raw.get("truncated"),
        "store expected truncated",
    )
    candidates = _store_expected_candidates(
        _require_list(
            raw.get("candidates"),
            "store expected candidates",
        )
    )
    scoring = _store_expected_scoring(
        _require_mapping(
            raw.get("scoring"),
            "store expected scoring",
        )
    )
    if stages[-1]["output_unique_count"] != len(candidates):
        raise ValueError(
            "store expected final stage count must equal candidate count"
        )
    return {
        "stages": stages,
        "union_unique_count": union_unique_count,
        "deduplicated_count": deduplicated_count,
        "truncated": truncated,
        "candidates": candidates,
        "scoring": scoring,
    }


def _store_expected_stages(
    raw: list[ValidationJsonValue],
) -> tuple[dict[str, object], ...]:
    if not raw:
        raise ValueError("store expected stages must not be empty")
    stages: list[dict[str, object]] = []
    seen: set[CandidateStage] = set()
    for entry_raw in raw:
        entry = _require_mapping(entry_raw, "store expected stage")
        if set(entry) != {
            "stage",
            "input_count",
            "added_unique_count",
            "output_unique_count",
            "dropped_count",
        }:
            raise ValueError("store expected stage fields are not closed")
        stage_name = _require_string(
            entry.get("stage"),
            "store expected stage name",
        )
        try:
            stage = CandidateStage(stage_name)
        except ValueError:
            raise ValueError(
                "store expected stage name is unsupported"
            ) from None
        if stage in seen:
            raise ValueError("store expected stages must not repeat")
        seen.add(stage)
        input_count = _require_integer(
            entry.get("input_count"),
            "store expected stage input_count",
        )
        added_unique_count = _require_integer(
            entry.get("added_unique_count"),
            "store expected stage added_unique_count",
        )
        output_unique_count = _require_integer(
            entry.get("output_unique_count"),
            "store expected stage output_unique_count",
        )
        dropped_count = _require_integer(
            entry.get("dropped_count"),
            "store expected stage dropped_count",
        )
        if any(
            count < 0
            for count in (
                input_count,
                added_unique_count,
                output_unique_count,
                dropped_count,
            )
        ):
            raise ValueError("store expected stage counts are invalid")
        if (
            input_count + added_unique_count - dropped_count
            != output_unique_count
        ):
            raise ValueError("store expected stage counts are not conserved")
        stages.append(
            {
                "stage": stage.value,
                "input_count": input_count,
                "added_unique_count": added_unique_count,
                "output_unique_count": output_unique_count,
                "dropped_count": dropped_count,
            }
        )
    return tuple(stages)


def _store_expected_candidates(
    raw: list[ValidationJsonValue],
) -> tuple[dict[str, object], ...]:
    if not raw:
        raise ValueError("store expected candidates must not be empty")
    candidates: list[dict[str, object]] = []
    seen_ids: set[int] = set()
    for entry_raw in raw:
        entry = _require_mapping(entry_raw, "store expected candidate")
        if set(entry) != {
            "record_id",
            "recall_stages",
            "matched_grams",
            "query_grams",
            "overlap_ratio",
            "pretruncate_rank",
        }:
            raise ValueError(
                "store expected candidate fields are not closed"
            )
        record_id = _require_integer(
            entry.get("record_id"),
            "store expected candidate record_id",
        )
        if record_id < 1:
            raise ValueError(
                "store expected candidate record_id must be positive"
            )
        if record_id in seen_ids:
            raise ValueError(
                "store expected candidate record ids must be unique"
            )
        seen_ids.add(record_id)
        recall_stages = tuple(
            _store_recall_stage(raw_stage)
            for raw_stage in _require_list(
                entry.get("recall_stages"),
                "store expected candidate recall_stages",
            )
        )
        matched_grams = _require_integer(
            entry.get("matched_grams"),
            "store expected candidate matched_grams",
        )
        query_grams = _require_integer(
            entry.get("query_grams"),
            "store expected candidate query_grams",
        )
        if matched_grams < 1 or query_grams < 1:
            raise ValueError(
                "store expected candidate gram counts are invalid"
            )
        if matched_grams > query_grams:
            raise ValueError(
                "store expected candidate matched_grams exceeds query_grams"
            )
        overlap_ratio = _require_ratio_number(
            entry.get("overlap_ratio"),
            "store expected candidate overlap_ratio",
        )
        pretruncate_rank = entry.get("pretruncate_rank")
        if pretruncate_rank is not None:
            pretruncate_rank = _require_integer(
                pretruncate_rank,
                "store expected candidate pretruncate_rank",
            )
            if pretruncate_rank < 1:
                raise ValueError(
                    "store expected candidate pretruncate_rank is invalid"
                )
        candidates.append(
            {
                "record_id": record_id,
                "recall_stages": recall_stages,
                "matched_grams": matched_grams,
                "query_grams": query_grams,
                "overlap_ratio": overlap_ratio,
                "pretruncate_rank": pretruncate_rank,
            }
        )
    return tuple(candidates)


def _store_recall_stage(raw: object) -> CandidateStage:
    name = _require_string(
        raw,
        "store expected candidate recall stage",
    )
    try:
        stage = CandidateStage(name)
    except ValueError:
        raise ValueError(
            "store expected candidate recall stage is unsupported"
        ) from None
    if stage not in _FUZZY_SOURCE_STAGES:
        raise ValueError(
            "store expected candidate recall stage is not a source stage"
        )
    return stage


def _store_expected_scoring(
    raw: Mapping[str, ValidationJsonValue],
) -> dict[str, object]:
    if set(raw) != {"accepted", "accepted_count", "scored_count"}:
        raise ValueError("store expected scoring fields are not closed")
    scored_count = _require_integer(
        raw.get("scored_count"),
        "store expected scored_count",
    )
    if scored_count < 0:
        raise ValueError(
            "store expected scored_count must be non-negative"
        )
    accepted_count = _require_integer(
        raw.get("accepted_count"),
        "store expected accepted_count",
    )
    if accepted_count < 0:
        raise ValueError(
            "store expected accepted_count must be non-negative"
        )
    accepted = _store_expected_accepted(
        _require_list(raw.get("accepted"), "store expected accepted")
    )
    if len(accepted) != accepted_count:
        raise ValueError("store expected accepted count diverged")
    return {
        "scored_count": scored_count,
        "accepted_count": accepted_count,
        "accepted": accepted,
    }


def _store_expected_accepted(
    raw: list[ValidationJsonValue],
) -> tuple[dict[str, object], ...]:
    accepted: list[dict[str, object]] = []
    seen_ids: set[int] = set()
    for entry_raw in raw:
        entry = _require_mapping(entry_raw, "store expected accepted")
        if set(entry) != {
            "record_id",
            "similarity",
            "levenshtein_ratio",
            "dice_bigram",
            "final_similarity",
        }:
            raise ValueError(
                "store expected accepted fields are not closed"
            )
        record_id = _require_integer(
            entry.get("record_id"),
            "store expected accepted record_id",
        )
        if record_id in seen_ids:
            raise ValueError(
                "store expected accepted record ids must be unique"
            )
        seen_ids.add(record_id)
        similarity = _require_ratio_number(
            entry.get("similarity"),
            "store expected accepted similarity",
        )
        levenshtein_ratio = _require_ratio_number(
            entry.get("levenshtein_ratio"),
            "store expected accepted levenshtein_ratio",
        )
        dice_bigram = _require_ratio_number(
            entry.get("dice_bigram"),
            "store expected accepted dice_bigram",
        )
        final_similarity = _require_ratio_number(
            entry.get("final_similarity"),
            "store expected accepted final_similarity",
        )
        if final_similarity != similarity:
            raise ValueError("store expected accepted evidence diverged")
        accepted.append(
            {
                "record_id": record_id,
                "similarity": similarity,
                "levenshtein_ratio": levenshtein_ratio,
                "dice_bigram": dice_bigram,
                "final_similarity": final_similarity,
            }
        )
    return tuple(accepted)


def _store_expected_rollback(
    raw: Mapping[str, ValidationJsonValue],
) -> dict[str, object]:
    if set(raw) != {
        "absent",
        "absent_record_id",
        "candidate_count_after",
        "candidate_count_before",
        "candidates_unchanged",
        "exception_type",
        "exported_count_after",
        "exported_count_before",
        "exported_unchanged",
        "health_unchanged",
        "record_count_after",
        "record_count_before",
        "revision_unchanged",
    }:
        raise ValueError("store expected rollback fields are not closed")
    exception_type = _require_string(
        raw.get("exception_type"),
        "store expected rollback exception_type",
    )
    if not exception_type:
        raise ValueError("store expected rollback exception_type is empty")
    absent_record_id = _require_integer(
        raw.get("absent_record_id"),
        "store expected rollback absent_record_id",
    )
    if absent_record_id < 1:
        raise ValueError(
            "store expected rollback absent_record_id must be positive"
        )
    counts: dict[str, int] = {}
    for field_name in (
        "record_count_before",
        "record_count_after",
        "exported_count_before",
        "exported_count_after",
        "candidate_count_before",
        "candidate_count_after",
    ):
        count = _require_integer(
            raw.get(field_name),
            f"store expected rollback {field_name}",
        )
        if count < 0:
            raise ValueError(
                f"store expected rollback {field_name} must be non-negative"
            )
        counts[field_name] = count
    booleans: dict[str, bool] = {}
    for field_name in (
        "revision_unchanged",
        "exported_unchanged",
        "health_unchanged",
        "candidates_unchanged",
        "absent",
    ):
        booleans[field_name] = _require_bool(
            raw.get(field_name),
            f"store expected rollback {field_name}",
        )
    return {
        "exception_type": exception_type,
        "absent_record_id": absent_record_id,
        **counts,
        **booleans,
    }


def _store_branch(
    expected: Mapping[str, object],
    index_kind: str,
) -> dict[str, object]:
    by_runtime = cast(
        "dict[str, dict[str, object]]",
        expected["by_runtime"],
    )
    branch = by_runtime.get(index_kind)
    if branch is None:
        raise ValueError("store runtime index kind is unsupported")
    return branch


def _store_query(config: _StoreConfig) -> TMQuery:
    return TMQuery(
        query_source=config.query_source,
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        minimum_similarity=config.minimum_similarity,
        limit=config.result_limit,
        resource_order=(config.resource_id,),
    )


def _verify_store_revision(
    revision: CanonicalRevisionSnapshot,
    expected: Mapping[str, object],
) -> None:
    if (
        revision.generation != expected["generation"]
        or revision.head_revision != expected["head_revision"]
        or revision.record_count != expected["record_count"]
    ):
        raise ValueError("store revision diverged from approved fixture")


def _verify_store_exported(
    exported: tuple[TMRecord, ...],
    expected: Mapping[str, object],
) -> None:
    if tuple(record.record_id for record in exported) != expected[
        "exported_record_ids"
    ]:
        raise ValueError("store exported records diverged from approved fixture")


def _verify_store_report(
    report: CandidateRetrievalReport,
    branch: Mapping[str, object],
) -> None:
    expected_stages = cast(
        "tuple[dict[str, object], ...]",
        branch["stages"],
    )
    if len(report.metadata.stages) != len(expected_stages):
        raise ValueError("store candidate stages diverged")
    for stage_metadata, raw_expected in zip(
        report.metadata.stages,
        expected_stages,
    ):
        if (
            stage_metadata.stage.value != raw_expected["stage"]
            or stage_metadata.input_count
            != raw_expected["input_count"]
            or stage_metadata.added_unique_count
            != raw_expected["added_unique_count"]
            or stage_metadata.output_unique_count
            != raw_expected["output_unique_count"]
            or stage_metadata.dropped_count
            != raw_expected["dropped_count"]
        ):
            raise ValueError("store candidate stages diverged")
    if (
        report.metadata.union_unique_count
        != branch["union_unique_count"]
        or report.metadata.deduplicated_count
        != branch["deduplicated_count"]
        or report.metadata.truncated != branch["truncated"]
    ):
        raise ValueError("store candidate metadata diverged")
    expected_candidates = cast(
        "tuple[dict[str, object], ...]",
        branch["candidates"],
    )
    if len(report.candidates) != len(expected_candidates):
        raise ValueError("store candidate identities diverged")
    for candidate, raw_expected in zip(
        report.candidates,
        expected_candidates,
    ):
        if (
            candidate.record_id != raw_expected["record_id"]
            or candidate.recall_stages
            != raw_expected["recall_stages"]
        ):
            raise ValueError("store candidate identities diverged")
        if (
            candidate.matched_grams != raw_expected["matched_grams"]
            or candidate.query_grams != raw_expected["query_grams"]
            or candidate.overlap_ratio != raw_expected["overlap_ratio"]
            or candidate.pretruncate_rank
            != raw_expected["pretruncate_rank"]
        ):
            raise ValueError("store candidate evidence diverged")


def _store_parity(
    report: CandidateRetrievalReport,
    view_report: CandidateRetrievalReport,
) -> dict[str, bool]:
    return {
        "candidate_report_equal": report == view_report,
        "metadata_equal": report.metadata == view_report.metadata,
        "candidate_identities_equal": tuple(
            candidate.record_id for candidate in report.candidates
        )
        == tuple(
            candidate.record_id for candidate in view_report.candidates
        ),
        "stages_equal": (
            report.metadata.stages == view_report.metadata.stages
        ),
    }


def _verify_store_parity(parity: Mapping[str, bool]) -> None:
    if not all(parity.values()):
        raise ValueError("store query-view parity diverged")


def _verify_store_scoring(
    scoring: FuzzyScoringResult,
    branch: Mapping[str, object],
) -> None:
    expected = cast("dict[str, object]", branch["scoring"])
    if scoring.scored_count != expected["scored_count"]:
        raise ValueError("store scoring scored count diverged")
    expected_accepted = cast(
        "tuple[dict[str, object], ...]",
        expected["accepted"],
    )
    if len(scoring.accepted) != expected["accepted_count"]:
        raise ValueError("store scoring accepted count diverged")
    if len(scoring.accepted) != len(expected_accepted):
        raise ValueError("store scoring accepted order diverged")
    for accepted, raw_expected in zip(scoring.accepted, expected_accepted):
        if accepted.record_id != raw_expected["record_id"]:
            raise ValueError("store scoring accepted order diverged")
        if accepted.similarity != raw_expected["similarity"]:
            raise ValueError("store scoring accepted similarity diverged")
        evidence = accepted.similarity_evidence
        if evidence is None:
            raise ValueError("store scoring accepted evidence is missing")
        if (
            evidence.levenshtein_ratio
            != raw_expected["levenshtein_ratio"]
            or evidence.dice_bigram != raw_expected["dice_bigram"]
            or evidence.final_similarity
            != raw_expected["final_similarity"]
        ):
            raise ValueError("store scoring accepted evidence diverged")


def _store_rollback_facts(
    *,
    config: _StoreConfig,
    exception_type: str,
    before_revision: CanonicalRevisionSnapshot,
    before_exported: tuple[TMRecord, ...],
    before_health: StoreHealth,
    before_report: CandidateRetrievalReport,
    after_revision: CanonicalRevisionSnapshot,
    after_exported: tuple[TMRecord, ...],
    after_health: StoreHealth,
    after_report: CandidateRetrievalReport,
    exact_absent: bool,
) -> dict[str, object]:
    return {
        "batch_id": config.batch_id,
        "exception_type": exception_type,
        "revision_unchanged": before_revision == after_revision,
        "exported_unchanged": before_exported == after_exported,
        "health_unchanged": before_health == after_health,
        "candidates_unchanged": before_report == after_report,
        "record_count_before": before_revision.record_count,
        "record_count_after": after_revision.record_count,
        "exported_count_before": len(before_exported),
        "exported_count_after": len(after_exported),
        "candidate_count_before": len(before_report.candidates),
        "candidate_count_after": len(after_report.candidates),
        "absent_record_id": len(before_exported) + 1,
        "absent": (
            (len(before_exported) + 1) not in after_exported
        )
        and exact_absent,
    }


def _verify_store_rollback(
    facts: Mapping[str, object],
    expected: Mapping[str, object],
) -> None:
    rollback = cast(
        "dict[str, object]",
        expected["rollback"],
    )
    if facts["exception_type"] != rollback["exception_type"]:
        raise ValueError("store rollback exception type diverged")
    if facts["absent_record_id"] != rollback["absent_record_id"]:
        raise ValueError("store rollback absent record id diverged")
    for field_name in (
        "revision_unchanged",
        "exported_unchanged",
        "health_unchanged",
        "candidates_unchanged",
        "absent",
    ):
        if facts[field_name] != rollback[field_name]:
            raise ValueError("store rollback equality diverged")
    for field_name in (
        "record_count_before",
        "record_count_after",
        "exported_count_before",
        "exported_count_after",
        "candidate_count_before",
        "candidate_count_after",
    ):
        if facts[field_name] != rollback[field_name]:
            raise ValueError("store rollback counts diverged")


def _store_scoring_entry(
    accepted: TMResult,
) -> dict[str, object]:
    evidence = accepted.similarity_evidence
    if evidence is None:
        raise ValueError("store scoring accepted evidence is missing")
    return {
        "record_id": accepted.record_id,
        "similarity": accepted.similarity,
        "similarity_evidence": {
            "levenshtein_ratio": evidence.levenshtein_ratio,
            "dice_bigram": evidence.dice_bigram,
            "final_similarity": evidence.final_similarity,
        },
    }


def _store_transcript_entry(
    *,
    config: _StoreConfig,
    fts5_available: bool,
    schema_version: int,
    revision: CanonicalRevisionSnapshot,
    exported: tuple[TMRecord, ...],
    report: CandidateRetrievalReport,
    parity: Mapping[str, bool],
    scoring: FuzzyScoringResult,
    facts: Mapping[str, object],
) -> dict[str, object]:
    _verify_store_parity(parity)
    return {
        "id": config.id,
        "resource_id": config.resource_id,
        "canonical_store_id": config.canonical_store_id,
        "batch_id": config.batch_id,
        "runtime": {
            "fts5_available": fts5_available,
            "index_kind": report.metadata.index_kind,
            "schema_version": schema_version,
        },
        "revision": {
            "generation": revision.generation,
            "head_revision": revision.head_revision,
            "record_count": revision.record_count,
        },
        "exported_record_ids": [
            record.record_id for record in exported
        ],
        "candidate_report": {
            "index_kind": report.metadata.index_kind,
            "result_limit": report.metadata.result_limit,
            "candidate_budget": report.metadata.candidate_budget,
            "union_unique_count": report.metadata.union_unique_count,
            "deduplicated_count": report.metadata.deduplicated_count,
            "truncated": report.metadata.truncated,
            "stages": [
                {
                    "stage": stage_metadata.stage.value,
                    "input_count": stage_metadata.input_count,
                    "added_unique_count": (
                        stage_metadata.added_unique_count
                    ),
                    "output_unique_count": (
                        stage_metadata.output_unique_count
                    ),
                    "dropped_count": stage_metadata.dropped_count,
                }
                for stage_metadata in report.metadata.stages
            ],
            "candidates": [
                {
                    "record_id": candidate.record_id,
                    "recall_stages": [
                        stage.value for stage in candidate.recall_stages
                    ],
                    "matched_grams": candidate.matched_grams,
                    "query_grams": candidate.query_grams,
                    "overlap_ratio": candidate.overlap_ratio,
                    "pretruncate_rank": candidate.pretruncate_rank,
                }
                for candidate in report.candidates
            ],
        },
        "query_view_parity": dict(parity),
        "scoring": {
            "scored_count": scoring.scored_count,
            "accepted_count": len(scoring.accepted),
            "accepted": [
                _store_scoring_entry(accepted)
                for accepted in scoring.accepted
            ],
        },
        "rollback": {
            "batch_id": facts["batch_id"],
            "exception_type": facts["exception_type"],
            "revision_unchanged": facts["revision_unchanged"],
            "exported_unchanged": facts["exported_unchanged"],
            "health_unchanged": facts["health_unchanged"],
            "candidates_unchanged": facts["candidates_unchanged"],
            "record_count_before": facts["record_count_before"],
            "record_count_after": facts["record_count_after"],
            "exported_count_before": facts["exported_count_before"],
            "exported_count_after": facts["exported_count_after"],
            "candidate_count_before": facts["candidate_count_before"],
            "candidate_count_after": facts["candidate_count_after"],
            "absent_record_id": facts["absent_record_id"],
            "absent": facts["absent"],
        },
    }


def _service_config(
    section: Mapping[str, ValidationJsonValue],
) -> dict[str, object]:
    if set(section) != {"query", "resources", "scenarios", "version"}:
        raise ValueError("service section fields are not closed")
    if (
        _require_string(
            section.get("version"),
            "service version",
        )
        != _SERVICE_SECTION_VERSION
    ):
        raise ValueError("unsupported service section version")
    resources = tuple(
        _service_resource_config(
            _require_mapping(raw, "service resource")
        )
        for raw in _require_list(
            section.get("resources"),
            "service resources",
        )
    )
    resource_ids = tuple(resource.resource_id for resource in resources)
    if len(resource_ids) != len(set(resource_ids)):
        raise ValueError("service resource ids must be unique")
    scenarios = tuple(
        _service_scenario_config(
            _require_mapping(raw, "service scenario")
        )
        for raw in _require_list(
            section.get("scenarios"),
            "service scenarios",
        )
    )
    scenario_ids = tuple(scenario.id for scenario in scenarios)
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("service scenario ids must be unique")
    query = _service_query_config(
        _require_mapping(section.get("query"), "service query")
    )
    if tuple(query.resource_order) != resource_ids:
        raise ValueError(
            "service resource_order must map one-to-one to resource ids"
        )
    known_scenario_kinds = {"global_limit", "partial_failure"}
    for scenario in scenarios:
        if scenario.kind not in known_scenario_kinds:
            raise ValueError("unsupported service scenario kind")
        if scenario.failed_resource_id is not None:
            if scenario.failed_resource_id not in resource_ids:
                raise ValueError("service failed resource id is unknown")
            if scenario.failure is None:
                raise ValueError("service failure scenario needs failure facts")
            if scenario.kind != "partial_failure":
                raise ValueError(
                    "only partial_failure scenarios may fail a resource"
                )
        else:
            if scenario.failure is not None:
                raise ValueError(
                    "healthy scenarios must not carry failure facts"
                )
    return {
        "query": query,
        "resources": resources,
        "scenarios": scenarios,
    }


def _service_query_config(
    raw: Mapping[str, ValidationJsonValue],
) -> _ServiceQueryConfig:
    if set(raw) != {
        "query_source",
        "speaker_raw",
        "context_prev_raw",
        "context_next_raw",
        "minimum_similarity",
        "limit",
        "resource_order",
    }:
        raise ValueError("service query fields are not closed")
    query_source = _require_string(
        raw.get("query_source"),
        "service query_source",
    )
    if not query_source.strip():
        raise ValueError("service query_source must not be empty")
    minimum_similarity = _require_ratio_number(
        raw.get("minimum_similarity"),
        "service minimum_similarity",
    )
    limit = _require_integer(
        raw.get("limit"),
        "service query limit",
    )
    if limit < 1:
        raise ValueError("service query limit must be positive")
    resource_order = _require_string_list(
        raw.get("resource_order"),
        "service resource_order",
    )
    if not resource_order:
        raise ValueError("service resource_order must not be empty")
    if len(resource_order) != len(set(resource_order)):
        raise ValueError("service resource_order ids must be unique")
    return _ServiceQueryConfig(
        query_source=query_source,
        speaker_raw=_require_optional_string(
            raw.get("speaker_raw"),
            "service query speaker_raw",
        ),
        context_prev_raw=_require_optional_string(
            raw.get("context_prev_raw"),
            "service query context_prev_raw",
        ),
        context_next_raw=_require_optional_string(
            raw.get("context_next_raw"),
            "service query context_next_raw",
        ),
        minimum_similarity=minimum_similarity,
        limit=limit,
        resource_order=resource_order,
    )


def _service_resource_config(
    raw: Mapping[str, ValidationJsonValue],
) -> _ServiceResourceConfig:
    if set(raw) != {
        "batch_id",
        "batch_kind",
        "canonical_store_id",
        "drafts",
        "expected",
        "id",
        "resource_id",
        "source_digest",
        "source_name",
        "stage_id",
    }:
        raise ValueError("service resource fields are not closed")
    section_id = _require_string(
        raw.get("id"),
        "service resource id",
    )
    if not section_id.strip():
        raise ValueError("service resource id must not be empty")
    resource_id = _require_string(
        raw.get("resource_id"),
        "service resource resource_id",
    )
    if not resource_id.strip():
        raise ValueError("service resource_id must not be empty")
    canonical_store_id = _require_string(
        raw.get("canonical_store_id"),
        "service canonical_store_id",
    )
    if not canonical_store_id.strip():
        raise ValueError("service canonical_store_id must not be empty")
    stage_id = _require_string(
        raw.get("stage_id"),
        "service stage_id",
    )
    if not stage_id.strip():
        raise ValueError("service stage_id must not be empty")
    batch_id = _require_string(
        raw.get("batch_id"),
        "service batch_id",
    )
    if not batch_id.strip():
        raise ValueError("service batch_id must not be empty")
    batch_kind = _require_string(
        raw.get("batch_kind"),
        "service batch_kind",
    )
    if batch_kind != "import":
        raise ValueError("service batch_kind is unsupported")
    source_digest = require_digest(
        raw.get("source_digest"),
        "service source_digest",
    )
    source_name = _require_string(
        raw.get("source_name"),
        "service source_name",
    )
    if not source_name.strip():
        raise ValueError("service source_name must not be empty")
    return _ServiceResourceConfig(
        id=section_id,
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        stage_id=stage_id,
        batch_id=batch_id,
        batch_kind=batch_kind,
        source_digest=source_digest,
        source_name=source_name,
        drafts=_service_drafts(
            _require_list(raw.get("drafts"), "service drafts")
        ),
        expected=_service_resource_expected(
            _require_mapping(
                raw.get("expected"),
                "service resource expected",
            )
        ),
    )


def _service_drafts(
    raw: list[ValidationJsonValue],
) -> tuple[_ServiceDraftConfig, ...]:
    if not raw:
        raise ValueError("service drafts must not be empty")
    drafts: list[_ServiceDraftConfig] = []
    seen_ordinals: set[int] = set()
    for index, entry_raw in enumerate(raw):
        entry = _service_draft_config(
            _require_mapping(entry_raw, "service draft")
        )
        if entry.ordinal in seen_ordinals:
            raise ValueError("service draft ordinals must be unique")
        seen_ordinals.add(entry.ordinal)
        if entry.ordinal != index:
            raise ValueError("service draft ordinals must be contiguous")
        drafts.append(entry)
    return tuple(drafts)


def _service_draft_config(
    raw: Mapping[str, ValidationJsonValue],
) -> _ServiceDraftConfig:
    if set(raw) != {
        "ordinal",
        "source_raw",
        "target_raw",
        "speaker_raw",
        "context_prev_raw",
        "context_next_raw",
    }:
        raise ValueError("service draft fields are not closed")
    ordinal = _require_integer(raw.get("ordinal"), "service draft ordinal")
    if ordinal < 0:
        raise ValueError("service draft ordinal must be non-negative")
    source_raw = _require_string(
        raw.get("source_raw"),
        "service draft source_raw",
    )
    if not source_raw:
        raise ValueError("service draft source_raw must not be empty")
    target_raw = _require_string(
        raw.get("target_raw"),
        "service draft target_raw",
    )
    if not target_raw:
        raise ValueError("service draft target_raw must not be empty")
    return _ServiceDraftConfig(
        ordinal=ordinal,
        source_raw=source_raw,
        target_raw=target_raw,
        speaker_raw=_require_optional_string(
            raw.get("speaker_raw"),
            "service draft speaker_raw",
        ),
        context_prev_raw=_require_optional_string(
            raw.get("context_prev_raw"),
            "service draft context_prev_raw",
        ),
        context_next_raw=_require_optional_string(
            raw.get("context_next_raw"),
            "service draft context_next_raw",
        ),
    )


def _service_resource_expected(
    raw: Mapping[str, ValidationJsonValue],
) -> dict[str, object]:
    if set(raw) != {
        "context_variant",
        "exported_record_ids",
        "generation",
        "head_revision",
        "record_count",
        "winner_record_id",
    }:
        raise ValueError("service resource expected fields are not closed")
    generation = _require_integer(
        raw.get("generation"),
        "service expected generation",
    )
    if generation < 0:
        raise ValueError("service expected generation must be non-negative")
    head_revision = _require_integer(
        raw.get("head_revision"),
        "service expected head_revision",
    )
    if head_revision < 0:
        raise ValueError(
            "service expected head_revision must be non-negative"
        )
    record_count = _require_integer(
        raw.get("record_count"),
        "service expected record_count",
    )
    if record_count < 0:
        raise ValueError("service expected record_count must be non-negative")
    exported_record_ids = _require_int_list(
        raw.get("exported_record_ids"),
        "service expected exported_record_ids",
    )
    if any(record_id < 1 for record_id in exported_record_ids):
        raise ValueError(
            "service expected exported_record_ids must be positive"
        )
    if tuple(exported_record_ids) != tuple(sorted(exported_record_ids)):
        raise ValueError(
            "service expected exported_record_ids must be sorted"
        )
    if len(exported_record_ids) != len(set(exported_record_ids)):
        raise ValueError(
            "service expected exported_record_ids must be unique"
        )
    winner_record_id = _require_integer(
        raw.get("winner_record_id"),
        "service expected winner_record_id",
    )
    if winner_record_id < 1:
        raise ValueError(
            "service expected winner_record_id must be positive"
        )
    variant = _service_context_variant(
        _require_mapping(
            raw.get("context_variant"),
            "service expected context_variant",
        )
    )
    return {
        "generation": generation,
        "head_revision": head_revision,
        "record_count": record_count,
        "exported_record_ids": exported_record_ids,
        "winner_record_id": winner_record_id,
        "context_variant": variant,
    }


def _service_context_variant(
    raw: Mapping[str, ValidationJsonValue],
) -> dict[str, object]:
    if set(raw) != {
        "record_id",
        "comparable_fields",
        "matched_fields",
        "mismatched_fields",
        "strength_v1",
    }:
        raise ValueError("service context variant fields are not closed")
    record_id = _require_integer(
        raw.get("record_id"),
        "service context variant record_id",
    )
    if record_id < 1:
        raise ValueError(
            "service context variant record_id must be positive"
        )
    comparable = _require_string_list(
        raw.get("comparable_fields"),
        "service context comparable_fields",
    )
    matched = _require_string_list(
        raw.get("matched_fields"),
        "service context matched_fields",
    )
    mismatched = _require_string_list(
        raw.get("mismatched_fields"),
        "service context mismatched_fields",
    )
    strength = _require_int_list(
        raw.get("strength_v1"),
        "service context strength_v1",
    )
    if len(strength) != 5:
        raise ValueError(
            "service context strength_v1 must have five entries"
        )
    evidence = ContextEvidence(
        comparable_fields=comparable,
        matched_fields=matched,
        mismatched_fields=mismatched,
        strength_v1=cast("tuple[int, int, int, int, int]", strength),
    )
    return {
        "record_id": record_id,
        "evidence": evidence,
    }


def _service_scenario_config(
    raw: Mapping[str, ValidationJsonValue],
) -> _ServiceScenarioConfig:
    if set(raw) != {
        "expected",
        "failure",
        "failed_resource_id",
        "id",
        "kind",
    }:
        raise ValueError("service scenario fields are not closed")
    scenario_id = _require_string(
        raw.get("id"),
        "service scenario id",
    )
    if not scenario_id.strip():
        raise ValueError("service scenario id must not be empty")
    kind = _require_string(
        raw.get("kind"),
        "service scenario kind",
    )
    failed_resource_id = raw.get("failed_resource_id")
    if failed_resource_id is not None:
        failed_resource_id = _require_string(
            failed_resource_id,
            "service scenario failed_resource_id",
        )
    failure = raw.get("failure")
    if failure is not None:
        failure = _service_scenario_failure(
            _require_mapping(failure, "service scenario failure")
        )
    return _ServiceScenarioConfig(
        id=scenario_id,
        kind=kind,
        failed_resource_id=failed_resource_id,
        failure=failure,
        expected=_service_scenario_expected(
            _require_mapping(
                raw.get("expected"),
                "service scenario expected",
            )
        ),
    )


def _service_scenario_failure(
    raw: Mapping[str, ValidationJsonValue],
) -> dict[str, object]:
    if set(raw) != {"code", "retryable", "stage"}:
        raise ValueError("service scenario failure fields are not closed")
    stage = _require_string(
        raw.get("stage"),
        "service scenario failure stage",
    )
    code = _require_string(
        raw.get("code"),
        "service scenario failure code",
    )
    retryable = _require_bool(
        raw.get("retryable"),
        "service scenario failure retryable",
    )
    if not stage or not code:
        raise ValueError("service scenario failure facts must not be empty")
    return {
        "stage": stage,
        "code": code,
        "retryable": retryable,
    }


def _service_scenario_expected(
    raw: Mapping[str, ValidationJsonValue],
) -> dict[str, object]:
    if set(raw) != {
        "context_observed_count",
        "context_returned_count",
        "failure_count",
        "result_count",
        "result_record_ids",
        "result_resource_ids",
        "returned_count_by_resource",
        "scored_count_total",
    }:
        raise ValueError("service scenario expected fields are not closed")
    result_count = _require_integer(
        raw.get("result_count"),
        "service expected result_count",
    )
    if result_count < 0:
        raise ValueError("service expected result_count must be non-negative")
    result_record_ids = list(
        _require_int_list(
            raw.get("result_record_ids"),
            "service expected result_record_ids",
        )
    )
    if any(record_id < 1 for record_id in result_record_ids):
        raise ValueError(
            "service expected result_record_ids must be positive"
        )
    if len(result_record_ids) != result_count:
        raise ValueError("service expected result count diverged")
    result_resource_ids = list(
        _require_string_list(
            raw.get("result_resource_ids"),
            "service expected result_resource_ids",
        )
    )
    if len(result_resource_ids) != result_count:
        raise ValueError("service expected result resource count diverged")
    returned_by_resource_raw = _require_mapping(
        raw.get("returned_count_by_resource"),
        "service expected returned_count_by_resource",
    )
    returned_by_resource = {
        resource_id: _require_integer(
            count,
            "service expected returned count",
        )
        for resource_id, count in returned_by_resource_raw.items()
    }
    if any(count < 0 for count in returned_by_resource.values()):
        raise ValueError(
            "service expected returned counts must be non-negative"
        )
    for field_name in (
        "context_observed_count",
        "context_returned_count",
        "scored_count_total",
        "failure_count",
    ):
        count = _require_integer(
            raw.get(field_name),
            f"service expected {field_name}",
        )
        if count < 0:
            raise ValueError(
                f"service expected {field_name} must be non-negative"
            )
    return {
        "result_count": result_count,
        "result_record_ids": list(result_record_ids),
        "result_resource_ids": list(result_resource_ids),
        "returned_count_by_resource": returned_by_resource,
        "context_observed_count": _require_integer(
            raw.get("context_observed_count"),
            "service expected context_observed_count",
        ),
        "context_returned_count": _require_integer(
            raw.get("context_returned_count"),
            "service expected context_returned_count",
        ),
        "scored_count_total": _require_integer(
            raw.get("scored_count_total"),
            "service expected scored_count_total",
        ),
        "failure_count": _require_integer(
            raw.get("failure_count"),
            "service expected failure_count",
        ),
    }


def _service_query(config: _ServiceQueryConfig) -> TMQuery:
    return TMQuery(
        query_source=config.query_source,
        speaker_raw=config.speaker_raw,
        context_prev_raw=config.context_prev_raw,
        context_next_raw=config.context_next_raw,
        minimum_similarity=config.minimum_similarity,
        limit=config.limit,
        resource_order=config.resource_order,
    )


def _service_draft(config: _ServiceDraftConfig) -> TMRecordDraft:
    return TMRecordDraft(
        source_raw=config.source_raw,
        target_raw=config.target_raw,
        speaker_raw=config.speaker_raw,
        context_prev_raw=config.context_prev_raw,
        context_next_raw=config.context_next_raw,
        file_source=None,
        provenance=(_SERVICE_ORIGIN_PROVENANCE,),
    )


def _service_resource_store(
    config: _ServiceResourceConfig,
    root: Path,
) -> SQLiteTMStore:
    configured = (root / f"{config.resource_id}.jsonl").resolve()
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        config.resource_id,
        configured,
    )
    stage = MutableStageRef(
        stage_id=config.stage_id,
        resource_identity=identity,
        staged_db_path=(root / ".stage.sqlite3").resolve(),
        manifest_temp_path=(root / ".stage.snapshot.tmp").resolve(),
    )
    initialize_stage_schema(
        stage,
        canonical_store_id=config.canonical_store_id,
    )
    store = SQLiteTMStore(
        stage,
        canonical_store_id=config.canonical_store_id,
    )
    store.append_batch(
        batch_id=config.batch_id,
        kind=config.batch_kind,
        drafts=tuple(_service_draft(draft) for draft in config.drafts),
        source_digest=config.source_digest,
        source_path=(root / config.source_name).resolve(),
    )
    return store


def _service_resource_observed(
    config: _ServiceResourceConfig,
    store: SQLiteTMStore,
    query: TMQuery,
    *,
    resource_order: int,
) -> dict[str, object]:
    expected = config.expected
    revision = store.canonical_revision()
    if revision.generation != expected["generation"]:
        raise ValueError(f"{config.id}: service generation diverged")
    if revision.head_revision != expected["head_revision"]:
        raise ValueError(f"{config.id}: service head revision diverged")
    if revision.record_count != expected["record_count"]:
        raise ValueError(f"{config.id}: service record count diverged")
    exported = tuple(store.export_records())
    if tuple(record.record_id for record in exported) != expected[
        "exported_record_ids"
    ]:
        raise ValueError(f"{config.id}: service exported records diverged")
    with store.query_lease() as view:
        records = view.exact_records(query.query_source)
    classification = classify_exact_context(
        resource_id=config.resource_id,
        resource_order=resource_order,
        query=query,
        records=records,
    )
    winner = classification.winner
    if winner is None or winner.match_type is not TMMatchType.EXACT:
        raise ValueError(f"{config.id}: service winner diverged")
    if winner.record_id != expected["winner_record_id"]:
        raise ValueError(f"{config.id}: service winner record id diverged")
    expected_variant = cast(
        "dict[str, object]",
        expected["context_variant"],
    )
    if len(classification.context_results) != 1:
        raise ValueError(f"{config.id}: service context count diverged")
    variant = classification.context_results[0]
    if variant.record_id != expected_variant["record_id"]:
        raise ValueError(f"{config.id}: service context variant diverged")
    if variant.context_evidence != expected_variant["evidence"]:
        raise ValueError(f"{config.id}: service context evidence diverged")
    return {
        "resource_id": config.resource_id,
        "generation": revision.generation,
        "winner_record_id": winner.record_id,
        "context_count": len(classification.context_results),
        "context_variant": {
            "record_id": variant.record_id,
            "comparable_fields": list(
                variant.context_evidence.comparable_fields
            ),
            "matched_fields": list(variant.context_evidence.matched_fields),
            "mismatched_fields": list(
                variant.context_evidence.mismatched_fields
            ),
            "strength_v1": list(variant.context_evidence.strength_v1),
        },
        "exported_record_ids": [
            record.record_id for record in exported
        ],
    }


def _harness_service_health(store: SQLiteTMStore) -> StoreHealth:
    """Derive the harness-verified health snapshot for one real stage.

    The real stage stays unpublished (the offline leaf may not import the
    migration/sealing pipeline), so ``exact_available`` is granted from the
    harness's own observation of the real query view in
    ``_service_resource_observed`` rather than from the physical ACTIVE
    marker.  Physical facts and the stable sorted diagnostic codes are
    carried through unchanged except the physical not-active code, which
    the harness observation has already ruled out.
    """

    physical = store.health()
    return StoreHealth(
        healthy=physical.healthy,
        schema_version=physical.schema_version,
        generation=physical.generation,
        record_count=physical.record_count,
        index_kind=physical.index_kind,
        snapshot_binding_digest=physical.snapshot_binding_digest,
        source_binding_state=physical.source_binding_state,
        exact_available=True,
        context_available=False,
        fuzzy_available=False,
        diagnostic_codes=tuple(
            code
            for code in physical.diagnostic_codes
            if code != "STORE.CANONICAL_NOT_ACTIVE"
        ),
    )


def _harness_capability_publisher(
    expectation: RetrievalCapabilityExpectation,
    *,
    observed_context_digest: str,
    observed_fuzzy_core_digest: str,
    generated_at_utc: datetime,
    valid_until_utc: datetime,
) -> RetrievalCapabilityPublisher:
    generated = _require_utc(generated_at_utc, "generated_at_utc")
    valid_until = _require_utc(valid_until_utc, "valid_until_utc")
    generated_text = _format_utc(generated)
    valid_until_text = _format_utc(valid_until)
    context_expected = expectation.context_cohorts[0]
    fuzzy_expected = expectation.fuzzy_core_cohorts[0]
    context_passed = (
        observed_context_digest == context_expected.cohort_digest
    )
    harness_manifest = RetrievalCapabilityManifest(
        evidence_schema_version=expectation.evidence_schema_version,
        retrieval_artifact_digest=expectation.retrieval_artifact_digest,
        retrieval_build_digest=expectation.retrieval_build_digest,
        semantics_version=expectation.semantics_version,
        fixture_digest=expectation.fixture_digest,
        evaluator_digest=expectation.evaluator_digest,
        generated_at_utc=generated_text,
        valid_until_utc=valid_until_text,
        context_cohorts=(
            RetrievalCorrectnessCohortEvidence(
                cohort_id=context_expected.cohort_id,
                cohort_digest=observed_context_digest,
                passed=context_passed,
                generated_at_utc=generated_text,
                valid_until_utc=valid_until_text,
            ),
        ),
        fuzzy_core_cohorts=(
            RetrievalCorrectnessCohortEvidence(
                cohort_id=fuzzy_expected.cohort_id,
                cohort_digest=observed_fuzzy_core_digest,
                passed=False,
                generated_at_utc=generated_text,
                valid_until_utc=valid_until_text,
            ),
        ),
        fts5_trigram_benchmark=None,
        gram_fallback_benchmark=None,
    )
    return RetrievalCapabilityPublisher(
        RetrievalCapabilityEvaluator(expectation),
        initial_manifest=harness_manifest,
        evaluated_at_utc=generated,
    )


def _verify_harness_snapshot(
    snapshot: RetrievalCapabilitySnapshot,
    *,
    observed_context_digest: str,
    expectation: RetrievalCapabilityExpectation,
) -> None:
    context_passed = (
        observed_context_digest
        == expectation.context_cohorts[0].cohort_digest
    )
    if snapshot.context.available != context_passed:
        raise ValueError("harness context decision diverged")
    if context_passed:
        if snapshot.context.unavailable_code is not None:
            raise ValueError("harness context code diverged")
    elif snapshot.context.unavailable_code != (
        RETRIEVAL_CONTEXT_EVIDENCE_FAILED_CODE
    ):
        raise ValueError("harness context failure code diverged")
    if snapshot.fuzzy_core.available is not False:
        raise ValueError("harness fuzzy core must stay closed")
    if snapshot.fuzzy_core.unavailable_code != (
        RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE
    ):
        raise ValueError("harness fuzzy core code diverged")
    if (
        snapshot.fts5_trigram.available is not False
        or snapshot.gram_fallback.available is not False
    ):
        raise ValueError("harness Gate D paths must stay closed")
    if snapshot.fts5_trigram.unavailable_code != (
        RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE
    ):
        raise ValueError("harness FTS5 trigram code diverged")
    if snapshot.gram_fallback.unavailable_code != (
        RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE
    ):
        raise ValueError("harness gram fallback code diverged")


def _observe_service_transcript(
    fixture_path: Path,
    *,
    expectation: RetrievalCapabilityExpectation,
    observed_context_digest: str,
    observed_fuzzy_core_digest: str,
    generated_at_utc: datetime,
    valid_until_utc: datetime,
) -> list[dict[str, object]]:
    """Run both fixed multi-resource service scenarios and emit entries.

    Each scenario builds two real temporary ``SQLiteTMStore`` stages from
    the fixed service section, observes each real stage through its public
    query view (revision, exported records, exact winner and context
    variant), and hands the service a harness adapter whose read lease is
    the real ``SQLiteTMQueryView`` and whose health snapshot carries the
    harness-verified exact availability; the configured failing resource's
    adapter raises one stable ``SQLiteStoreLifecycleError`` from the public
    ``query_lease`` port.  The harness ``RetrievalCapabilityPublisher`` is
    constructed from the approved expectation and the observed CONTEXT
    digest with the fuzzy-core row pending, and ``TMRetrievalService`` runs
    with sentinel retriever/scorer ports so any fuzzy access fails closed.
    Entries carry only scenario/vector/resource ids, generation, result ids
    and match types, context field names and strength tuples,
    returned/scored counts, availability/unavailable codes and stable
    failure facts - never bodies, provenance, paths or exception text.
    """

    payload = _load_fixture_payload(fixture_path)
    section = _require_mapping(payload.get("service"), "service section")
    config = _service_config(section)
    query = _service_query(cast("_ServiceQueryConfig", config["query"]))
    resources = cast(
        "tuple[_ServiceResourceConfig, ...]",
        config["resources"],
    )
    scenarios = cast(
        "tuple[_ServiceScenarioConfig, ...]",
        config["scenarios"],
    )
    resource_by_id = {
        resource.resource_id: resource for resource in resources
    }
    transcript: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for scenario in scenarios:
            scenario_root = root / scenario.id
            scenario_root.mkdir()
            stores: dict[str, SQLiteTMStore] = {}
            observed: dict[str, dict[str, object]] = {}
            for resource in resources:
                resource_root = scenario_root / resource.id
                resource_root.mkdir()
                store = _service_resource_store(resource, resource_root)
                stores[resource.resource_id] = store
                observed[resource.resource_id] = _service_resource_observed(
                    resource,
                    store,
                    query,
                    resource_order=query.resource_order.index(
                        resource.resource_id
                    ),
                )
            harness = _harness_capability_publisher(
                expectation,
                observed_context_digest=observed_context_digest,
                observed_fuzzy_core_digest=observed_fuzzy_core_digest,
                generated_at_utc=generated_at_utc,
                valid_until_utc=valid_until_utc,
            )
            snapshot = harness.snapshot()
            _verify_harness_snapshot(
                snapshot,
                observed_context_digest=observed_context_digest,
                expectation=expectation,
            )
            handles: list[TMResourceHandle] = []
            for resource_id in query.resource_order:
                store = stores[resource_id]
                if scenario.failed_resource_id == resource_id:
                    binding: TMStore = _GenerationChangedQueryLeaseStore(
                        store,
                        resource_id=resource_id,
                        generation=cast(
                            int,
                            observed[resource_id]["generation"],
                        ),
                    )
                else:
                    binding = _HarnessServiceStore(
                        store,
                        health=_harness_service_health(store),
                    )
                handles.append(
                    TMResourceHandle(
                        resource_id=resource_id,
                        store=binding,
                        active=True,
                        lookup=True,
                        update=False,
                        order=query.resource_order.index(resource_id),
                    )
                )
            service = TMRetrievalService(
                retriever=cast(
                    CandidateRetriever,
                    cast(Any, _NoFuzzyAccessPort()),
                ),
                scorer=cast(
                    SimilarityScorer,
                    cast(Any, _NoFuzzyAccessPort()),
                ),
                capability_publisher=harness,
            )
            report = service.query(tuple(handles), query)
            entry = _service_transcript_entry(
                scenario=scenario,
                query=query,
                report=report,
                observed=observed,
                snapshot=snapshot,
            )
            _verify_service_entry(entry, scenario, snapshot)
            transcript.append(entry)
    return transcript


def _service_transcript_entry(
    *,
    scenario: _ServiceScenarioConfig,
    query: TMQuery,
    report: QueryReport,
    observed: Mapping[str, Mapping[str, object]],
    snapshot: RetrievalCapabilitySnapshot,
) -> dict[str, object]:
    resource_entries: list[dict[str, object]] = []
    for metadata in report.resource_metadata:
        resource_observed = observed[metadata.resource_id]
        recall = metadata.recall
        resource_results = [
            result
            for result in report.results
            if result.resource_id == metadata.resource_id
        ]
        context_returned = sum(
            1
            for result in resource_results
            if result.match_type is TMMatchType.CONTEXT
        )
        resource_entries.append(
            {
                "resource_id": metadata.resource_id,
                "generation": resource_observed["generation"],
                "context": {
                    "available": metadata.context_available,
                    "unavailable_code": metadata.context_unavailable_code,
                    "observed_count": resource_observed["context_count"],
                    "returned_count": context_returned,
                },
                "context_variant": resource_observed["context_variant"],
                "recall": {
                    "index_kind": recall.index_kind,
                    "fuzzy_available": recall.fuzzy_available,
                    "fuzzy_unavailable_code": (
                        recall.fuzzy_unavailable_code
                    ),
                    "stages": [],
                    "union_unique_count": recall.union_unique_count,
                    "deduplicated_count": recall.deduplicated_count,
                    "result_limit": recall.result_limit,
                    "candidate_budget": recall.candidate_budget,
                    "candidate_budget_version": (
                        recall.candidate_budget_version
                    ),
                    "truncated": recall.truncated,
                },
                "scored_count": metadata.scored_count,
                "returned_count": metadata.returned_count,
                "results": [
                    {
                        "record_id": result.record_id,
                        "match_type": result.match_type.value,
                    }
                    for result in resource_results
                ],
            }
        )
    failures = [
        {
            "resource_id": failure.resource_id,
            "stage": failure.stage,
            "error_code": failure.error_code,
            "retryable": failure.retryable,
        }
        for failure in report.resource_failures
    ]
    context_observed_total = sum(
        cast(int, observed[metadata.resource_id]["context_count"])
        for metadata in report.resource_metadata
    )
    context_returned_total = sum(
        1
        for result in report.results
        if result.match_type is TMMatchType.CONTEXT
    )
    scored_total = sum(
        metadata.scored_count for metadata in report.resource_metadata
    )
    return {
        "id": scenario.id,
        "kind": scenario.kind,
        "version": _SERVICE_SECTION_VERSION,
        "query": {
            "limit": query.limit,
            "minimum_similarity": query.minimum_similarity,
            "resource_order": list(query.resource_order),
        },
        "capability": {
            "context": {
                "available": snapshot.context.available,
                "unavailable_code": snapshot.context.unavailable_code,
            },
            "fuzzy_core": {
                "available": snapshot.fuzzy_core.available,
                "unavailable_code": snapshot.fuzzy_core.unavailable_code,
            },
            "fts5_trigram": {
                "available": snapshot.fts5_trigram.available,
                "unavailable_code": snapshot.fts5_trigram.unavailable_code,
            },
            "gram_fallback": {
                "available": snapshot.gram_fallback.available,
                "unavailable_code": snapshot.gram_fallback.unavailable_code,
            },
            "summary_unavailable_codes": list(
                snapshot.summary.unavailable_codes
            ),
        },
        "resources": resource_entries,
        "failures": failures,
        "aggregation": {
            "result_count": len(report.results),
            "result_record_ids": [
                result.record_id for result in report.results
            ],
            "result_resource_ids": [
                result.resource_id for result in report.results
            ],
            "returned_count_by_resource": {
                metadata.resource_id: metadata.returned_count
                for metadata in report.resource_metadata
            },
            "context_observed_count": context_observed_total,
            "context_returned_count": context_returned_total,
            "scored_count_total": scored_total,
        },
    }


def _verify_service_entry(
    entry: Mapping[str, object],
    scenario: _ServiceScenarioConfig,
    snapshot: RetrievalCapabilitySnapshot,
) -> None:
    expected = scenario.expected
    aggregation = cast("dict[str, object]", entry["aggregation"])
    failures = cast("list[dict[str, object]]", entry["failures"])
    resources = cast("list[dict[str, object]]", entry["resources"])
    for field_name in (
        "result_count",
        "result_record_ids",
        "result_resource_ids",
        "returned_count_by_resource",
        "context_observed_count",
        "context_returned_count",
        "scored_count_total",
    ):
        if aggregation[field_name] != expected[field_name]:
            raise ValueError(f"service {field_name} diverged")
    if len(failures) != expected["failure_count"]:
        raise ValueError("service failure count diverged")
    if expected["failure_count"]:
        failure = failures[0]
        scenario_failure = scenario.failure
        if scenario_failure is None:
            raise ValueError("service failure facts are missing")
        if failure["stage"] != scenario_failure["stage"]:
            raise ValueError("service failure stage diverged")
        if failure["error_code"] != scenario_failure["code"]:
            raise ValueError("service failure code diverged")
        if failure["retryable"] != scenario_failure["retryable"]:
            raise ValueError("service failure retryable diverged")
        if failure["resource_id"] != scenario.failed_resource_id:
            raise ValueError("service failure resource diverged")
    for resource in resources:
        context = cast("dict[str, object]", resource["context"])
        if context["available"] != snapshot.context.available:
            raise ValueError("service context availability diverged")
        if (
            context["unavailable_code"]
            != snapshot.context.unavailable_code
        ):
            raise ValueError("service context code diverged")
        recall = cast("dict[str, object]", resource["recall"])
        if recall["fuzzy_available"] is not False:
            raise ValueError("service fuzzy recall must stay closed")
        if recall["fuzzy_unavailable_code"] != (
            RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE
        ):
            raise ValueError("service fuzzy recall code diverged")
        if recall["stages"] != []:
            raise ValueError("service fuzzy recall stages must stay empty")
        if (
            recall["union_unique_count"] != 0
            or recall["deduplicated_count"] != 0
        ):
            raise ValueError("service fuzzy recall counts must stay zero")
        if recall["truncated"] is not False:
            raise ValueError("service fuzzy recall must not truncate")
        if resource["scored_count"] != 0:
            raise ValueError("service scored count must stay zero")


def _require_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a built-in boolean")
    return value


def _require_ratio_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return number


def _require_utc(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(
            f"{field_name} must be a timezone-aware UTC datetime"
        )
    if value.microsecond != 0:
        raise ValueError(f"{field_name} must have whole-second precision")
    return value


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_mapping(
    value: object,
    field_name: str,
) -> Mapping[str, ValidationJsonValue]:
    if type(value) is not dict:
        raise TypeError(f"{field_name} must be a built-in object")
    return cast(Mapping[str, ValidationJsonValue], value)


def _require_list(
    value: object,
    field_name: str,
) -> list[ValidationJsonValue]:
    if type(value) is not list:
        raise TypeError(f"{field_name} must be a built-in list")
    return cast(list[ValidationJsonValue], value)


def _require_string(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a built-in string")
    return value


def _require_optional_string(
    value: object,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    return _require_string(value, field_name)


def _require_integer(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be a built-in integer")
    return value


def _require_string_list(
    value: object,
    field_name: str,
) -> tuple[str, ...]:
    return tuple(
        _require_string(item, field_name)
        for item in _require_list(value, field_name)
    )


def _require_int_list(
    value: object,
    field_name: str,
) -> tuple[int, ...]:
    return tuple(
        _require_integer(item, field_name)
        for item in _require_list(value, field_name)
    )


__all__ = [
    "RETRIEVAL_GATE_C_ROOTS_SCHEMA_VERSION",
    "RETRIEVAL_CONTEXT_COHORT_ID",
    "RETRIEVAL_FUZZY_CORE_COHORT_ID",
    "RetrievalValidationRelease",
    "recompute_retrieval_validation",
]
