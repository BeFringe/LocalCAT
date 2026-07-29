from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
import json
from typing import cast
import unittest

import tm_contracts
from tm_contracts import (
    MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION,
    MATCHER_VALIDATION_SUMMARY_VERSION,
    CapabilityGatedTextMatcher,
    MatcherValidationCohortEvidence,
    MatcherValidationManifest,
    MatcherValidationSummary,
    SearchHit,
    SearchOptions,
    TextMatchOutcome,
    TextMatchProfile,
    TextMatchRejectCode,
    TextMatchRejected,
    TextMatchRequest,
    TextMatchSuccess,
    TextMatcherCapability,
    TextMatcherState,
    contract_from_json,
    contract_to_json,
    matcher_validation_manifest_from_json,
    matcher_validation_manifest_to_json,
)


_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_DIGEST_D = "d" * 64

_LEGACY = SearchOptions(match_case=True, whole_word=False)
_BASIC = SearchOptions(match_case=False, whole_word=False)
_CASE_WORD = SearchOptions(match_case=True, whole_word=True)
_FOLD_WORD = SearchOptions(match_case=False, whole_word=True)
_ALL_OPTIONS = (_LEGACY, _BASIC, _CASE_WORD, _FOLD_WORD)


def _summary() -> MatcherValidationSummary:
    return MatcherValidationSummary(
        summary_version=MATCHER_VALIDATION_SUMMARY_VERSION,
        evidence_digest=_DIGEST_A,
    )


def _capability(state: TextMatcherState) -> TextMatcherCapability:
    if state is TextMatcherState.UNAVAILABLE:
        return TextMatcherCapability(
            state=state,
            semantics_version=None,
            supported_profiles=(),
            validation_summary=None,
            unavailable_reason="MATCHER.VALIDATION_UNAVAILABLE",
        )
    profiles = (
        TextMatchProfile.LEGACY_COMPAT,
        TextMatchProfile.BASIC_CONTIGUOUS,
    )
    if state is TextMatcherState.TEXT_V1_VALIDATED:
        profiles += (TextMatchProfile.CONFIGURABLE_TEXT_V1,)
    return TextMatcherCapability(
        state=state,
        semantics_version="text-v1",
        supported_profiles=profiles,
        validation_summary=_summary(),
        unavailable_reason=None,
    )


def _request(
    profile: TextMatchProfile = TextMatchProfile.BASIC_CONTIGUOUS,
    options: SearchOptions = _BASIC,
) -> TextMatchRequest:
    return TextMatchRequest(
        text="Straße and STRASSE",
        query="strasse",
        profile=profile,
        options=options,
    )


def _manifest() -> MatcherValidationManifest:
    return MatcherValidationManifest(
        evidence_schema_version=(
            MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION
        ),
        matcher_artifact_digest=_DIGEST_A,
        matcher_build_digest=_DIGEST_B,
        semantics_version="text-v1",
        required_cohort_ids=("matcher-basic-v1", "matcher-text-v1"),
        cohort_evidence=(
            MatcherValidationCohortEvidence(
                cohort_id="matcher-basic-v1",
                cohort_digest=_DIGEST_C,
                passed=True,
            ),
            MatcherValidationCohortEvidence(
                cohort_id="matcher-text-v1",
                cohort_digest=_DIGEST_D,
                passed=False,
            ),
        ),
        fixture_digest=_DIGEST_C,
        evaluator_digest=_DIGEST_D,
        generated_at_utc="2020-01-01T00:00:00Z",
        valid_until_utc="2021-01-01T00:00:00Z",
    )


def _expected_reject_code(
    state: TextMatcherState,
    profile: TextMatchProfile,
    options: SearchOptions,
) -> TextMatchRejectCode | None:
    if state is TextMatcherState.UNAVAILABLE:
        return TextMatchRejectCode.CAPABILITY_UNAVAILABLE
    if (
        state is TextMatcherState.BASIC_VALIDATED
        and profile is TextMatchProfile.CONFIGURABLE_TEXT_V1
    ):
        return TextMatchRejectCode.PROFILE_NOT_VALIDATED
    if (
        profile is TextMatchProfile.LEGACY_COMPAT
        and options != _LEGACY
    ):
        return TextMatchRejectCode.OPTIONS_NOT_ALLOWED
    if (
        profile is TextMatchProfile.BASIC_CONTIGUOUS
        and options != _BASIC
    ):
        return TextMatchRejectCode.OPTIONS_NOT_ALLOWED
    return None


