"""Recompute short-lived matcher evidence from approved release roots.

No manifest is persisted by this module.  Every publication reruns the
versioned cohorts, records observed digests, and delegates the only readiness
decision to ``MatcherCapabilityEvaluator``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from capability_gated_text_matcher import CapabilityGatedTextMatcherV1
from matcher_capability import (
    MatcherCapabilityEvaluator,
    MatcherCapabilityPublisher,
    MatcherValidationCohortExpectation,
    MatcherValidationExpectation,
)
from text_matcher import TEXT_MATCHER_SEMANTICS_VERSION
from tm_contracts import (
    MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION,
    MatcherValidationCohortEvidence,
    MatcherValidationManifest,
)
from tm_gate_a import (
    ValidationJsonValue,
    aggregate_paths_digest,
    basic_matcher_cohort_transcript,
    canonical_digest,
    full_matcher_cohort_transcript,
    load_approved_roots,
    require_digest,
    require_mapping,
    require_paths,
    require_string,
    unicode_transcript,
)


_MAX_EVIDENCE_TTL = timedelta(days=30)
_DEFAULT_APPROVED_ROOTS = (
    Path(__file__).parent
    / "tests"
    / "fixtures"
    / "feature5_gate_a_v1.json"
)


@dataclass(frozen=True)
class MatcherValidationRelease:
    """Observed manifest plus its immutable approved expectation."""

    expectation: MatcherValidationExpectation
    manifest: MatcherValidationManifest | None


def recompute_matcher_validation(
    *,
    repository_root: Path,
    approved_roots_path: Path = _DEFAULT_APPROVED_ROOTS,
    generated_at_utc: datetime,
    valid_until_utc: datetime,
    include_full: bool,
) -> MatcherValidationRelease:
    """Execute matcher cohorts and build one non-persisted evidence manifest."""

    if not isinstance(repository_root, Path) or not repository_root.is_dir():
        raise ValueError("repository_root must be an existing directory")
    if not isinstance(include_full, bool):
        raise TypeError("include_full must be a boolean")
    generated = _require_utc(generated_at_utc, "generated_at_utc")
    valid_until = _require_utc(valid_until_utc, "valid_until_utc")
    lifetime = valid_until - generated
    if lifetime <= timedelta(0):
        raise ValueError("valid_until_utc must be later than generated_at_utc")
    if lifetime > _MAX_EVIDENCE_TTL:
        raise ValueError("matcher evidence TTL must not exceed 30 days")

    approved = load_approved_roots(approved_roots_path)
    matcher = require_mapping(approved["matcher"], "approved matcher")
    if set(matcher) != {
        "artifact_paths",
        "basic_cohorts",
        "build_paths",
        "evaluator_digest",
        "evaluator_path",
        "fixture_digest",
        "fixture_paths",
        "full_cohorts",
        "matcher_artifact_digest",
        "matcher_build_digest",
        "semantics_version",
    }:
        raise ValueError("approved matcher fields are not closed")
    expectation = _expectation_from_approved(matcher)
    artifact_paths = require_paths(
        matcher.get("artifact_paths"),
        "matcher artifact_paths",
    )
    build_paths = require_paths(
        matcher.get("build_paths"),
        "matcher build_paths",
    )
    fixture_paths = require_paths(
        matcher.get("fixture_paths"),
        "matcher fixture_paths",
    )
    evaluator_path = require_string(
        matcher.get("evaluator_path"),
        "matcher evaluator_path",
    )

    try:
        observed_cohorts = {
            "matcher-basic-v1": canonical_digest(
                basic_matcher_cohort_transcript(repository_root)
            )
        }
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
    except Exception:
        # BASIC evidence or common build identity is incomplete.  There is no
        # lower capability state that can safely execute a request.
        return MatcherValidationRelease(
            expectation=expectation,
            manifest=None,
        )

    selected_expectations = expectation.basic_cohorts
    if include_full:
        selected_expectations += expectation.full_cohorts
        for item in expectation.full_cohorts:
            try:
                observed_cohorts[item.cohort_id] = (
                    _observe_full_cohort_digest(
                        item.cohort_id,
                        repository_root,
                    )
                )
            except Exception:
                # Full-only evidence fails independently so valid BASIC
                # evidence can still publish BASIC_VALIDATED.
                observed_cohorts[item.cohort_id] = canonical_digest(
                    {
                        "cohort_id": item.cohort_id,
                        "observation": "EXECUTION_FAILED",
                    }
                )
    selected_expectations = tuple(
        sorted(selected_expectations, key=lambda item: item.cohort_id)
    )
    try:
        generated_text = _format_utc(generated)
        valid_until_text = _format_utc(valid_until)
        evidence = tuple(
            MatcherValidationCohortEvidence(
                cohort_id=item.cohort_id,
                cohort_digest=observed_cohorts[item.cohort_id],
                passed=(
                    observed_cohorts[item.cohort_id]
                    == item.cohort_digest
                ),
                generated_at_utc=generated_text,
                valid_until_utc=valid_until_text,
            )
            for item in selected_expectations
        )
        manifest: MatcherValidationManifest | None = (
            MatcherValidationManifest(
                evidence_schema_version=(
                    MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION
                ),
                matcher_artifact_digest=observed_artifact_digest,
                matcher_build_digest=observed_build_digest,
                semantics_version=TEXT_MATCHER_SEMANTICS_VERSION,
                required_cohort_ids=tuple(
                    item.cohort_id for item in selected_expectations
                ),
                cohort_evidence=evidence,
                fixture_digest=observed_fixture_digest,
                evaluator_digest=observed_evaluator_digest,
                generated_at_utc=generated_text,
                valid_until_utc=valid_until_text,
            )
        )
    except Exception:
        # Approved roots remain authoritative and validated above.  Failure to
        # observe current source/fixtures cannot mint partial evidence.
        manifest = None
    return MatcherValidationRelease(
        expectation=expectation,
        manifest=manifest,
    )


def _observe_full_cohort_digest(
    cohort_id: str,
    repository_root: Path,
) -> str:
    if cohort_id == "matcher-text-v1":
        fixture_paths = (
            "tests/fixtures/text_matcher_v1_vectors.json",
        )
        transcript = full_matcher_cohort_transcript(repository_root)
    elif cohort_id == "matcher-unicode-v1":
        fixture_paths = (
            "tests/fixtures/text_matcher_unicode_vectors.json",
            "tests/fixtures/unicode-16.0.0-WordBreakTest.txt",
        )
        transcript = unicode_transcript(repository_root)
    else:
        raise ValueError("approved full matcher cohort is unsupported")
    return canonical_digest(
        {
            "fixture_digest": aggregate_paths_digest(
                repository_root,
                fixture_paths,
            ),
            "transcript": transcript,
        }
    )


def build_validated_matcher_v1(
    *,
    repository_root: Path,
    approved_roots_path: Path = _DEFAULT_APPROVED_ROOTS,
    generated_at_utc: datetime,
    valid_until_utc: datetime,
    evaluated_at_utc: datetime,
    include_full: bool,
) -> CapabilityGatedTextMatcherV1:
    """Rerun evidence, then construct the only capability-gated matcher."""

    evaluated = _require_utc(evaluated_at_utc, "evaluated_at_utc")
    release = recompute_matcher_validation(
        repository_root=repository_root,
        approved_roots_path=approved_roots_path,
        generated_at_utc=generated_at_utc,
        valid_until_utc=valid_until_utc,
        include_full=include_full,
    )
    evaluator = MatcherCapabilityEvaluator(release.expectation)
    publisher = MatcherCapabilityPublisher(
        evaluator,
        initial_manifest=release.manifest,
        evaluated_at_utc=evaluated,
    )
    return CapabilityGatedTextMatcherV1(publisher)


def _expectation_from_approved(
    matcher: Mapping[str, ValidationJsonValue],
) -> MatcherValidationExpectation:
    if (
        require_string(
            matcher.get("semantics_version"),
            "matcher semantics_version",
        )
        != TEXT_MATCHER_SEMANTICS_VERSION
    ):
        raise ValueError("approved matcher semantics version is unsupported")
    basic = _cohort_expectations(
        matcher.get("basic_cohorts"),
        "matcher basic_cohorts",
    )
    full = _cohort_expectations(
        matcher.get("full_cohorts"),
        "matcher full_cohorts",
    )
    return MatcherValidationExpectation(
        evidence_schema_version=(
            MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION
        ),
        matcher_artifact_digest=require_digest(
            matcher.get("matcher_artifact_digest"),
            "matcher matcher_artifact_digest",
        ),
        matcher_build_digest=require_digest(
            matcher.get("matcher_build_digest"),
            "matcher matcher_build_digest",
        ),
        semantics_version=TEXT_MATCHER_SEMANTICS_VERSION,
        basic_cohorts=basic,
        full_cohorts=full,
        fixture_digest=require_digest(
            matcher.get("fixture_digest"),
            "matcher fixture_digest",
        ),
        evaluator_digest=require_digest(
            matcher.get("evaluator_digest"),
            "matcher evaluator_digest",
        ),
    )


def _cohort_expectations(
    value: object,
    field_name: str,
) -> tuple[MatcherValidationCohortExpectation, ...]:
    raw = require_mapping(value, field_name)
    if not raw:
        raise ValueError(f"{field_name} must not be empty")
    ids = tuple(raw)
    if ids != tuple(sorted(ids)):
        raise ValueError(f"{field_name} must use stable cohort id order")
    return tuple(
        MatcherValidationCohortExpectation(
            cohort_id=cohort_id,
            cohort_digest=require_digest(
                raw[cohort_id],
                f"{field_name} cohort digest",
            ),
        )
        for cohort_id in ids
    )


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


__all__ = [
    "MatcherValidationRelease",
    "build_validated_matcher_v1",
    "recompute_matcher_validation",
]
