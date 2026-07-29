from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Thread
from typing import cast
from unittest.mock import patch
import unittest

from capability_gated_text_matcher import CapabilityGatedTextMatcherV1
from matcher_capability import (
    MatcherCapabilityEvaluator,
    MatcherCapabilityPublisher,
    MatcherValidationCohortExpectation,
    MatcherValidationExpectation,
)
from text_matcher import TEXT_MATCHER_SEMANTICS_VERSION, TextMatcherV1
from tm_contracts import (
    MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION,
    CapabilityGatedTextMatcher,
    MatcherValidationCohortEvidence,
    MatcherValidationManifest,
    SearchOptions,
    TextMatchProfile,
    TextMatchRejectCode,
    TextMatchRejected,
    TextMatchRequest,
    TextMatchSuccess,
    TextMatcherState,
)


_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_DIGEST_D = "d" * 64
_DIGEST_E = "e" * 64
_DIGEST_F = "f" * 64
_GENERATED_AT = "2030-01-01T00:00:00Z"
_VALID_UNTIL = "2030-02-01T00:00:00Z"
_NOW = datetime(2030, 1, 15, tzinfo=timezone.utc)


def _expectation(
    *,
    semantics_version: str = TEXT_MATCHER_SEMANTICS_VERSION,
) -> MatcherValidationExpectation:
    return MatcherValidationExpectation(
        evidence_schema_version=(
            MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION
        ),
        matcher_artifact_digest=_DIGEST_A,
        matcher_build_digest=_DIGEST_B,
        semantics_version=semantics_version,
        basic_cohorts=(
            MatcherValidationCohortExpectation(
                cohort_id="matcher-basic-v1",
                cohort_digest=_DIGEST_C,
            ),
        ),
        full_cohorts=(
            MatcherValidationCohortExpectation(
                cohort_id="matcher-text-v1",
                cohort_digest=_DIGEST_D,
            ),
        ),
        fixture_digest=_DIGEST_E,
        evaluator_digest=_DIGEST_F,
    )


def _manifest(
    expectation: MatcherValidationExpectation,
    *,
    include_full: bool,
) -> MatcherValidationManifest:
    cohorts = expectation.basic_cohorts + (
        expectation.full_cohorts if include_full else ()
    )
    return MatcherValidationManifest(
        evidence_schema_version=expectation.evidence_schema_version,
        matcher_artifact_digest=expectation.matcher_artifact_digest,
        matcher_build_digest=expectation.matcher_build_digest,
        semantics_version=expectation.semantics_version,
        required_cohort_ids=tuple(
            sorted(cohort.cohort_id for cohort in cohorts)
        ),
        cohort_evidence=tuple(
            MatcherValidationCohortEvidence(
                cohort_id=cohort.cohort_id,
                cohort_digest=cohort.cohort_digest,
                passed=True,
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
            )
            for cohort in sorted(cohorts, key=lambda item: item.cohort_id)
        ),
        fixture_digest=expectation.fixture_digest,
        evaluator_digest=expectation.evaluator_digest,
        generated_at_utc=_GENERATED_AT,
        valid_until_utc=_VALID_UNTIL,
    )


def _publisher() -> tuple[
    MatcherCapabilityPublisher,
    MatcherValidationExpectation,
]:
    expectation = _expectation()
    return (
        MatcherCapabilityPublisher(
            MatcherCapabilityEvaluator(expectation),
            initial_manifest=None,
            evaluated_at_utc=_NOW,
        ),
        expectation,
    )


def _request(
    profile: TextMatchProfile,
    options: SearchOptions,
    *,
    text: str = "alpha ALPHA",
    query: str = "alpha",
) -> TextMatchRequest:
    return TextMatchRequest(
        text=text,
        query=query,
        profile=profile,
        options=options,
    )


