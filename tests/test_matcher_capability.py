from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any, ClassVar, cast
import unittest

import tm_contracts
from matcher_capability import (
    MatcherCapabilityEvaluator,
    MatcherCapabilityPublisher,
    MatcherValidationCohortExpectation,
    MatcherValidationExpectation,
)
from tm_contracts import (
    MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION,
    MATCHER_VALIDATION_SUMMARY_VERSION,
    MatcherValidationCohortEvidence,
    MatcherValidationManifest,
    TextMatchProfile,
    TextMatcherCapability,
    TextMatcherState,
)


_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "matcher_capability_v1_vectors.json"
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


def _load_vectors() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(_FIXTURE_PATH.read_text(encoding="utf-8")),
    )


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(
        value,
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=timezone.utc)


def _cohort_expectation(
    payload: dict[str, Any],
) -> MatcherValidationCohortExpectation:
    return MatcherValidationCohortExpectation(
        cohort_id=cast(str, payload["cohort_id"]),
        cohort_digest=cast(str, payload["cohort_digest"]),
    )


def _expectation(
    vectors: dict[str, Any] | None = None,
) -> MatcherValidationExpectation:
    payload = cast(
        dict[str, Any],
        (vectors or _load_vectors())["expectation"],
    )
    return MatcherValidationExpectation(
        evidence_schema_version=cast(
            str,
            payload["evidence_schema_version"],
        ),
        matcher_artifact_digest=cast(
            str,
            payload["matcher_artifact_digest"],
        ),
        matcher_build_digest=cast(
            str,
            payload["matcher_build_digest"],
        ),
        semantics_version=cast(str, payload["semantics_version"]),
        basic_cohorts=tuple(
            _cohort_expectation(cast(dict[str, Any], item))
            for item in cast(list[object], payload["basic_cohorts"])
        ),
        full_cohorts=tuple(
            _cohort_expectation(cast(dict[str, Any], item))
            for item in cast(list[object], payload["full_cohorts"])
        ),
        fixture_digest=cast(str, payload["fixture_digest"]),
        evaluator_digest=cast(str, payload["evaluator_digest"]),
    )


def _scenario_manifest(
    vectors: dict[str, Any],
    scenario: dict[str, Any],
) -> MatcherValidationManifest | None:
    kind = cast(str, scenario["manifest_kind"])
    if kind == "missing":
        return None

    expectation = _expectation(vectors)
    basic = expectation.basic_cohorts[0]
    full = expectation.full_cohorts
    full_ids = tuple(item.cohort_id for item in full)
    cohort_ids_by_kind = {
        "basic": (basic.cohort_id,),
        "basic_full": (basic.cohort_id,) + full_ids,
        "partial_full": (basic.cohort_id, full_ids[0]),
        "full_only": full_ids,
        "extra": (
            basic.cohort_id,
            "matcher-unknown-v1",
        )
        + full_ids,
    }
    cohort_ids = tuple(sorted(cohort_ids_by_kind[kind]))
    expected_digests = {
        basic.cohort_id: basic.cohort_digest,
        **{
            item.cohort_id: item.cohort_digest
            for item in full
        },
        "matcher-unknown-v1": "7" * 64,
    }
    cohort_overrides = cast(
        dict[str, dict[str, object]],
        scenario.get("cohort_overrides", {}),
    )
    window = cast(dict[str, str], vectors["manifest_window"])
    evidence = tuple(
        MatcherValidationCohortEvidence(
            cohort_id=cohort_id,
            cohort_digest=cast(
                str,
                cohort_overrides.get(cohort_id, {}).get(
                    "cohort_digest",
                    expected_digests[cohort_id],
                ),
            ),
            passed=cast(
                bool,
                cohort_overrides.get(cohort_id, {}).get(
                    "passed",
                    True,
                ),
            ),
            generated_at_utc=cast(
                str,
                cohort_overrides.get(cohort_id, {}).get(
                    "generated_at_utc",
                    window["generated_at_utc"],
                ),
            ),
            valid_until_utc=cast(
                str,
                cohort_overrides.get(cohort_id, {}).get(
                    "valid_until_utc",
                    window["valid_until_utc"],
                ),
            ),
        )
        for cohort_id in cohort_ids
    )
    values: dict[str, object] = {
        "evidence_schema_version": expectation.evidence_schema_version,
        "matcher_artifact_digest": expectation.matcher_artifact_digest,
        "matcher_build_digest": expectation.matcher_build_digest,
        "semantics_version": expectation.semantics_version,
        "required_cohort_ids": cohort_ids,
        "cohort_evidence": evidence,
        "fixture_digest": expectation.fixture_digest,
        "evaluator_digest": expectation.evaluator_digest,
        "generated_at_utc": window["generated_at_utc"],
        "valid_until_utc": window["valid_until_utc"],
    }
    values.update(
        cast(dict[str, object], scenario.get("manifest_overrides", {}))
    )
    manifest = MatcherValidationManifest(**cast(Any, values))
    for field_name, value in cast(
        dict[str, object],
        scenario.get("unsafe_manifest_overrides", {}),
    ).items():
        object.__setattr__(manifest, field_name, value)
    return manifest


