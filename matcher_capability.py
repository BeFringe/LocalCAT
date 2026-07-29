"""Core-internal matcher validation evaluator and snapshot publisher.

This module turns a closed validation manifest into one immutable public
capability snapshot.  It deliberately does not execute text matching; the
capability-gated execution port belongs to Task 2.5.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from threading import Lock
from typing import cast, final

from tm_contracts import (
    MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION,
    MATCHER_VALIDATION_SUMMARY_VERSION,
    MatcherValidationCohortEvidence,
    MatcherValidationManifest,
    MatcherValidationSummary,
    TextMatchProfile,
    TextMatcherCapability,
    TextMatcherState,
    matcher_validation_manifest_to_json,
)


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STRICT_UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z"
)
_UNAVAILABLE_REASON = "MATCHER.VALIDATION_UNAVAILABLE"
_BASIC_PROFILES = (
    TextMatchProfile.LEGACY_COMPAT,
    TextMatchProfile.BASIC_CONTIGUOUS,
)
_TEXT_V1_PROFILES = (
    TextMatchProfile.LEGACY_COMPAT,
    TextMatchProfile.BASIC_CONTIGUOUS,
    TextMatchProfile.CONFIGURABLE_TEXT_V1,
)


def _require_identity(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _require_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True)
class MatcherValidationCohortExpectation:
    """Expected identity and digest for one evidence cohort."""

    cohort_id: str
    cohort_digest: str

    def __post_init__(self) -> None:
        _ = _require_identity(
            self.cohort_id,
            "matcher expected cohort id",
        )
        _ = _require_digest(
            self.cohort_digest,
            "matcher expected cohort digest",
        )


def _require_expectation_tuple(
    value: object,
    field_name: str,
) -> tuple[MatcherValidationCohortExpectation, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    raw_items = cast(tuple[object, ...], value)
    if not raw_items:
        raise ValueError(f"{field_name} must not be empty")
    validated: list[MatcherValidationCohortExpectation] = []
    for item in raw_items:
        if not isinstance(item, MatcherValidationCohortExpectation):
            raise TypeError(
                f"{field_name} must contain cohort expectations"
            )
        validated.append(item)
    items = tuple(validated)
    ids = tuple(item.cohort_id for item in items)
    if len(ids) != len(set(ids)):
        raise ValueError(f"{field_name} cohort ids must be unique")
    if ids != tuple(sorted(ids)):
        raise ValueError(f"{field_name} must use stable cohort id order")
    return items


@dataclass(frozen=True)
class MatcherValidationExpectation:
    """Frozen current implementation/evidence identity used for evaluation."""

    evidence_schema_version: str
    matcher_artifact_digest: str
    matcher_build_digest: str
    semantics_version: str
    basic_cohorts: tuple[MatcherValidationCohortExpectation, ...]
    full_cohorts: tuple[MatcherValidationCohortExpectation, ...]
    fixture_digest: str
    evaluator_digest: str

    def __post_init__(self) -> None:
        if (
            self.evidence_schema_version
            != MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION
        ):
            raise ValueError("unsupported matcher evidence schema version")
        _ = _require_digest(
            self.matcher_artifact_digest,
            "matcher expected artifact digest",
        )
        _ = _require_digest(
            self.matcher_build_digest,
            "matcher expected build digest",
        )
        _ = _require_identity(
            self.semantics_version,
            "matcher expected semantics version",
        )
        basic = _require_expectation_tuple(
            self.basic_cohorts,
            "matcher expected basic cohorts",
        )
        full = _require_expectation_tuple(
            self.full_cohorts,
            "matcher expected full cohorts",
        )
        basic_ids = {item.cohort_id for item in basic}
        full_ids = {item.cohort_id for item in full}
        if not basic_ids.isdisjoint(full_ids):
            raise ValueError(
                "matcher basic and full cohort ids must be disjoint"
            )
        _ = _require_digest(
            self.fixture_digest,
            "matcher expected fixture digest",
        )
        _ = _require_digest(
            self.evaluator_digest,
            "matcher expected evaluator digest",
        )


def _parse_manifest_utc(value: str) -> datetime:
    if _STRICT_UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("matcher evidence timestamp must be strict UTC")
    return datetime.strptime(
        value,
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=timezone.utc)


def _require_utc_instant(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(
            "evaluated_at_utc must be a timezone-aware UTC datetime"
        )
    return value


def _is_active_window(
    *,
    generated_at_utc: str,
    valid_until_utc: str,
    evaluated_at_utc: datetime,
) -> bool:
    generated = _parse_manifest_utc(generated_at_utc)
    valid_until = _parse_manifest_utc(valid_until_utc)
    return generated <= evaluated_at_utc < valid_until


def _expectation_payload(
    expectation: MatcherValidationExpectation,
) -> dict[str, object]:
    def encode_cohorts(
        cohorts: tuple[MatcherValidationCohortExpectation, ...],
    ) -> list[dict[str, str]]:
        return [
            {
                "cohort_digest": cohort.cohort_digest,
                "cohort_id": cohort.cohort_id,
            }
            for cohort in cohorts
        ]

    return {
        "basic_cohorts": encode_cohorts(expectation.basic_cohorts),
        "evaluator_digest": expectation.evaluator_digest,
        "evidence_schema_version": expectation.evidence_schema_version,
        "fixture_digest": expectation.fixture_digest,
        "full_cohorts": encode_cohorts(expectation.full_cohorts),
        "matcher_artifact_digest": expectation.matcher_artifact_digest,
        "matcher_build_digest": expectation.matcher_build_digest,
        "semantics_version": expectation.semantics_version,
    }


def _expectation_digest(
    expectation: MatcherValidationExpectation,
) -> str:
    canonical = json.dumps(
        _expectation_payload(expectation),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _clone_expectation(
    expectation: MatcherValidationExpectation,
) -> MatcherValidationExpectation:
    """Detach publisher authority from caller-owned live object aliases."""

    def clone_cohorts(
        cohorts: tuple[MatcherValidationCohortExpectation, ...],
    ) -> tuple[MatcherValidationCohortExpectation, ...]:
        return tuple(
            MatcherValidationCohortExpectation(
                cohort_id=cohort.cohort_id,
                cohort_digest=cohort.cohort_digest,
            )
            for cohort in cohorts
        )

    return MatcherValidationExpectation(
        evidence_schema_version=expectation.evidence_schema_version,
        matcher_artifact_digest=expectation.matcher_artifact_digest,
        matcher_build_digest=expectation.matcher_build_digest,
        semantics_version=expectation.semantics_version,
        basic_cohorts=clone_cohorts(expectation.basic_cohorts),
        full_cohorts=clone_cohorts(expectation.full_cohorts),
        fixture_digest=expectation.fixture_digest,
        evaluator_digest=expectation.evaluator_digest,
    )


def _capability_summary(
    *,
    canonical_manifest_json: str,
    expectation: MatcherValidationExpectation,
    state: TextMatcherState,
    profiles: tuple[TextMatchProfile, ...],
) -> MatcherValidationSummary:
    payload = {
        "canonical_manifest": json.loads(canonical_manifest_json),
        "derived_capability": {
            "profiles": [profile.value for profile in profiles],
            "semantics_version": expectation.semantics_version,
            "state": state.value,
        },
        "frozen_expectation": _expectation_payload(expectation),
        "summary_version": MATCHER_VALIDATION_SUMMARY_VERSION,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return MatcherValidationSummary(
        summary_version=MATCHER_VALIDATION_SUMMARY_VERSION,
        evidence_digest=hashlib.sha256(canonical).hexdigest(),
    )


def _unavailable_capability() -> TextMatcherCapability:
    return TextMatcherCapability(
        state=TextMatcherState.UNAVAILABLE,
        semantics_version=None,
        supported_profiles=(),
        validation_summary=None,
        unavailable_reason=_UNAVAILABLE_REASON,
    )


def _require_manifest(
    value: object,
) -> MatcherValidationManifest:
    if not isinstance(value, MatcherValidationManifest):
        raise TypeError(
            "manifest must be MatcherValidationManifest or None"
        )
    return value


def _require_expectation(
    value: object,
) -> MatcherValidationExpectation:
    if not isinstance(value, MatcherValidationExpectation):
        raise TypeError(
            "expectation must be MatcherValidationExpectation"
        )
    return value


def _require_evaluator(value: object) -> MatcherCapabilityEvaluator:
    if type(value) is not MatcherCapabilityEvaluator:
        raise TypeError("evaluator must be MatcherCapabilityEvaluator")
    return cast(MatcherCapabilityEvaluator, value)


def _cohorts_are_valid(
    *,
    evidence_by_id: Mapping[str, MatcherValidationCohortEvidence],
    expectations: tuple[MatcherValidationCohortExpectation, ...],
    evaluated_at_utc: datetime,
) -> bool:
    for expectation in expectations:
        evidence = evidence_by_id.get(expectation.cohort_id)
        if evidence is None:
            return False
        if (
            evidence.cohort_digest != expectation.cohort_digest
            or evidence.passed is not True
        ):
            return False
        if not _is_active_window(
            generated_at_utc=evidence.generated_at_utc,
            valid_until_utc=evidence.valid_until_utc,
            evaluated_at_utc=evaluated_at_utc,
        ):
            return False
    return True


@final
class MatcherCapabilityEvaluator:
    """The only Core state decision from validation evidence."""

    __slots__: tuple[str, ...] = ("__expectation",)

    def __init__(
        self,
        expectation: MatcherValidationExpectation,
    ) -> None:
        self.__expectation = _require_expectation(expectation)

    @property
    def expectation(self) -> MatcherValidationExpectation:
        return self.__expectation

    def evaluate(
        self,
        manifest: MatcherValidationManifest | None,
        *,
        evaluated_at_utc: datetime,
    ) -> TextMatcherCapability:
        """Derive one fail-closed snapshot at an explicit UTC instant."""

        instant = _require_utc_instant(evaluated_at_utc)
        if manifest is None:
            return _unavailable_capability()
        validated_manifest = _require_manifest(manifest)

        try:
            canonical_manifest_json = (
                matcher_validation_manifest_to_json(validated_manifest)
            )
            manifest_is_active = _is_active_window(
                generated_at_utc=validated_manifest.generated_at_utc,
                valid_until_utc=validated_manifest.valid_until_utc,
                evaluated_at_utc=instant,
            )
        except (TypeError, ValueError):
            return _unavailable_capability()

        expectation = self.__expectation
        if (
            validated_manifest.evidence_schema_version
            != expectation.evidence_schema_version
            or validated_manifest.matcher_artifact_digest
            != expectation.matcher_artifact_digest
            or validated_manifest.matcher_build_digest
            != expectation.matcher_build_digest
            or validated_manifest.semantics_version
            != expectation.semantics_version
            or validated_manifest.fixture_digest
            != expectation.fixture_digest
            or validated_manifest.evaluator_digest
            != expectation.evaluator_digest
            or not manifest_is_active
        ):
            return _unavailable_capability()

        basic_ids = tuple(
            cohort.cohort_id for cohort in expectation.basic_cohorts
        )
        complete_ids = tuple(
            sorted(
                basic_ids
                + tuple(
                    cohort.cohort_id
                    for cohort in expectation.full_cohorts
                )
            )
        )
        required_ids = set(validated_manifest.required_cohort_ids)
        expected_basic_ids = set(basic_ids)
        allowed_ids = set(complete_ids)
        if (
            not expected_basic_ids.issubset(required_ids)
            or not required_ids.issubset(allowed_ids)
        ):
            return _unavailable_capability()

        evidence_by_id = {
            evidence.cohort_id: evidence
            for evidence in validated_manifest.cohort_evidence
        }
        if not _cohorts_are_valid(
            evidence_by_id=evidence_by_id,
            expectations=expectation.basic_cohorts,
            evaluated_at_utc=instant,
        ):
            return _unavailable_capability()

        state = TextMatcherState.BASIC_VALIDATED
        profiles = _BASIC_PROFILES
        if (
            validated_manifest.required_cohort_ids == complete_ids
            and _cohorts_are_valid(
                evidence_by_id=evidence_by_id,
                expectations=expectation.full_cohorts,
                evaluated_at_utc=instant,
            )
        ):
            state = TextMatcherState.TEXT_V1_VALIDATED
            profiles = _TEXT_V1_PROFILES

        return TextMatcherCapability(
            state=state,
            semantics_version=expectation.semantics_version,
            supported_profiles=profiles,
            validation_summary=_capability_summary(
                canonical_manifest_json=canonical_manifest_json,
                expectation=expectation,
                state=state,
                profiles=profiles,
            ),
            unavailable_reason=None,
        )


@final
class MatcherCapabilityPublisher:
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
        evaluator: MatcherCapabilityEvaluator,
        *,
        initial_manifest: MatcherValidationManifest | None,
        evaluated_at_utc: datetime,
    ) -> None:
        validated_evaluator = _require_evaluator(evaluator)
        expectation = _clone_expectation(
            validated_evaluator.expectation
        )
        private_evaluator = MatcherCapabilityEvaluator(expectation)
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

    def snapshot(self) -> TextMatcherCapability:
        """Return one immutable snapshot reference under the read lock."""

        with self.__lock:
            return self.__snapshot

    @property
    def semantics_version(self) -> str:
        """Return the frozen semantics identity accepted by this publisher."""

        return self.__semantics_version

    def __trusted_evaluator(
        self,
    ) -> MatcherCapabilityEvaluator | None:
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
        manifest: MatcherValidationManifest | None,
        *,
        evaluated_at_utc: datetime,
    ) -> TextMatcherCapability:
        """Evaluate a manifest, then atomically publish that exact result."""

        evaluator = self.__trusted_evaluator()
        if evaluator is None:
            next_snapshot = _unavailable_capability()
        else:
            next_snapshot = evaluator.evaluate(
                manifest,
                evaluated_at_utc=evaluated_at_utc,
            )
        with self.__lock:
            trusted_at_publish = self.__trusted_evaluator()
            semantics_match = (
                next_snapshot.state is TextMatcherState.UNAVAILABLE
                or next_snapshot.semantics_version
                == self.__semantics_version
            )
            if (
                trusted_at_publish is not evaluator
                or not semantics_match
            ):
                next_snapshot = _unavailable_capability()
            self.__snapshot = next_snapshot
            return self.__snapshot


__all__ = [
    "MatcherCapabilityEvaluator",
    "MatcherCapabilityPublisher",
    "MatcherValidationCohortExpectation",
    "MatcherValidationExpectation",
]