class CapabilityGatedTextMatcherV1Tests(unittest.TestCase):
    def test_state_profile_options_matrix_is_fail_closed(self) -> None:
        publisher, expectation = _publisher()
        runtime = CapabilityGatedTextMatcherV1(publisher)
        options = tuple(
            SearchOptions(match_case=match_case, whole_word=whole_word)
            for match_case in (False, True)
            for whole_word in (False, True)
        )
        states = (
            (
                TextMatcherState.UNAVAILABLE,
                None,
            ),
            (
                TextMatcherState.BASIC_VALIDATED,
                _manifest(expectation, include_full=False),
            ),
            (
                TextMatcherState.TEXT_V1_VALIDATED,
                _manifest(expectation, include_full=True),
            ),
        )

        for state, manifest in states:
            publisher.refresh(manifest, evaluated_at_utc=_NOW)
            for profile in TextMatchProfile:
                for search_options in options:
                    with self.subTest(
                        state=state,
                        profile=profile,
                        options=search_options,
                    ):
                        outcome = runtime.match(
                            _request(profile, search_options)
                        )
                        if state is TextMatcherState.UNAVAILABLE:
                            self.assertIsInstance(
                                outcome,
                                TextMatchRejected,
                            )
                            assert isinstance(outcome, TextMatchRejected)
                            self.assertIs(
                                outcome.code,
                                TextMatchRejectCode.CAPABILITY_UNAVAILABLE,
                            )
                            continue
                        if (
                            profile is TextMatchProfile.CONFIGURABLE_TEXT_V1
                            and state
                            is TextMatcherState.BASIC_VALIDATED
                        ):
                            self.assertIsInstance(
                                outcome,
                                TextMatchRejected,
                            )
                            assert isinstance(outcome, TextMatchRejected)
                            self.assertIs(
                                outcome.code,
                                TextMatchRejectCode.PROFILE_NOT_VALIDATED,
                            )
                            continue
                        fixed_options = {
                            TextMatchProfile.LEGACY_COMPAT: SearchOptions(
                                match_case=True,
                                whole_word=False,
                            ),
                            TextMatchProfile.BASIC_CONTIGUOUS: SearchOptions(
                                match_case=False,
                                whole_word=False,
                            ),
                        }
                        expected = fixed_options.get(profile)
                        if (
                            expected is not None
                            and search_options != expected
                        ):
                            self.assertIsInstance(
                                outcome,
                                TextMatchRejected,
                            )
                            assert isinstance(outcome, TextMatchRejected)
                            self.assertIs(
                                outcome.code,
                                TextMatchRejectCode.OPTIONS_NOT_ALLOWED,
                            )
                        else:
                            self.assertIsInstance(
                                outcome,
                                TextMatchSuccess,
                            )
                            assert isinstance(outcome, TextMatchSuccess)
                            self.assertIs(
                                outcome.capability,
                                runtime.capability(),
                            )

    def test_success_binds_request_snapshot_and_unicode_hits(self) -> None:
        publisher, expectation = _publisher()
        publisher.refresh(
            _manifest(expectation, include_full=True),
            evaluated_at_utc=_NOW,
        )
        runtime = CapabilityGatedTextMatcherV1(publisher)

        expansion_request = _request(
            TextMatchProfile.CONFIGURABLE_TEXT_V1,
            SearchOptions(match_case=False, whole_word=False),
            text="Straße STRASSE",
            query="strasse",
        )
        expansion = runtime.match(expansion_request)
        self.assertIsInstance(expansion, TextMatchSuccess)
        assert isinstance(expansion, TextMatchSuccess)
        self.assertEqual(
            tuple(
                (hit.start_index, hit.end_index)
                for hit in expansion.hits
            ),
            ((0, 6), (7, 14)),
        )
        self.assertEqual(
            expansion.request_digest,
            expansion_request.request_digest,
        )
        self.assertIs(
            expansion.request_profile,
            expansion_request.profile,
        )
        self.assertIs(
            expansion.request_options,
            expansion_request.options,
        )

        cjk = runtime.match(
            _request(
                TextMatchProfile.CONFIGURABLE_TEXT_V1,
                SearchOptions(match_case=False, whole_word=True),
                text="办公室里办公室",
                query="办公室",
            )
        )
        self.assertIsInstance(cjk, TextMatchSuccess)
        assert isinstance(cjk, TextMatchSuccess)
        self.assertEqual(
            tuple((hit.start_index, hit.end_index) for hit in cjk.hits),
            ((0, 3), (4, 7)),
        )

        empty_request = _request(
            TextMatchProfile.BASIC_CONTIGUOUS,
            SearchOptions(match_case=False, whole_word=False),
            query="",
        )
        empty = runtime.match(empty_request)
        self.assertIsInstance(empty, TextMatchSuccess)
        assert isinstance(empty, TextMatchSuccess)
        self.assertEqual(empty.hits, ())

    def test_rejection_never_executes_algorithm_or_leaks_content(
        self,
    ) -> None:
        publisher, _ = _publisher()
        runtime = CapabilityGatedTextMatcherV1(publisher)
        request = _request(
            TextMatchProfile.CONFIGURABLE_TEXT_V1,
            SearchOptions(match_case=True, whole_word=True),
            text="TOP SECRET SOURCE",
            query="SECRET QUERY",
        )

        with patch.object(
            TextMatcherV1,
            "match",
            side_effect=AssertionError("algorithm must not execute"),
        ):
            outcome = runtime.match(request)

        self.assertIsInstance(outcome, TextMatchRejected)
        assert isinstance(outcome, TextMatchRejected)
        self.assertEqual(
            outcome.safe_reason,
            "MATCHER.CAPABILITY_UNAVAILABLE",
        )
        self.assertEqual(outcome.request_digest, request.request_digest)
        rendered = repr(outcome)
        self.assertNotIn(request.text, rendered)
        self.assertNotIn(request.query, rendered)
        self.assertIs(outcome.capability, runtime.capability())

    def test_inflight_call_uses_exactly_one_snapshot(self) -> None:
        publisher, expectation = _publisher()
        publisher.refresh(
            _manifest(expectation, include_full=False),
            evaluated_at_utc=_NOW,
        )
        runtime = CapabilityGatedTextMatcherV1(publisher)
        request = _request(
            TextMatchProfile.BASIC_CONTIGUOUS,
            SearchOptions(match_case=False, whole_word=False),
        )
        entered = Event()
        release = Event()
        original_match = TextMatcherV1.match
        original_snapshot = MatcherCapabilityPublisher.snapshot
        snapshot_reads = 0
        result: list[TextMatchSuccess | TextMatchRejected] = []

        def counting_snapshot(
            source: MatcherCapabilityPublisher,
        ):
            nonlocal snapshot_reads
            snapshot_reads += 1
            return original_snapshot(source)

        def blocking_match(
            matcher: TextMatcherV1,
            **kwargs: object,
        ):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release matcher")
            return original_match(
                matcher,
                text=cast(str, kwargs["text"]),
                query=cast(str, kwargs["query"]),
                profile=cast(TextMatchProfile, kwargs["profile"]),
                options=cast(SearchOptions, kwargs["options"]),
            )

        with (
            patch.object(
                MatcherCapabilityPublisher,
                "snapshot",
                new=counting_snapshot,
            ),
            patch.object(
                TextMatcherV1,
                "match",
                new=blocking_match,
            ),
        ):
            worker = Thread(
                target=lambda: result.append(runtime.match(request)),
            )
            worker.start()
            self.assertTrue(entered.wait(timeout=5))
            publisher.refresh(
                _manifest(expectation, include_full=True),
                evaluated_at_utc=_NOW,
            )
            release.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

        self.assertEqual(snapshot_reads, 1)
        self.assertEqual(len(result), 1)
        outcome = result[0]
        self.assertIsInstance(outcome, TextMatchSuccess)
        assert isinstance(outcome, TextMatchSuccess)
        self.assertIs(
            outcome.capability.state,
            TextMatcherState.BASIC_VALIDATED,
        )
        self.assertIs(
            runtime.capability().state,
            TextMatcherState.TEXT_V1_VALIDATED,
        )

    def test_runtime_rejects_wrong_semantics_and_stays_core_only(
        self,
    ) -> None:
        expectation = _expectation(semantics_version="text-v2")
        publisher = MatcherCapabilityPublisher(
            MatcherCapabilityEvaluator(expectation),
            initial_manifest=None,
            evaluated_at_utc=_NOW,
        )
        with self.assertRaisesRegex(ValueError, "semantics"):
            CapabilityGatedTextMatcherV1(publisher)

        valid_publisher, _ = _publisher()
        runtime = CapabilityGatedTextMatcherV1(valid_publisher)
        self.assertTrue(hasattr(runtime, "match"))
        self.assertTrue(hasattr(runtime, "capability"))
        self.assertFalse(hasattr(runtime, "set_state"))
        self.assertFalse(hasattr(runtime, "publish"))
        self.assertFalse(hasattr(runtime, "fallback"))
        self.assertTrue(
            isinstance(
                cast(CapabilityGatedTextMatcher, runtime),
                CapabilityGatedTextMatcherV1,
            )
        )

    def test_publisher_identity_drift_fails_closed_before_execution(
        self,
    ) -> None:
        publisher, _ = _publisher()
        runtime = CapabilityGatedTextMatcherV1(publisher)
        alternate_expectation = _expectation(
            semantics_version="text-v2",
        )
        alternate_evaluator = MatcherCapabilityEvaluator(
            alternate_expectation
        )
        object.__setattr__(
            publisher,
            "_MatcherCapabilityPublisher__evaluator",
            alternate_evaluator,
        )

        with patch.object(
            TextMatcherV1,
            "match",
            side_effect=AssertionError("algorithm must not execute"),
        ) as algorithm:
            refreshed = publisher.refresh(
                _manifest(
                    alternate_expectation,
                    include_full=True,
                ),
                evaluated_at_utc=_NOW,
            )
            outcome = runtime.match(
                _request(
                    TextMatchProfile.CONFIGURABLE_TEXT_V1,
                    SearchOptions(
                        match_case=False,
                        whole_word=False,
                    ),
                )
            )

        self.assertIs(
            refreshed.state,
            TextMatcherState.UNAVAILABLE,
        )
        self.assertEqual(
            publisher.semantics_version,
            TEXT_MATCHER_SEMANTICS_VERSION,
        )
        self.assertIsInstance(outcome, TextMatchRejected)
        assert isinstance(outcome, TextMatchRejected)
        self.assertIs(
            outcome.code,
            TextMatchRejectCode.CAPABILITY_UNAVAILABLE,
        )
        self.assertIs(outcome.capability, refreshed)
        algorithm.assert_not_called()

    def test_expectation_drift_during_refresh_fails_closed(
        self,
    ) -> None:
        publisher, expectation = _publisher()
        runtime = CapabilityGatedTextMatcherV1(publisher)
        alternate_expectation = _expectation(
            semantics_version="text-v2",
        )
        alternate_manifest = _manifest(
            alternate_expectation,
            include_full=True,
        )
        entered_evaluate = Event()
        release_evaluate = Event()
        refresh_results = []
        original_evaluate = MatcherCapabilityEvaluator.evaluate

        def blocking_evaluate(
            evaluator: MatcherCapabilityEvaluator,
            manifest: MatcherValidationManifest | None,
            *,
            evaluated_at_utc: datetime,
        ):
            entered_evaluate.set()
            if not release_evaluate.wait(timeout=5):
                raise AssertionError("test did not release evaluator")
            return original_evaluate(
                evaluator,
                manifest,
                evaluated_at_utc=evaluated_at_utc,
            )

        with patch.object(
            MatcherCapabilityEvaluator,
            "evaluate",
            new=blocking_evaluate,
        ):
            worker = Thread(
                target=lambda: refresh_results.append(
                    publisher.refresh(
                        alternate_manifest,
                        evaluated_at_utc=_NOW,
                    )
                )
            )
            worker.start()
            self.assertTrue(entered_evaluate.wait(timeout=5))
            object.__setattr__(
                expectation,
                "semantics_version",
                "text-v2",
            )
            release_evaluate.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

        self.assertEqual(len(refresh_results), 1)
        refreshed = refresh_results[0]
        self.assertIs(
            refreshed.state,
            TextMatcherState.UNAVAILABLE,
        )
        self.assertEqual(
            publisher.semantics_version,
            TEXT_MATCHER_SEMANTICS_VERSION,
        )
        with patch.object(
            TextMatcherV1,
            "match",
            side_effect=AssertionError("algorithm must not execute"),
        ) as algorithm:
            outcome = runtime.match(
                _request(
                    TextMatchProfile.CONFIGURABLE_TEXT_V1,
                    SearchOptions(
                        match_case=False,
                        whole_word=False,
                    ),
                )
            )
        self.assertIsInstance(outcome, TextMatchRejected)
        assert isinstance(outcome, TextMatchRejected)
        self.assertIs(
            outcome.code,
            TextMatchRejectCode.CAPABILITY_UNAVAILABLE,
        )
        self.assertIs(outcome.capability, refreshed)
        algorithm.assert_not_called()

    def test_caller_expectation_aba_cannot_authorize_other_artifact(
        self,
    ) -> None:
        publisher, expectation = _publisher()
        runtime = CapabilityGatedTextMatcherV1(publisher)
        original_artifact_digest = expectation.matcher_artifact_digest
        alternate_artifact_digest = "9" * 64
        alternate_manifest = _manifest(
            expectation,
            include_full=True,
        )
        object.__setattr__(
            alternate_manifest,
            "matcher_artifact_digest",
            alternate_artifact_digest,
        )
        entered_evaluate = Event()
        permit_evaluate = Event()
        evaluated = Event()
        permit_publish = Event()
        refresh_results = []
        original_evaluate = MatcherCapabilityEvaluator.evaluate

        def blocking_evaluate(
            evaluator: MatcherCapabilityEvaluator,
            manifest: MatcherValidationManifest | None,
            *,
            evaluated_at_utc: datetime,
        ):
            entered_evaluate.set()
            if not permit_evaluate.wait(timeout=5):
                raise AssertionError("test did not permit evaluation")
            result = original_evaluate(
                evaluator,
                manifest,
                evaluated_at_utc=evaluated_at_utc,
            )
            evaluated.set()
            if not permit_publish.wait(timeout=5):
                raise AssertionError("test did not permit publication")
            return result

        with patch.object(
            MatcherCapabilityEvaluator,
            "evaluate",
            new=blocking_evaluate,
        ):
            worker = Thread(
                target=lambda: refresh_results.append(
                    publisher.refresh(
                        alternate_manifest,
                        evaluated_at_utc=_NOW,
                    )
                )
            )
            worker.start()
            self.assertTrue(entered_evaluate.wait(timeout=5))
            object.__setattr__(
                expectation,
                "matcher_artifact_digest",
                alternate_artifact_digest,
            )
            permit_evaluate.set()
            self.assertTrue(evaluated.wait(timeout=5))
            object.__setattr__(
                expectation,
                "matcher_artifact_digest",
                original_artifact_digest,
            )
            permit_publish.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

        self.assertEqual(len(refresh_results), 1)
        refreshed = refresh_results[0]
        self.assertIs(
            refreshed.state,
            TextMatcherState.UNAVAILABLE,
        )
        self.assertEqual(
            expectation.matcher_artifact_digest,
            original_artifact_digest,
        )
        with patch.object(
            TextMatcherV1,
            "match",
            side_effect=AssertionError("algorithm must not execute"),
        ) as algorithm:
            outcome = runtime.match(
                _request(
                    TextMatchProfile.CONFIGURABLE_TEXT_V1,
                    SearchOptions(
                        match_case=False,
                        whole_word=False,
                    ),
                )
            )
        self.assertIsInstance(outcome, TextMatchRejected)
        assert isinstance(outcome, TextMatchRejected)
        self.assertIs(
            outcome.code,
            TextMatchRejectCode.CAPABILITY_UNAVAILABLE,
        )
        self.assertIs(outcome.capability, refreshed)
        algorithm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