class MatcherCapabilityTests(unittest.TestCase):
    vectors: ClassVar[dict[str, Any]] = _load_vectors()
    expectation: ClassVar[MatcherValidationExpectation] = _expectation(
        vectors
    )
    evaluator: ClassVar[MatcherCapabilityEvaluator] = (
        MatcherCapabilityEvaluator(expectation)
    )

    def test_versioned_state_matrix_is_closed_and_fail_safe(self) -> None:
        self.assertEqual(
            self.vectors["fixture_version"],
            "matcher-capability-v1",
        )
        states_seen: set[TextMatcherState] = set()
        for raw_scenario in cast(
            list[object],
            self.vectors["state_matrix"],
        ):
            scenario = cast(dict[str, Any], raw_scenario)
            with self.subTest(scenario=scenario["id"]):
                capability = self.evaluator.evaluate(
                    _scenario_manifest(self.vectors, scenario),
                    evaluated_at_utc=_parse_utc(
                        cast(str, scenario["evaluated_at_utc"])
                    ),
                )
                expected_state = TextMatcherState(
                    cast(str, scenario["expected_state"])
                )
                states_seen.add(capability.state)
                self.assertEqual(capability.state, expected_state)
                if expected_state is TextMatcherState.UNAVAILABLE:
                    self.assertEqual(capability.supported_profiles, ())
                    self.assertIsNone(capability.semantics_version)
                    self.assertIsNone(capability.validation_summary)
                    self.assertEqual(
                        capability.unavailable_reason,
                        "MATCHER.VALIDATION_UNAVAILABLE",
                    )
                else:
                    self.assertEqual(
                        capability.semantics_version,
                        self.expectation.semantics_version,
                    )
                    self.assertIsNotNone(capability.validation_summary)
                    self.assertIsNone(capability.unavailable_reason)
        self.assertEqual(states_seen, set(TextMatcherState))

    def test_available_profiles_are_fixed_by_the_derived_state(self) -> None:
        scenarios = {
            cast(str, scenario["id"]): cast(dict[str, Any], scenario)
            for scenario in cast(
                list[dict[str, Any]],
                self.vectors["state_matrix"],
            )
        }
        basic = self.evaluator.evaluate(
            _scenario_manifest(
                self.vectors,
                scenarios["basic_only_valid"],
            ),
            evaluated_at_utc=_parse_utc("2030-01-15T00:00:00Z"),
        )
        full = self.evaluator.evaluate(
            _scenario_manifest(
                self.vectors,
                scenarios["basic_and_full_valid"],
            ),
            evaluated_at_utc=_parse_utc("2030-01-15T00:00:00Z"),
        )
        self.assertEqual(
            basic.supported_profiles,
            (
                TextMatchProfile.LEGACY_COMPAT,
                TextMatchProfile.BASIC_CONTIGUOUS,
            ),
        )
        self.assertEqual(
            full.supported_profiles,
            (
                TextMatchProfile.LEGACY_COMPAT,
                TextMatchProfile.BASIC_CONTIGUOUS,
                TextMatchProfile.CONFIGURABLE_TEXT_V1,
            ),
        )

    def test_evaluation_instant_is_required_explicit_and_strictly_utc(
        self,
    ) -> None:
        scenario = cast(
            dict[str, Any],
            cast(list[object], self.vectors["state_matrix"])[2],
        )
        manifest = _scenario_manifest(self.vectors, scenario)
        with self.assertRaises(TypeError):
            self.evaluator.evaluate(manifest)  # pyright: ignore[reportCallIssue]
        with self.assertRaisesRegex(ValueError, "timezone-aware UTC"):
            self.evaluator.evaluate(
                manifest,
                evaluated_at_utc=datetime(2030, 1, 15),
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware UTC"):
            self.evaluator.evaluate(
                manifest,
                evaluated_at_utc=datetime(
                    2030,
                    1,
                    15,
                    tzinfo=timezone(timedelta(hours=8)),
                ),
            )

    def test_summary_is_deterministic_opaque_and_binds_expectation(
        self,
    ) -> None:
        scenario = cast(
            dict[str, Any],
            cast(list[object], self.vectors["state_matrix"])[2],
        )
        manifest = _scenario_manifest(self.vectors, scenario)
        first = self.evaluator.evaluate(
            manifest,
            evaluated_at_utc=_parse_utc("2030-01-01T00:00:00Z"),
        )
        second = self.evaluator.evaluate(
            manifest,
            evaluated_at_utc=_parse_utc("2030-01-31T23:59:59Z"),
        )
        self.assertEqual(first, second)
        summary = first.validation_summary
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(
            summary.summary_version,
            MATCHER_VALIDATION_SUMMARY_VERSION,
        )
        self.assertRegex(summary.evidence_digest, _DIGEST_RE)
        fixture_values = cast(
            dict[str, object],
            self.vectors["expectation"],
        )
        self.assertNotIn(
            summary.evidence_digest,
            json.dumps(fixture_values, sort_keys=True),
        )

        altered_expectation = MatcherValidationExpectation(
            evidence_schema_version=(
                self.expectation.evidence_schema_version
            ),
            matcher_artifact_digest="8" * 64,
            matcher_build_digest=self.expectation.matcher_build_digest,
            semantics_version=self.expectation.semantics_version,
            basic_cohorts=self.expectation.basic_cohorts,
            full_cohorts=self.expectation.full_cohorts,
            fixture_digest=self.expectation.fixture_digest,
            evaluator_digest=self.expectation.evaluator_digest,
        )
        altered_manifest = cast(
            MatcherValidationManifest,
            _scenario_manifest(self.vectors, scenario),
        )
        object.__setattr__(
            altered_manifest,
            "matcher_artifact_digest",
            altered_expectation.matcher_artifact_digest,
        )
        altered = MatcherCapabilityEvaluator(altered_expectation).evaluate(
            altered_manifest,
            evaluated_at_utc=_parse_utc("2030-01-15T00:00:00Z"),
        )
        self.assertEqual(
            altered.state,
            TextMatcherState.TEXT_V1_VALIDATED,
        )
        self.assertNotEqual(
            altered.validation_summary,
            first.validation_summary,
        )

    def test_expectation_is_frozen_closed_and_versioned(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.expectation.semantics_version = "consumer-v2"  # pyright: ignore[reportAttributeAccessIssue]
        self.assertEqual(
            self.expectation.evidence_schema_version,
            MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION,
        )
        with self.assertRaisesRegex(ValueError, "evidence schema"):
            MatcherValidationExpectation(
                evidence_schema_version="consumer-v1",
                matcher_artifact_digest="a" * 64,
                matcher_build_digest="b" * 64,
                semantics_version="text-v1",
                basic_cohorts=(
                    MatcherValidationCohortExpectation(
                        cohort_id="basic",
                        cohort_digest="c" * 64,
                    ),
                ),
                full_cohorts=(
                    MatcherValidationCohortExpectation(
                        cohort_id="full",
                        cohort_digest="d" * 64,
                    ),
                ),
                fixture_digest="e" * 64,
                evaluator_digest="f" * 64,
            )
        with self.assertRaisesRegex(ValueError, "disjoint"):
            MatcherValidationExpectation(
                evidence_schema_version=(
                    MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION
                ),
                matcher_artifact_digest="a" * 64,
                matcher_build_digest="b" * 64,
                semantics_version="text-v1",
                basic_cohorts=(
                    MatcherValidationCohortExpectation(
                        cohort_id="shared",
                        cohort_digest="c" * 64,
                    ),
                ),
                full_cohorts=(
                    MatcherValidationCohortExpectation(
                        cohort_id="shared",
                        cohort_digest="d" * 64,
                    ),
                ),
                fixture_digest="e" * 64,
                evaluator_digest="f" * 64,
            )

    def test_publisher_only_refreshes_manifests_through_evaluator(
        self,
    ) -> None:
        instant = _parse_utc("2030-01-15T00:00:00Z")
        publisher = MatcherCapabilityPublisher(
            self.evaluator,
            initial_manifest=None,
            evaluated_at_utc=instant,
        )
        unavailable = publisher.snapshot()
        self.assertEqual(
            unavailable.state,
            TextMatcherState.UNAVAILABLE,
        )
        scenarios = {
            cast(str, scenario["id"]): cast(dict[str, Any], scenario)
            for scenario in cast(
                list[dict[str, Any]],
                self.vectors["state_matrix"],
            )
        }
        basic = publisher.refresh(
            _scenario_manifest(
                self.vectors,
                scenarios["basic_only_valid"],
            ),
            evaluated_at_utc=instant,
        )
        self.assertIs(publisher.snapshot(), basic)
        full = publisher.refresh(
            _scenario_manifest(
                self.vectors,
                scenarios["basic_and_full_valid"],
            ),
            evaluated_at_utc=instant,
        )
        self.assertIs(publisher.snapshot(), full)
        downgraded = publisher.refresh(
            _scenario_manifest(
                self.vectors,
                scenarios["full_failed"],
            ),
            evaluated_at_utc=instant,
        )
        self.assertEqual(
            downgraded.state,
            TextMatcherState.BASIC_VALIDATED,
        )
        self.assertIs(publisher.snapshot(), downgraded)
        with self.assertRaises(FrozenInstanceError):
            downgraded.state = TextMatcherState.TEXT_V1_VALIDATED  # pyright: ignore[reportAttributeAccessIssue]
        with self.assertRaises(TypeError):
            publisher.refresh(
                cast(MatcherValidationManifest, cast(object, full)),
                evaluated_at_utc=instant,
            )
        self.assertFalse(hasattr(publisher, "publish"))
        self.assertFalse(hasattr(publisher, "publish_snapshot"))
        self.assertFalse(hasattr(publisher, "set_state"))
        self.assertFalse(hasattr(publisher, "validated"))
        self.assertFalse(hasattr(publisher, "match"))

    def test_module_stays_core_internal_and_does_not_execute_match(
        self,
    ) -> None:
        forbidden_public = {
            "MatcherCapabilityEvaluator",
            "MatcherCapabilityPublisher",
            "MatcherValidationCohortEvidence",
            "MatcherValidationCohortExpectation",
            "MatcherValidationExpectation",
            "MatcherValidationManifest",
        }
        self.assertTrue(forbidden_public.isdisjoint(tm_contracts.__all__))
        source = (
            Path(__file__).parents[1] / "matcher_capability.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertNotIn("match", methods)
        self.assertEqual(
            {field.name for field in fields(TextMatcherCapability)},
            {
                "state",
                "semantics_version",
                "supported_profiles",
                "validation_summary",
                "unavailable_reason",
            },
        )


if __name__ == "__main__":
    unittest.main()
