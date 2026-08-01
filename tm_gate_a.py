"""Deterministic, storage-independent evidence for Feature 5 Gate A.

The checked-in JSON contains approved digest roots, never a live readiness
claim.  This module recomputes observations from version-controlled source and
fixture bytes, executes the frozen codecs/algorithms, and grants each component
independently.  SQLite, FTS5, and benchmark state are intentionally absent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from enum import Enum
import hashlib
import inspect
import json
from pathlib import Path
from typing import cast, get_args

import tm_contracts as tm_contracts_module

from text_matcher import (
    TextMatcherV1,
    fold_text_v1,
    is_pure_cjk_v1,
    project_folded_span_v1,
    word_boundaries_v1,
)
from tm_contracts import (
    ContextEvidence,
    ResourceQueryFailure,
    SearchHit,
    SearchOptions,
    SimilarityEvidence,
    SourceBindingState,
    StoreHealth,
    TMMatchType,
    TMQuery,
    TMRecord,
    TMRecordDraft,
    TMResult,
    TextMatchProfile,
    contract_from_json,
    contract_to_json,
)
from tm_similarity import SimilarityScorerV1


GATE_A_SCHEMA_VERSION = "feature5-gate-a-v1"
_DIGEST_LENGTH = 64
_DEFAULT_APPROVED_ROOTS = (
    Path(__file__).parent
    / "tests"
    / "fixtures"
    / "feature5_gate_a_v1.json"
)
_GATE_A_FACTORY_KEY = object()

type ValidationJsonValue = (
    None
    | bool
    | int
    | float
    | str
    | list["ValidationJsonValue"]
    | dict[str, "ValidationJsonValue"]
)


class GateAComponent(str, Enum):
    CONTRACTS = "CONTRACTS"
    SIMILARITY = "SIMILARITY"
    TEXT = "TEXT"


@dataclass(frozen=True)
class GateAComponentEvidence:
    component: GateAComponent
    granted: bool
    approved_artifact_digest: str
    observed_artifact_digest: str
    approved_fixture_digest: str
    observed_fixture_digest: str
    approved_transcript_digest: str
    observed_transcript_digest: str
    safe_failure_code: str | None
    _factory_key: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_key is not _GATE_A_FACTORY_KEY:
            raise TypeError(
                "Gate A component evidence requires the Core factory"
            )


@dataclass(frozen=True)
class GateAEvidenceReport:
    schema_version: str
    components: tuple[GateAComponentEvidence, ...]

    @property
    def granted_components(self) -> tuple[GateAComponent, ...]:
        return tuple(
            item.component for item in self.components if item.granted
        )

    def evidence_for(
        self,
        component: GateAComponent,
    ) -> GateAComponentEvidence:
        return next(
            item for item in self.components if item.component is component
        )


def recompute_gate_a(
    *,
    repository_root: Path,
    approved_roots_path: Path = _DEFAULT_APPROVED_ROOTS,
) -> GateAEvidenceReport:
    """Recompute independent CONTRACTS/SIMILARITY/TEXT evidence grants."""

    root = _require_directory(repository_root, "repository_root")
    approved = load_approved_roots(approved_roots_path)
    raw_components = require_mapping(
        approved["components"],
        "approved components",
    )
    evidence: list[GateAComponentEvidence] = []
    for component in GateAComponent:
        raw = require_mapping(
            raw_components.get(component.value),
            f"approved {component.value} component",
        )
        if set(raw) != {
            "artifact_digest",
            "artifact_paths",
            "fixture_digest",
            "fixture_paths",
            "transcript_digest",
        }:
            raise ValueError(
                f"approved {component.value} component fields are not closed"
            )
        artifact_paths = require_paths(
            raw.get("artifact_paths"),
            f"{component.value} artifact_paths",
        )
        fixture_paths = require_paths(
            raw.get("fixture_paths"),
            f"{component.value} fixture_paths",
            allow_empty=True,
        )
        approved_artifact = require_digest(
            raw.get("artifact_digest"),
            f"{component.value} artifact_digest",
        )
        approved_fixture = require_digest(
            raw.get("fixture_digest"),
            f"{component.value} fixture_digest",
        )
        approved_transcript = require_digest(
            raw.get("transcript_digest"),
            f"{component.value} transcript_digest",
        )
        execution_failed = False
        try:
            observed_artifact = aggregate_paths_digest(root, artifact_paths)
        except Exception:
            observed_artifact = _failed_observation_digest(
                component,
                "artifact",
            )
            execution_failed = True
        try:
            observed_fixture = aggregate_paths_digest(root, fixture_paths)
        except Exception:
            observed_fixture = _failed_observation_digest(
                component,
                "fixture",
            )
            execution_failed = True
        try:
            observed_transcript = _component_transcript_digest(
                component,
                root,
            )
        except Exception:
            observed_transcript = _failed_observation_digest(
                component,
                "transcript",
            )
            execution_failed = True
        granted = (
            not execution_failed
            and observed_artifact == approved_artifact
            and observed_fixture == approved_fixture
            and observed_transcript == approved_transcript
        )
        evidence.append(
            _make_component_evidence(
                component=component,
                granted=granted,
                approved_artifact_digest=approved_artifact,
                observed_artifact_digest=observed_artifact,
                approved_fixture_digest=approved_fixture,
                observed_fixture_digest=observed_fixture,
                approved_transcript_digest=approved_transcript,
                observed_transcript_digest=observed_transcript,
                execution_failed=execution_failed,
            )
        )
    return GateAEvidenceReport(
        schema_version=GATE_A_SCHEMA_VERSION,
        components=tuple(evidence),
    )


def _failed_observation_digest(
    component: GateAComponent,
    evidence_kind: str,
) -> str:
    return canonical_digest(
        {
            "component": component.value,
            "evidence_kind": evidence_kind,
            "observation": "EXECUTION_FAILED",
        }
    )


def _make_component_evidence(
    *,
    component: GateAComponent,
    granted: bool,
    approved_artifact_digest: str,
    observed_artifact_digest: str,
    approved_fixture_digest: str,
    observed_fixture_digest: str,
    approved_transcript_digest: str,
    observed_transcript_digest: str,
    execution_failed: bool,
) -> GateAComponentEvidence:
    """Private grant factory: callers cannot promote observed evidence."""

    failure_code: str | None = None
    if not granted:
        if execution_failed:
            failure_code = f"GATE_A.{component.value}.EXECUTION_FAILED"
        elif observed_artifact_digest != approved_artifact_digest:
            failure_code = f"GATE_A.{component.value}.ARTIFACT_MISMATCH"
        elif observed_fixture_digest != approved_fixture_digest:
            failure_code = f"GATE_A.{component.value}.FIXTURE_MISMATCH"
        else:
            failure_code = f"GATE_A.{component.value}.GOLDEN_MISMATCH"
    return GateAComponentEvidence(
        component=component,
        granted=granted,
        approved_artifact_digest=approved_artifact_digest,
        observed_artifact_digest=observed_artifact_digest,
        approved_fixture_digest=approved_fixture_digest,
        observed_fixture_digest=observed_fixture_digest,
        approved_transcript_digest=approved_transcript_digest,
        observed_transcript_digest=observed_transcript_digest,
        safe_failure_code=failure_code,
        _factory_key=_GATE_A_FACTORY_KEY,
    )


def _component_transcript_digest(
    component: GateAComponent,
    repository_root: Path,
) -> str:
    if component is GateAComponent.CONTRACTS:
        transcript = _contracts_transcript()
    elif component is GateAComponent.SIMILARITY:
        transcript = _similarity_transcript(repository_root)
    else:
        transcript = {
            "matcher": _all_matcher_transcript(repository_root),
            "unicode": unicode_transcript(repository_root),
        }
    return canonical_digest(transcript)


def _contracts_transcript() -> dict[str, object]:
    context = ContextEvidence(
        comparable_fields=(),
        matched_fields=(),
        mismatched_fields=(),
        strength_v1=(0, 0, 0, 0, 0),
    )
    similarity = SimilarityEvidence(
        levenshtein_ratio=0.75,
        dice_bigram=0.85,
        final_similarity=0.8,
    )
    contracts = (
        TMRecord(
            record_id=7,
            source_raw="Open the door.",
            target_raw="开门。",
            speaker_raw="narrator",
            context_prev_raw=None,
            context_next_raw="Now.",
            file_source="chapter-01.json",
            provenance=(("importer", "gate-a"),),
            legacy_line_no=11,
            origin_batch_id="batch.gate-a",
            origin_ordinal=3,
        ),
        TMRecordDraft(
            source_raw="Open the door.",
            target_raw="开门。",
            speaker_raw=None,
            context_prev_raw=None,
            context_next_raw=None,
            file_source="chapter-01.json",
            provenance=(("writer", "gate-a"),),
        ),
        TMQuery(
            query_source="Open the door.",
            speaker_raw=None,
            context_prev_raw=None,
            context_next_raw=None,
            minimum_similarity=0.6,
            limit=10,
            resource_order=("tm.primary",),
        ),
        similarity,
        context,
        TMResult(
            resource_id="tm.primary",
            record_id=7,
            query_source="Open the door.",
            matched_source="Open that door.",
            target="开门。",
            match_type=TMMatchType.FUZZY,
            similarity=0.8,
            similarity_evidence=similarity,
            context_evidence=context,
            provenance=(("importer", "gate-a"),),
            stable_tie_key=(0, 7),
        ),
        ResourceQueryFailure(
            resource_id="tm.secondary",
            stage="QUERY",
            error_code="RESOURCE_UNREADABLE",
            retryable=False,
        ),
        SearchOptions(match_case=False, whole_word=True),
        SearchHit(start_index=1, end_index=4),
    )
    round_trips: list[dict[str, object]] = []
    for contract in contracts:
        encoded = contract_to_json(contract)
        decoded = contract_from_json(encoded)
        round_trips.append(
            {
                "contract_type": type(contract).__name__,
                "encoded_digest": _sha256(encoded.encode("utf-8")),
                "round_trip_equal": decoded == contract,
                "stable_reencode": contract_to_json(decoded) == encoded,
            }
        )

    valid = cast(
        dict[str, object],
        json.loads(contract_to_json(contracts[0])),
    )
    strict_results: list[dict[str, str]] = []
    mutations = (
        ("unsupported-version", {"contract_version": 999}),
        ("unexpected-envelope", {"unexpected": "field"}),
    )
    for case_id, mutation in mutations:
        altered: dict[str, object] = dict(valid)
        altered.update(mutation)
        strict_results.append(
            _capture_rejection(case_id, _canonical_json(altered))
        )
    strict_results.append(
        _capture_rejection(
            "non-finite-number",
            (
                '{"contract_type":"SimilarityEvidence",'
                '"contract_version":1,"payload":{"dice_bigram":0.5,'
                '"final_similarity":NaN,"levenshtein_ratio":0.5,'
                '"scorer_version":"scorer-v1"}}'
            ),
        )
    )
    return {
        "codec_smoke_version": "tm-contract-codec-smoke-v1",
        "public_surface": contracts_public_surface_transcript(),
        "round_trips": round_trips,
        "strict_rejections": strict_results,
    }


def contracts_public_surface_transcript() -> dict[str, object]:
    """Mechanically describe every frozen public contract entry."""

    public_items = [
        {
            "name": name,
            "surface": _public_value_surface(
                getattr(tm_contracts_module, name)
            ),
        }
        for name in sorted(tm_contracts_module.__all__)
    ]
    alias_value = getattr(tm_contracts_module.TMContract, "__value__")
    union_members = tuple(
        _qualified_name(member) for member in get_args(alias_value)
    )
    valid_health = StoreHealth(
        healthy=True,
        schema_version=1,
        generation=0,
        record_count=0,
        index_kind="UNINDEXED",
        snapshot_binding_digest=None,
        source_binding_state=SourceBindingState.VERIFIED_CURRENT,
        exact_available=True,
        context_available=False,
        fuzzy_available=False,
        diagnostic_codes=(),
    )
    try:
        StoreHealth(
            healthy=True,
            schema_version=1,
            generation=0,
            record_count=0,
            index_kind="UNINDEXED",
            snapshot_binding_digest=None,
            source_binding_state=SourceBindingState.VERIFIED_CURRENT,
            exact_available=False,
            context_available=False,
            fuzzy_available=True,
            diagnostic_codes=(),
        )
    except (TypeError, ValueError) as error:
        invalid_gate_error_type = type(error).__name__
    else:
        invalid_gate_error_type = "NOT_REJECTED"
    return {
        "surface_version": "tm-contract-public-surface-v1",
        "public_items": public_items,
        "store_health_probe": {
            "invalid_gate_error_type": invalid_gate_error_type,
            "valid_exact_available": valid_health.exact_available,
        },
        "tm_contract_union_members": list(union_members),
    }


def _public_value_surface(value: object) -> dict[str, object]:
    if isinstance(value, type) and issubclass(value, Enum):
        return {
            "kind": "enum",
            "members": [
                [member.name, member.value] for member in value
            ],
        }
    if isinstance(value, type) and is_dataclass(value):
        return {
            "kind": "frozen_dataclass",
            "fields": [
                {
                    "name": item.name,
                    "type": _annotation_name(item.type),
                    "has_default": (
                        item.default is not MISSING
                        or item.default_factory is not MISSING
                    ),
                    "init": item.init,
                    "kw_only": item.kw_only,
                }
                for item in fields(value)
            ],
        }
    if isinstance(value, type):
        members: list[dict[str, object]] = []
        for name, member in sorted(value.__dict__.items()):
            if name.startswith("_"):
                continue
            if isinstance(member, property):
                members.append(
                    {
                        "kind": "property",
                        "name": name,
                        "signature": _callable_surface(member.fget),
                    }
                )
            elif callable(member):
                members.append(
                    {
                        "kind": "callable",
                        "name": name,
                        "signature": _callable_surface(member),
                    }
                )
        return {
            "kind": (
                "protocol" if getattr(value, "_is_protocol", False)
                else "class"
            ),
            "members": members,
        }
    if callable(value):
        return {
            "kind": "callable",
            "signature": _callable_surface(value),
        }
    if type(value).__name__ == "TypeAliasType":
        alias_value = getattr(value, "__value__")
        return {
            "kind": "type_alias",
            "members": [
                _qualified_name(member) for member in get_args(alias_value)
            ],
        }
    return {
        "kind": "constant",
        "type": type(value).__name__,
        "value": cast(object, value),
    }


def _callable_surface(value: object) -> dict[str, object]:
    callable_signature = inspect.signature(
        cast(Callable[..., object], value)
    )
    return {
        "parameters": [
            {
                "annotation": _annotation_name(parameter.annotation),
                "has_default": parameter.default is not inspect.Signature.empty,
                "kind": parameter.kind.name,
                "name": parameter.name,
            }
            for parameter in callable_signature.parameters.values()
        ],
        "return": _annotation_name(callable_signature.return_annotation),
    }


def _annotation_name(value: object) -> str:
    if value is inspect.Signature.empty:
        return "EMPTY"
    if isinstance(value, str):
        return value
    return _qualified_name(value)


def _qualified_name(value: object) -> str:
    name = getattr(value, "__qualname__", None)
    if isinstance(name, str):
        return name
    return str(value).replace("tm_contracts.", "")


def _capture_rejection(case_id: str, serialized: str) -> dict[str, str]:
    try:
        _ = contract_from_json(serialized)
    except (TypeError, ValueError) as error:
        return {
            "case_id": case_id,
            "error_type": type(error).__name__,
        }
    return {"case_id": case_id, "error_type": "NOT_REJECTED"}


def _similarity_transcript(repository_root: Path) -> dict[str, object]:
    fixture = _load_json(
        repository_root / "tests/fixtures/tm_similarity_vectors.json"
    )
    scorer = SimilarityScorerV1()
    observed: list[dict[str, object]] = []
    for raw in _require_list(fixture.get("vectors"), "similarity vectors"):
        vector = require_mapping(raw, "similarity vector")
        vector_id = require_string(vector.get("id"), "similarity vector id")
        query = _require_text_string(vector.get("query_raw"), "query_raw")
        candidate = _require_text_string(
            vector.get("candidate_raw"),
            "candidate_raw",
        )
        try:
            evidence = scorer.score(query, candidate)
            outcome: dict[str, object] = {
                "dice_bigram": evidence.dice_bigram,
                "final_similarity": evidence.final_similarity,
                "levenshtein_ratio": evidence.levenshtein_ratio,
                "scorer_version": evidence.scorer_version,
            }
        except (TypeError, ValueError) as error:
            outcome = {"error_type": type(error).__name__}
        observed.append({"id": vector_id, "outcome": outcome})
    return {
        "algorithm_version": fixture.get("algorithm_version"),
        "fixture_version": fixture.get("fixture_version"),
        "observed": observed,
    }


def _all_matcher_transcript(repository_root: Path) -> dict[str, object]:
    fixture = _load_json(
        repository_root / "tests/fixtures/text_matcher_v1_vectors.json"
    )
    vectors = tuple(
        require_mapping(item, "text matcher vector")
        for item in _require_list(fixture.get("vectors"), "matcher vectors")
    )
    return _run_matcher_vectors(
        vectors,
        transcript_version="text-matcher-all-v1",
    )


def basic_matcher_cohort_transcript(
    repository_root: Path,
) -> dict[str, object]:
    fixture = _load_json(
        repository_root / "tests/fixtures/text_matcher_v1_vectors.json"
    )
    ids = {
        "legacy-case-sensitive-contiguous",
        "basic-unicode-casefold-contiguous",
        "sharp-s-partial-expansion-deduplicates-span",
        "empty-query-legacy",
        "empty-query-basic",
    }
    vectors = tuple(
        vector
        for vector in (
            require_mapping(item, "text matcher vector")
            for item in _require_list(
                fixture.get("vectors"),
                "matcher vectors",
            )
        )
        if vector.get("id") in ids
    )
    transcript = _run_matcher_vectors(
        vectors,
        transcript_version="matcher-basic-cohort-v1",
    )
    stable_hits = TextMatcherV1().match(
        text="ßß SS",
        query="s",
        profile=TextMatchProfile.BASIC_CONTIGUOUS,
        options=SearchOptions(match_case=False, whole_word=False),
    )
    return {
        **transcript,
        "stable_order_probe": [
            [hit.start_index, hit.end_index] for hit in stable_hits
        ],
    }


def full_matcher_cohort_transcript(
    repository_root: Path,
) -> dict[str, object]:
    fixture = _load_json(
        repository_root / "tests/fixtures/text_matcher_v1_vectors.json"
    )
    vectors = tuple(
        vector
        for vector in (
            require_mapping(item, "text matcher vector")
            for item in _require_list(
                fixture.get("vectors"),
                "matcher vectors",
            )
        )
        if vector.get("profile")
        == TextMatchProfile.CONFIGURABLE_TEXT_V1.value
    )
    return _run_matcher_vectors(
        vectors,
        transcript_version="matcher-text-v1-cohort-v1",
    )


def _run_matcher_vectors(
    vectors: tuple[Mapping[str, ValidationJsonValue], ...],
    *,
    transcript_version: str,
) -> dict[str, object]:
    matcher = TextMatcherV1()
    observed: list[dict[str, object]] = []
    for vector in vectors:
        raw_options = require_mapping(
            vector.get("options"),
            "matcher options",
        )
        options = SearchOptions(
            match_case=_require_boolean(
                raw_options.get("match_case"),
                "match_case",
            ),
            whole_word=_require_boolean(
                raw_options.get("whole_word"),
                "whole_word",
            ),
        )
        hits = matcher.match(
            text=_require_text_string(vector.get("text"), "matcher text"),
            query=_require_text_string(vector.get("query"), "matcher query"),
            profile=TextMatchProfile(
                require_string(
                    vector.get("profile"),
                    "matcher profile",
                )
            ),
            options=options,
        )
        observed.append(
            {
                "hits": [
                    [hit.start_index, hit.end_index] for hit in hits
                ],
                "id": require_string(vector.get("id"), "matcher id"),
            }
        )
    return {
        "observed": observed,
        "transcript_version": transcript_version,
    }


def unicode_transcript(repository_root: Path) -> dict[str, object]:
    fixture = _load_json(
        repository_root
        / "tests/fixtures/text_matcher_unicode_vectors.json"
    )
    fold_observed: list[dict[str, object]] = []
    for raw in _require_list(fixture.get("fold_vectors"), "fold vectors"):
        vector = require_mapping(raw, "fold vector")
        projection = fold_text_v1(
            require_string(vector.get("raw"), "fold raw")
        )
        fold_observed.append(
            {
                "folded": projection.folded_text,
                "id": require_string(vector.get("id"), "fold id"),
                "source_spans": [
                    list(span) for span in projection.source_spans
                ],
            }
        )

    projection_observed: list[dict[str, object]] = []
    for raw in _require_list(
        fixture.get("projection_vectors"),
        "projection vectors",
    ):
        vector = require_mapping(raw, "projection vector")
        projection = fold_text_v1(
            require_string(vector.get("raw"), "projection raw")
        )
        span = project_folded_span_v1(
            projection,
            _require_integer(vector.get("folded_start"), "folded_start"),
            _require_integer(vector.get("folded_end"), "folded_end"),
        )
        projection_observed.append(
            {
                "id": require_string(vector.get("id"), "projection id"),
                "source_span": list(span) if span is not None else None,
            }
        )

    boundary_observed: list[dict[str, object]] = []
    for raw in _require_list(
        fixture.get("word_boundary_vectors"),
        "word boundary vectors",
    ):
        vector = require_mapping(raw, "word boundary vector")
        boundary_observed.append(
            {
                "boundaries": list(
                    word_boundaries_v1(
                        _require_text_string(
                            vector.get("text"),
                            "boundary text",
                        )
                    )
                ),
                "id": require_string(vector.get("id"), "boundary id"),
            }
        )

    cjk_observed: list[dict[str, object]] = []
    for raw in _require_list(
        fixture.get("pure_cjk_vectors"),
        "pure CJK vectors",
    ):
        vector = require_mapping(raw, "pure CJK vector")
        cjk_observed.append(
            {
                "id": require_string(vector.get("id"), "pure CJK id"),
                "is_pure_cjk": is_pure_cjk_v1(
                    _require_text_string(
                        vector.get("query"),
                        "pure CJK query",
                    )
                ),
            }
        )

    conformance = _word_break_conformance_transcript(repository_root)
    return {
        "fold": fold_observed,
        "projection": projection_observed,
        "pure_cjk": cjk_observed,
        "transcript_version": "matcher-unicode-cohort-v1",
        "word_boundary": boundary_observed,
        "word_break_conformance": conformance,
    }


def _word_break_conformance_transcript(
    repository_root: Path,
) -> dict[str, object]:
    path = (
        repository_root
        / "tests/fixtures/unicode-16.0.0-WordBreakTest.txt"
    )
    transcript_hasher = hashlib.sha256()
    checked = 0
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        payload = raw_line.split("#", 1)[0].strip()
        if not payload:
            continue
        tokens = payload.split()
        code_points: list[int] = []
        for token in tokens:
            if token not in {"÷", "×"}:
                code_points.append(int(token, 16))
        text = "".join(chr(code_point) for code_point in code_points)
        transcript_hasher.update(
            _canonical_json(
                {
                    "boundaries": list(word_boundaries_v1(text)),
                    "line": line_number,
                }
            ).encode("utf-8")
        )
        transcript_hasher.update(b"\n")
        checked += 1
    return {
        "checked": checked,
        "observed_digest": transcript_hasher.hexdigest(),
    }


def load_approved_roots(
    path: Path,
) -> Mapping[str, ValidationJsonValue]:
    payload = _load_json(_require_file(path, "approved_roots_path"))
    if payload.get("schema_version") != GATE_A_SCHEMA_VERSION:
        raise ValueError("unsupported Gate A roots schema")
    if set(payload) != {"schema_version", "components", "matcher"}:
        raise ValueError("Gate A roots fields are not closed")
    return payload


def _load_json(path: Path) -> dict[str, ValidationJsonValue]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def reject_duplicates(
        pairs: list[tuple[str, ValidationJsonValue]],
    ) -> dict[str, ValidationJsonValue]:
        result: dict[str, ValidationJsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
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
        raise ValueError("validation JSON is invalid") from None
    if not isinstance(loaded, dict):
        raise ValueError("validation JSON root must be an object")
    return cast(dict[str, ValidationJsonValue], loaded)


def aggregate_paths_digest(
    repository_root: Path,
    relative_paths: tuple[str, ...],
) -> str:
    entries: list[dict[str, str]] = []
    for relative in relative_paths:
        path = repository_root / relative
        entries.append(
            {
                "path": relative,
                "sha256": _sha256(path.read_bytes()),
            }
        )
    return canonical_digest(entries)


def canonical_digest(value: object) -> str:
    return _sha256(_canonical_json(value).encode("utf-8"))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_digest(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _DIGEST_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def require_mapping(
    value: object,
    field_name: str,
) -> Mapping[str, ValidationJsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return cast(Mapping[str, ValidationJsonValue], value)


def _require_list(
    value: object,
    field_name: str,
) -> list[ValidationJsonValue]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return cast(list[ValidationJsonValue], value)


def require_paths(
    value: object,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    items = _require_list(value, field_name)
    paths = tuple(
        require_string(item, field_name)
        for item in items
    )
    if not paths and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    if len(paths) != len(set(paths)) or paths != tuple(sorted(paths)):
        raise ValueError(f"{field_name} must be unique and sorted")
    for path in paths:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"{field_name} must contain safe relative paths")
    return paths


def require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_text_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _require_boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _require_integer(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _require_directory(value: Path, field_name: str) -> Path:
    if not value.is_dir():
        raise ValueError(f"{field_name} must be an existing directory")
    return value


def _require_file(value: Path, field_name: str) -> Path:
    if not value.is_file():
        raise ValueError(f"{field_name} must be an existing file")
    return value


__all__ = [
    "GATE_A_SCHEMA_VERSION",
    "GateAComponent",
    "GateAComponentEvidence",
    "GateAEvidenceReport",
    "contracts_public_surface_transcript",
    "recompute_gate_a",
]