class TextMatcherContractTests(unittest.TestCase):
    def test_search_options_and_hits_are_frozen_and_closed(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            _BASIC.match_case = True  # pyright: ignore[reportAttributeAccessIssue]
        with self.assertRaisesRegex(TypeError, "boolean"):
            SearchOptions(
                match_case=cast(bool, cast(object, 0)),
                whole_word=False,
            )
        with self.assertRaisesRegex(TypeError, "integer"):
            SearchHit(start_index=cast(int, cast(object, False)), end_index=1)
        with self.assertRaisesRegex(ValueError, "at least 0"):
            SearchHit(start_index=-1, end_index=1)
        with self.assertRaisesRegex(ValueError, "greater"):
            SearchHit(start_index=2, end_index=2)

    def test_validation_summary_is_an_opaque_versioned_digest(self) -> None:
        summary_fields = {field.name for field in fields(_summary())}
        self.assertEqual(
            summary_fields,
            {"summary_version", "evidence_digest"},
        )
        with self.assertRaisesRegex(ValueError, "summary version"):
            replace(_summary(), summary_version="consumer-ready")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            replace(_summary(), evidence_digest="validated")

    def test_capability_state_closes_profiles_and_public_evidence(self) -> None:
        unavailable = _capability(TextMatcherState.UNAVAILABLE)
        basic = _capability(TextMatcherState.BASIC_VALIDATED)
        text_v1 = _capability(TextMatcherState.TEXT_V1_VALIDATED)
        self.assertEqual(unavailable.supported_profiles, ())
        self.assertEqual(
            basic.supported_profiles,
            (
                TextMatchProfile.LEGACY_COMPAT,
                TextMatchProfile.BASIC_CONTIGUOUS,
            ),
        )
        self.assertEqual(
            text_v1.supported_profiles,
            (
                TextMatchProfile.LEGACY_COMPAT,
                TextMatchProfile.BASIC_CONTIGUOUS,
                TextMatchProfile.CONFIGURABLE_TEXT_V1,
            ),
        )
        with self.assertRaisesRegex(ValueError, "profiles"):
            replace(
                basic,
                supported_profiles=(
                    TextMatchProfile.BASIC_CONTIGUOUS,
                    TextMatchProfile.LEGACY_COMPAT,
                ),
            )
        with self.assertRaisesRegex(ValueError, "profiles"):
            replace(
                unavailable,
                supported_profiles=(TextMatchProfile.LEGACY_COMPAT,),
            )
        with self.assertRaisesRegex(ValueError, "semantics"):
            replace(basic, semantics_version=None)
        with self.assertRaisesRegex(ValueError, "validation summary"):
            replace(basic, validation_summary=None)
        with self.assertRaisesRegex(ValueError, "unavailable reason"):
            replace(basic, unavailable_reason="MATCHER.UNAVAILABLE")
        with self.assertRaisesRegex(ValueError, "unavailable reason"):
            replace(unavailable, unavailable_reason=None)
        with self.assertRaisesRegex(ValueError, "must omit"):
            replace(unavailable, semantics_version="text-v1")

    def test_capability_exposes_no_consumer_readiness_inputs(self) -> None:
        public_fields = {
            field.name for field in fields(TextMatcherCapability)
        }
        forbidden = {
            "basic_passed",
            "text_passed",
            "validated",
            "sqlite_available",
            "fts5_available",
            "benchmark_passed",
        }
        self.assertTrue(public_fields.isdisjoint(forbidden))
        self.assertFalse(hasattr(TextMatcherCapability, "set_state"))

    def test_request_accepts_raw_inputs_but_produces_an_opaque_digest(
        self,
    ) -> None:
        request = _request()
        self.assertEqual(request.request_digest, request.request_digest)
        self.assertEqual(len(request.request_digest), 64)
        self.assertNotIn(request.text, request.request_digest)
        self.assertNotIn(request.query, request.request_digest)
        self.assertNotEqual(
            request.request_digest,
            replace(request, query="straße").request_digest,
        )
        self.assertEqual(replace(request, query="").query, "")
        with self.assertRaisesRegex(TypeError, "text must be a string"):
            replace(request, text=cast(str, cast(object, None)))
        with self.assertRaisesRegex(TypeError, "query must be a string"):
            replace(request, query=cast(str, cast(object, 1)))

    def test_state_profile_options_matrix_is_mechanically_closed(self) -> None:
        for state in TextMatcherState:
            capability = _capability(state)
            for profile in TextMatchProfile:
                for options in _ALL_OPTIONS:
                    with self.subTest(
                        state=state,
                        profile=profile,
                        options=options,
                    ):
                        request = _request(profile, options)
                        expected = _expected_reject_code(
                            state,
                            profile,
                            options,
                        )
                        if expected is None:
                            success = TextMatchSuccess(
                                hits=(),
                                request_profile=profile,
                                request_options=options,
                                request_digest=request.request_digest,
                                capability=capability,
                            )
                            self.assertIs(success.capability, capability)
                            with self.assertRaisesRegex(
                                ValueError,
                                "authorized",
                            ):
                                TextMatchRejected(
                                    code=TextMatchRejectCode.OPTIONS_NOT_ALLOWED,
                                    safe_reason="MATCHER.OPTIONS_NOT_ALLOWED",
                                    request_profile=profile,
                                    request_options=options,
                                    request_digest=request.request_digest,
                                    capability=capability,
                                )
                        else:
                            rejected = TextMatchRejected(
                                code=expected,
                                safe_reason=f"MATCHER.{expected.value}",
                                request_profile=profile,
                                request_options=options,
                                request_digest=request.request_digest,
                                capability=capability,
                            )
                            self.assertIs(rejected.capability, capability)
                            with self.assertRaisesRegex(
                                ValueError,
                                "not authorized",
                            ):
                                TextMatchSuccess(
                                    hits=(),
                                    request_profile=profile,
                                    request_options=options,
                                    request_digest=request.request_digest,
                                    capability=capability,
                                )

    def test_success_hits_must_be_unique_stable_and_nonzero(self) -> None:
        request = _request()
        capability = _capability(TextMatcherState.BASIC_VALIDATED)
        first = SearchHit(start_index=0, end_index=1)
        second = SearchHit(start_index=3, end_index=5)
        success = TextMatchSuccess(
            hits=(first, second),
            request_profile=request.profile,
            request_options=request.options,
            request_digest=request.request_digest,
            capability=capability,
        )
        self.assertEqual(success.hits, (first, second))
        with self.assertRaisesRegex(ValueError, "stable order"):
            replace(success, hits=(second, first))
        with self.assertRaisesRegex(ValueError, "unique"):
            replace(success, hits=(first, first))
        overlapping = replace(
            success,
            hits=(
                SearchHit(start_index=0, end_index=5),
                SearchHit(start_index=3, end_index=6),
            ),
        )
        self.assertEqual(len(overlapping.hits), 2)

    def test_rejection_is_safe_and_code_is_closed_to_the_matrix(self) -> None:
        request = _request()
        unavailable = _capability(TextMatcherState.UNAVAILABLE)
        rejected = TextMatchRejected(
            code=TextMatchRejectCode.CAPABILITY_UNAVAILABLE,
            safe_reason="MATCHER.CAPABILITY_UNAVAILABLE",
            request_profile=request.profile,
            request_options=request.options,
            request_digest=request.request_digest,
            capability=unavailable,
        )
        forbidden = {
            "hits",
            "query",
            "request",
            "source",
            "target",
            "text",
        }
        self.assertTrue(
            {field.name for field in fields(rejected)}.isdisjoint(forbidden)
        )
        encoded = contract_to_json(rejected)
        self.assertNotIn(request.text, encoded)
        self.assertNotIn(request.query, encoded)
        self.assertNotIn('"hits"', encoded)
        with self.assertRaisesRegex(ValueError, "safe diagnostic identifier"):
            replace(rejected, safe_reason="query=Straße and STRASSE")
        with self.assertRaisesRegex(ValueError, "does not match"):
            replace(rejected, code=TextMatchRejectCode.OPTIONS_NOT_ALLOWED)
        with self.assertRaisesRegex(ValueError, "derive from reject code"):
            replace(rejected, safe_reason="MATCHER.CAPABILITY_DISABLED")
        self.assertNotIn(
            "SEMANTICS_UNAVAILABLE",
            {code.value for code in TextMatchRejectCode},
        )

    def test_public_matcher_values_round_trip_through_strict_tm_codec(
        self,
    ) -> None:
        request = _request()
        capability = _capability(TextMatcherState.BASIC_VALIDATED)
        unavailable = _capability(TextMatcherState.UNAVAILABLE)
        contracts = (
            _BASIC,
            SearchHit(start_index=0, end_index=7),
            _summary(),
            capability,
            request,
            TextMatchSuccess(
                hits=(SearchHit(start_index=0, end_index=7),),
                request_profile=request.profile,
                request_options=request.options,
                request_digest=request.request_digest,
                capability=capability,
            ),
            TextMatchRejected(
                code=TextMatchRejectCode.CAPABILITY_UNAVAILABLE,
                safe_reason="MATCHER.CAPABILITY_UNAVAILABLE",
                request_profile=request.profile,
                request_options=request.options,
                request_digest=request.request_digest,
                capability=unavailable,
            ),
        )
        for contract in contracts:
            with self.subTest(contract=type(contract).__name__):
                encoded = contract_to_json(contract)
                decoded = contract_from_json(encoded)
                self.assertEqual(decoded, contract)
                self.assertEqual(contract_to_json(decoded), encoded)
                envelope = json.loads(encoded)
                envelope["payload"]["consumer_ready"] = True
                with self.assertRaisesRegex(ValueError, "unexpected fields"):
                    contract_from_json(
                        json.dumps(
                            envelope,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    )

    def test_codec_revalidates_mutated_nested_matcher_values(self) -> None:
        request = _request(
            options=SearchOptions(match_case=False, whole_word=False)
        )
        capability = _capability(TextMatcherState.BASIC_VALIDATED)
        success = TextMatchSuccess(
            hits=(),
            request_profile=request.profile,
            request_options=request.options,
            request_digest=request.request_digest,
            capability=capability,
        )
        object.__setattr__(
            success.request_options,
            "whole_word",
            cast(bool, cast(object, 1)),
        )
        with self.assertRaisesRegex(TypeError, "boolean"):
            contract_to_json(success)

    def test_manifest_is_portable_strict_and_not_a_public_tm_contract(
        self,
    ) -> None:
        manifest = _manifest()
        encoded = matcher_validation_manifest_to_json(manifest)
        decoded = matcher_validation_manifest_from_json(encoded)
        self.assertEqual(decoded, manifest)
        self.assertEqual(
            matcher_validation_manifest_to_json(decoded),
            encoded,
        )
        self.assertNotIn("MatcherValidationManifest", tm_contracts.__all__)
        self.assertNotIn(
            "MatcherValidationCohortEvidence",
            tm_contracts.__all__,
        )
        with self.assertRaisesRegex(TypeError, "unsupported TM contract"):
            contract_to_json(
                cast(tm_contracts.TMContract, cast(object, manifest))
            )

        envelope = json.loads(encoded)
        envelope["manifest"]["sqlite_ready"] = True
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            matcher_validation_manifest_from_json(
                json.dumps(
                    envelope,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )

    def test_manifest_allows_failed_and_expired_evidence(self) -> None:
        manifest = _manifest()
        self.assertFalse(manifest.cohort_evidence[1].passed)
        basic_only = replace(
            manifest,
            required_cohort_ids=("matcher-basic-v1",),
            cohort_evidence=(manifest.cohort_evidence[0],),
        )
        self.assertEqual(len(basic_only.cohort_evidence), 1)
        self.assertEqual(
            manifest.valid_until_utc,
            "2021-01-01T00:00:00Z",
        )

    def test_manifest_closes_order_digests_and_utc_interval(self) -> None:
        manifest = _manifest()
        with self.assertRaisesRegex(ValueError, "exactly match"):
            replace(
                manifest,
                cohort_evidence=(manifest.cohort_evidence[0],),
            )
        with self.assertRaisesRegex(ValueError, "stable order"):
            replace(
                manifest,
                cohort_evidence=tuple(reversed(manifest.cohort_evidence)),
            )
        with self.assertRaisesRegex(ValueError, "unique"):
            replace(
                manifest,
                cohort_evidence=(
                    manifest.cohort_evidence[0],
                    manifest.cohort_evidence[0],
                ),
            )
        with self.assertRaisesRegex(ValueError, "required cohort"):
            replace(
                manifest,
                cohort_evidence=(
                    replace(
                        manifest.cohort_evidence[0],
                        cohort_id="matcher-unknown-v1",
                    ),
                ),
            )
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            replace(manifest, matcher_build_digest="build-ready")
        with self.assertRaisesRegex(ValueError, "evidence schema version"):
            replace(manifest, evidence_schema_version="consumer-schema-v1")
        with self.assertRaisesRegex(ValueError, "strict UTC"):
            replace(
                manifest,
                generated_at_utc="2020-01-01T00:00:00+00:00",
            )
        with self.assertRaisesRegex(ValueError, "later"):
            replace(
                manifest,
                valid_until_utc=manifest.generated_at_utc,
            )
        envelope = json.loads(
            matcher_validation_manifest_to_json(manifest)
        )
        envelope["manifest_codec_version"] = 2
        with self.assertRaisesRegex(ValueError, "codec version"):
            matcher_validation_manifest_from_json(
                json.dumps(
                    envelope,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )

    def test_only_the_runtime_protocol_exposes_match_execution(self) -> None:
        public_matcher_names = {
            "CapabilityGatedTextMatcher",
            "MatcherValidationSummary",
            "SearchHit",
            "SearchOptions",
            "TextMatchOutcome",
            "TextMatchProfile",
            "TextMatchRejectCode",
            "TextMatchRejected",
            "TextMatchRequest",
            "TextMatchSuccess",
            "TextMatcherCapability",
            "TextMatcherState",
        }
        self.assertTrue(public_matcher_names.issubset(tm_contracts.__all__))
        internal_matcher_names = {
            "MATCHER_VALIDATION_EVIDENCE_SCHEMA_VERSION",
            "MATCHER_VALIDATION_MANIFEST_CODEC_VERSION",
            "MatcherValidationCohortEvidence",
            "MatcherValidationManifest",
            "matcher_validation_manifest_from_json",
            "matcher_validation_manifest_to_json",
        }
        self.assertTrue(internal_matcher_names.isdisjoint(tm_contracts.__all__))
        self.assertTrue(hasattr(CapabilityGatedTextMatcher, "capability"))
        self.assertTrue(hasattr(CapabilityGatedTextMatcher, "match"))
        with self.assertRaisesRegex(TypeError, "unsupported TM contract"):
            contract_to_json(
                cast(
                    tm_contracts.TMContract,
                    cast(object, CapabilityGatedTextMatcher),
                )
            )

    def test_outcome_union_is_explicit(self) -> None:
        request = _request()
        success: TextMatchOutcome = TextMatchSuccess(
            hits=(),
            request_profile=request.profile,
            request_options=request.options,
            request_digest=request.request_digest,
            capability=_capability(TextMatcherState.BASIC_VALIDATED),
        )
        rejected: TextMatchOutcome = TextMatchRejected(
            code=TextMatchRejectCode.CAPABILITY_UNAVAILABLE,
            safe_reason="MATCHER.CAPABILITY_UNAVAILABLE",
            request_profile=request.profile,
            request_options=request.options,
            request_digest=request.request_digest,
            capability=_capability(TextMatcherState.UNAVAILABLE),
        )
        self.assertIsInstance(success, TextMatchSuccess)
        self.assertIsInstance(rejected, TextMatchRejected)


if __name__ == "__main__":
    unittest.main()
