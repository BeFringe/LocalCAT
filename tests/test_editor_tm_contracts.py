from __future__ import annotations

import dataclasses
import json
import math
import unittest
from dataclasses import replace
from typing import Any, cast

from editor_contracts import (
    LegacyExactTMSuggestion,
    EditorTMContract,
    RetrievalDisplayState,
    SuggestionQueryIdentity,
    TMResourceDisplayMode,
    TMResourceStatus,
    TMSuggestion,
    TMSuggestionProvenance,
    TextMatcherDisplayState,
    editor_tm_contract_from_json,
    editor_tm_contract_to_json,
)
from tm_contracts import TMMatchType, TextMatcherState, TextMatchProfile


_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64


def _identity(**changes: object) -> SuggestionQueryIdentity:
    values: dict[str, Any] = {
        "project_session_id": "session-1",
        "segment_id": "segment-7",
        "source_digest": _DIGEST,
        "query_epoch": 11,
    }
    values.update(changes)
    return SuggestionQueryIdentity(**values)  # type: ignore[arg-type]


def _provenance(**changes: object) -> TMSuggestionProvenance:
    values: dict[str, Any] = {
        "resource_name": "Canonical TM",
        "resource_mode": TMResourceDisplayMode.CANONICAL_ACTIVE,
    }
    values.update(changes)
    return TMSuggestionProvenance(**values)  # type: ignore[arg-type]


def _exact(**changes: object) -> TMSuggestion:
    values: dict[str, Any] = {
        "resource_id": "tm-main",
        "record_id": "canonical:17",
        "query_source": "The office is ready.",
        "matched_source": "The office is ready.",
        "target": "办公室准备好了。",
        "match_type": TMMatchType.EXACT,
        "final_similarity": 1.0,
        "provenance": _provenance(),
        "query_identity": _identity(),
    }
    values.update(changes)
    return TMSuggestion(**values)  # type: ignore[arg-type]


def _fuzzy(**changes: object) -> TMSuggestion:
    values: dict[str, Any] = {
        "resource_id": "tm-main",
        "record_id": "canonical:29",
        "query_source": "The office is ready.",
        "matched_source": "The office was ready.",
        "target": "办公室已经准备好了。",
        "match_type": TMMatchType.FUZZY,
        "final_similarity": 0.875,
        "provenance": _provenance(),
        "query_identity": _identity(),
    }
    values.update(changes)
    return TMSuggestion(**values)  # type: ignore[arg-type]


class EditorTMContractShapeTest(unittest.TestCase):
    def test_contracts_are_frozen_and_reuse_core_enums(self) -> None:
        identity = _identity()
        suggestion = _exact()
        resource_status = TMResourceStatus(
            resource_id="tm-main",
            resource_name="Canonical TM",
            mode=TMResourceDisplayMode.CANONICAL_ACTIVE,
            exact_available=True,
            context_available=True,
            fuzzy_available=False,
            safe_codes=("RETRIEVAL.FUZZY_UNAVAILABLE",),
            retryable=False,
        )
        retrieval = RetrievalDisplayState(
            context_available=True,
            fuzzy_available=False,
            safe_codes=("RETRIEVAL.FUZZY_UNAVAILABLE",),
        )
        matcher = TextMatcherDisplayState(
            state=TextMatcherState.BASIC_VALIDATED,
            supported_profiles=(
                TextMatchProfile.LEGACY_COMPAT,
                TextMatchProfile.BASIC_CONTIGUOUS,
            ),
            safe_reason=None,
        )

        for value in (identity, suggestion, resource_status, retrieval, matcher):
            with self.subTest(contract=type(value).__name__):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(value, dataclasses.fields(value)[0].name, None)
                self.assertTrue(
                    getattr(type(value), "__dataclass_params__").frozen
                )
                self.assertFalse(hasattr(value, "__dict__"))
        self.assertIs(suggestion.match_type, TMMatchType.EXACT)
        self.assertIs(matcher.state, TextMatcherState.BASIC_VALIDATED)
        self.assertEqual(
            matcher.supported_profiles,
            (
                TextMatchProfile.LEGACY_COMPAT,
                TextMatchProfile.BASIC_CONTIGUOUS,
            ),
        )

    def test_query_identity_is_exact_four_field_schema(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(SuggestionQueryIdentity)),
            (
                "project_session_id",
                "segment_id",
                "source_digest",
                "query_epoch",
            ),
        )
        self.assertEqual(_identity().source_digest, _DIGEST)

    def test_record_identity_namespaces_are_closed(self) -> None:
        self.assertEqual(_exact().record_id, "canonical:17")
        legacy = _exact(
            record_id="legacy:" + _OTHER_DIGEST,
            provenance=_provenance(
                resource_mode=TMResourceDisplayMode.LEGACY_EXACT_ONLY
            ),
        )
        self.assertEqual(legacy.record_id, "legacy:" + _OTHER_DIGEST)

        invalid_ids: tuple[object, ...] = (
            "canonical:0",
            "canonical:-1",
            "canonical:01",
            "canonical:1.0",
            "legacy:" + "A" * 64,
            "legacy:" + "a" * 63,
            "legacy:body text",
            "17",
            "",
            17,
        )
        for record_id in invalid_ids:
            with self.subTest(record_id=record_id), self.assertRaises(
                (TypeError, ValueError)
            ):
                _exact(record_id=record_id)

    def test_match_type_similarity_and_source_relationships_are_closed(self) -> None:
        for match_type in (TMMatchType.EXACT, TMMatchType.CONTEXT):
            with self.subTest(match_type=match_type):
                suggestion = _exact(match_type=match_type)
                self.assertEqual(suggestion.final_similarity, 1.0)
                self.assertEqual(
                    suggestion.query_source,
                    suggestion.matched_source,
                )
        whitespace = _exact(
            query_source=" ",
            matched_source=" ",
            target=" ",
        )
        self.assertEqual(whitespace.query_source, " ")
        self.assertEqual(whitespace.target, " ")

        invalid: tuple[dict[str, object], ...] = (
            {"match_type": "EXACT"},
            {"match_type": TMMatchType.EXACT, "final_similarity": 0.99},
            {"match_type": TMMatchType.CONTEXT, "final_similarity": 0.99},
            {"match_type": TMMatchType.EXACT, "matched_source": "different"},
            {"match_type": TMMatchType.CONTEXT, "matched_source": "different"},
            {"match_type": TMMatchType.FUZZY, "matched_source": "The office is ready."},
            {
                "record_id": "legacy:" + _OTHER_DIGEST,
                "match_type": TMMatchType.CONTEXT,
                "provenance": _provenance(
                    resource_mode=TMResourceDisplayMode.ACTIVATING
                ),
            },
            {
                "record_id": "legacy:" + _OTHER_DIGEST,
                "provenance": _provenance(),
            },
            {
                "provenance": _provenance(
                    resource_mode=TMResourceDisplayMode.UNAVAILABLE
                ),
            },
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(
                (TypeError, ValueError)
            ):
                _exact(**changes)

        for score in (0.0, 0.6, 0.875, 1.0):
            with self.subTest(score=score):
                self.assertEqual(_fuzzy(final_similarity=score).final_similarity, score)

    def test_similarity_rejects_bool_non_numeric_and_non_finite_values(self) -> None:
        invalid: tuple[object, ...] = (
            True,
            False,
            "0.8",
            -0.01,
            1.01,
            math.nan,
            math.inf,
            -math.inf,
        )
        for score in invalid:
            with self.subTest(score=score), self.assertRaises(
                (TypeError, ValueError)
            ):
                _fuzzy(final_similarity=score)

    def test_nested_values_require_exact_frozen_contract_types(self) -> None:
        for field_name, value in (
            ("provenance", {"resource_name": "TM"}),
            ("query_identity", {"segment_id": "segment-7"}),
        ):
            with self.subTest(field_name=field_name), self.assertRaises(TypeError):
                _exact(**{field_name: value})

        with self.assertRaises(TypeError):
            TMSuggestionProvenance(
                resource_name="TM",
                resource_mode=cast(Any, "CANONICAL_ACTIVE"),
            )

    def test_identity_types_ranges_and_digest_are_strict(self) -> None:
        invalid: tuple[dict[str, object], ...] = (
            {"project_session_id": ""},
            {"project_session_id": 1},
            {"segment_id": "  "},
            {"segment_id": 1},
            {"source_digest": "A" * 64},
            {"source_digest": "a" * 63},
            {"source_digest": 1},
            {"query_epoch": -1},
            {"query_epoch": True},
            {"query_epoch": 1.0},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(
                (TypeError, ValueError)
            ):
                _identity(**changes)

    def test_safe_statuses_reject_messages_duplicates_and_mutable_collections(self) -> None:
        status_values: tuple[tuple[type[Any], dict[str, Any]], ...] = (
            (
                TMResourceStatus,
                {
                    "resource_id": "tm-main",
                    "resource_name": "TM",
                    "mode": TMResourceDisplayMode.DEGRADED,
                    "exact_available": True,
                    "context_available": False,
                    "fuzzy_available": False,
                    "safe_codes": ("RETRIEVAL.CLOSED",),
                    "retryable": True,
                },
            ),
            (
                RetrievalDisplayState,
                {
                    "context_available": False,
                    "fuzzy_available": False,
                    "safe_codes": ("RETRIEVAL.CLOSED",),
                },
            ),
        )
        for contract_type, values in status_values:
            valid = contract_type(**values)
            self.assertEqual(valid.safe_codes, ("RETRIEVAL.CLOSED",))
            for safe_codes in (
                ["RETRIEVAL.CLOSED"],
                ("RETRIEVAL.CLOSED", "RETRIEVAL.CLOSED"),
                ("raw exception body",),
                ("/tmp/private.db",),
                (1,),
            ):
                with self.subTest(
                    contract=contract_type.__name__, safe_codes=safe_codes
                ), self.assertRaises((TypeError, ValueError)):
                    cast(Any, contract_type)(
                        **(values | {"safe_codes": safe_codes})
                    )

    def test_resource_status_requires_exact_boolean_and_mode_types(self) -> None:
        values: dict[str, Any] = {
            "resource_id": "tm-main",
            "resource_name": "TM",
            "mode": TMResourceDisplayMode.CANONICAL_ACTIVE,
            "exact_available": True,
            "context_available": False,
            "fuzzy_available": False,
            "safe_codes": (),
            "retryable": False,
        }
        for field_name in (
            "exact_available",
            "context_available",
            "fuzzy_available",
            "retryable",
        ):
            with self.subTest(field_name=field_name), self.assertRaises(TypeError):
                cast(Any, TMResourceStatus)(**(values | {field_name: 1}))
        with self.assertRaises(TypeError):
            cast(Any, TMResourceStatus)(
                **(values | {"mode": "CANONICAL_ACTIVE"})
            )

    def test_matcher_display_is_a_closed_projection_of_core_state(self) -> None:
        unavailable = TextMatcherDisplayState(
            state=TextMatcherState.UNAVAILABLE,
            supported_profiles=(),
            safe_reason="MATCHER.CAPABILITY_UNAVAILABLE",
        )
        basic = TextMatcherDisplayState(
            state=TextMatcherState.BASIC_VALIDATED,
            supported_profiles=(
                TextMatchProfile.LEGACY_COMPAT,
                TextMatchProfile.BASIC_CONTIGUOUS,
            ),
            safe_reason=None,
        )
        text_v1 = TextMatcherDisplayState(
            state=TextMatcherState.TEXT_V1_VALIDATED,
            supported_profiles=(
                TextMatchProfile.LEGACY_COMPAT,
                TextMatchProfile.BASIC_CONTIGUOUS,
                TextMatchProfile.CONFIGURABLE_TEXT_V1,
            ),
            safe_reason=None,
        )
        self.assertIs(unavailable.state, TextMatcherState.UNAVAILABLE)
        self.assertEqual(len(basic.supported_profiles), 2)
        self.assertEqual(len(text_v1.supported_profiles), 3)

        invalid: tuple[dict[str, Any], ...] = (
            {
                "state": "UNAVAILABLE",
                "supported_profiles": (),
                "safe_reason": "MATCHER.CAPABILITY_UNAVAILABLE",
            },
            {
                "state": TextMatcherState.UNAVAILABLE,
                "supported_profiles": (),
                "safe_reason": None,
            },
            {
                "state": TextMatcherState.UNAVAILABLE,
                "supported_profiles": (TextMatchProfile.LEGACY_COMPAT,),
                "safe_reason": "MATCHER.CAPABILITY_UNAVAILABLE",
            },
            {
                "state": TextMatcherState.BASIC_VALIDATED,
                "supported_profiles": [
                    TextMatchProfile.LEGACY_COMPAT,
                    TextMatchProfile.BASIC_CONTIGUOUS,
                ],
                "safe_reason": None,
            },
            {
                "state": TextMatcherState.BASIC_VALIDATED,
                "supported_profiles": (TextMatchProfile.LEGACY_COMPAT,),
                "safe_reason": None,
            },
            {
                "state": TextMatcherState.BASIC_VALIDATED,
                "supported_profiles": (
                    TextMatchProfile.LEGACY_COMPAT,
                    TextMatchProfile.BASIC_CONTIGUOUS,
                ),
                "safe_reason": "MATCHER.CLOSED",
            },
            {
                "state": TextMatcherState.UNAVAILABLE,
                "supported_profiles": (),
                "safe_reason": "arbitrary failure body",
            },
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(
                (TypeError, ValueError)
            ):
                TextMatcherDisplayState(**values)  # type: ignore[arg-type]

    def test_legacy_bridge_is_explicit_and_not_shape_compatible_with_new_contract(self) -> None:
        legacy = LegacyExactTMSuggestion(
            source="Hello",
            target="你好",
            resource_id="legacy-tm",
            resource_name="Legacy TM",
            similarity=1.0,
            match_type="EXACT",
        )
        self.assertNotIsInstance(legacy, TMSuggestion)
        with self.assertRaises(TypeError):
            cast(Any, TMSuggestion)(
                source="Hello",
                target="你好",
                resource_id="legacy-tm",
                resource_name="Legacy TM",
            )


class EditorTMContractCodecTest(unittest.TestCase):
    def _contracts(self) -> tuple[EditorTMContract, ...]:
        return (
            _identity(),
            _provenance(),
            _exact(),
            TMResourceStatus(
                resource_id="tm-main",
                resource_name="Canonical TM",
                mode=TMResourceDisplayMode.DEGRADED,
                exact_available=True,
                context_available=False,
                fuzzy_available=False,
                safe_codes=("RETRIEVAL.CLOSED",),
                retryable=True,
            ),
            RetrievalDisplayState(
                context_available=False,
                fuzzy_available=False,
                safe_codes=("RETRIEVAL.CLOSED",),
            ),
            TextMatcherDisplayState(
                state=TextMatcherState.TEXT_V1_VALIDATED,
                supported_profiles=(
                    TextMatchProfile.LEGACY_COMPAT,
                    TextMatchProfile.BASIC_CONTIGUOUS,
                    TextMatchProfile.CONFIGURABLE_TEXT_V1,
                ),
                safe_reason=None,
            ),
        )

    def test_strict_deterministic_round_trip(self) -> None:
        for contract in self._contracts():
            with self.subTest(contract=type(contract).__name__):
                encoded = editor_tm_contract_to_json(contract)  # type: ignore[arg-type]
                self.assertEqual(editor_tm_contract_from_json(encoded), contract)
                self.assertEqual(
                    editor_tm_contract_to_json(editor_tm_contract_from_json(encoded)),
                    encoded,
                )
                envelope = json.loads(encoded)
                self.assertEqual(envelope["contract_version"], 1)
                self.assertEqual(
                    tuple(envelope),
                    ("contract_type", "contract_version", "payload"),
                )

    def test_every_suggestion_field_tamper_is_detected_or_changes_the_frozen_value(self) -> None:
        original = _exact()
        for field in dataclasses.fields(TMSuggestion):
            replacement: object
            if field.name == "resource_id":
                replacement = "tm-other"
            elif field.name == "record_id":
                replacement = "canonical:18"
            elif field.name == "query_source":
                replacement = "Changed source"
            elif field.name == "matched_source":
                replacement = "Changed source"
            elif field.name == "target":
                replacement = "被替换的译文"
            elif field.name == "match_type":
                replacement = TMMatchType.CONTEXT
            elif field.name == "final_similarity":
                replacement = 0.5
            elif field.name == "provenance":
                replacement = _provenance(resource_name="Other TM")
            elif field.name == "query_identity":
                replacement = _identity(query_epoch=12)
            else:  # pragma: no cover - the field list itself is frozen above
                self.fail(f"uncovered TMSuggestion field: {field.name}")

            with self.subTest(field=field.name):
                try:
                    forged = replace(original, **{field.name: replacement})
                except (TypeError, ValueError):
                    continue
                self.assertNotEqual(forged, original)
                self.assertNotEqual(
                    editor_tm_contract_to_json(forged),
                    editor_tm_contract_to_json(original),
                )

    def test_every_query_identity_field_tamper_changes_the_frozen_value(self) -> None:
        original = _identity()
        changes: dict[str, object] = {
            "project_session_id": "session-2",
            "segment_id": "segment-8",
            "source_digest": _OTHER_DIGEST,
            "query_epoch": 12,
        }
        self.assertEqual(
            set(changes),
            {field.name for field in dataclasses.fields(SuggestionQueryIdentity)},
        )
        for field_name, replacement in changes.items():
            with self.subTest(field=field_name):
                forged = replace(original, **{field_name: replacement})
                self.assertNotEqual(forged, original)
                self.assertNotEqual(
                    editor_tm_contract_to_json(forged),
                    editor_tm_contract_to_json(original),
                )

    def test_decoder_rejects_missing_unknown_and_duplicate_fields(self) -> None:
        encoded = editor_tm_contract_to_json(_exact())
        envelope = json.loads(encoded)

        for level, field_name in (
            ("envelope", "contract_version"),
            ("payload", "record_id"),
            ("nested", "query_epoch"),
        ):
            forged = json.loads(encoded)
            if level == "envelope":
                del forged[field_name]
            elif level == "payload":
                del forged["payload"][field_name]
            else:
                del forged["payload"]["query_identity"][field_name]
            with self.subTest(level=level, kind="missing"), self.assertRaises(
                ValueError
            ):
                editor_tm_contract_from_json(json.dumps(forged))

        for level in ("envelope", "payload", "nested"):
            forged = json.loads(encoded)
            if level == "envelope":
                forged["unexpected"] = 1
            elif level == "payload":
                forged["payload"]["unexpected"] = 1
            else:
                forged["payload"]["query_identity"]["unexpected"] = 1
            with self.subTest(level=level, kind="unknown"), self.assertRaises(
                ValueError
            ):
                editor_tm_contract_from_json(json.dumps(forged))

        payload = json.dumps(envelope["payload"], ensure_ascii=False)
        duplicate_envelope = (
            '{"contract_type":"TMSuggestion",'
            '"contract_type":"TMSuggestion",'
            f'"contract_version":1,"payload":{payload}}}'
        )
        duplicate_payload = encoded.replace(
            '"record_id":"canonical:17"',
            '"record_id":"canonical:17","record_id":"canonical:18"',
        )
        duplicate_nested = encoded.replace(
            '"query_epoch":11',
            '"query_epoch":11,"query_epoch":12',
        )
        for forged in (duplicate_envelope, duplicate_payload, duplicate_nested):
            with self.subTest(kind="duplicate"), self.assertRaises(ValueError):
                editor_tm_contract_from_json(forged)

    def test_decoder_rejects_bool_as_int_non_finite_and_enum_forgery(self) -> None:
        encoded = editor_tm_contract_to_json(_exact())
        for before, after in (
            ('"query_epoch":11', '"query_epoch":true'),
            ('"final_similarity":1.0', '"final_similarity":NaN'),
            ('"final_similarity":1.0', '"final_similarity":Infinity'),
            ('"match_type":"EXACT"', '"match_type":"exact"'),
            (
                '"resource_mode":"CANONICAL_ACTIVE"',
                '"resource_mode":"CANONICAL"',
            ),
        ):
            self.assertIn(before, encoded)
            with self.subTest(after=after), self.assertRaises(ValueError):
                editor_tm_contract_from_json(encoded.replace(before, after))

    def test_codec_public_surface_is_closed_and_never_exports_core_internals(self) -> None:
        legacy = LegacyExactTMSuggestion(
            source="Hello",
            target="你好",
            resource_id="legacy-tm",
            resource_name="Legacy TM",
        )
        with self.assertRaises(TypeError):
            editor_tm_contract_to_json(cast(Any, legacy))

        forbidden = {
            "similarity_evidence",
            "context_evidence",
            "proof",
            "candidate_proof",
            "folded_source",
            "scorer_components",
            "path",
        }
        for contract in self._contracts():
            with self.subTest(contract=type(contract).__name__):
                encoded = editor_tm_contract_to_json(contract)  # type: ignore[arg-type]
                self.assertTrue(forbidden.isdisjoint(_all_json_keys(json.loads(encoded))))

    def test_decoder_rejects_unsupported_envelope_and_non_string_input(self) -> None:
        encoded = editor_tm_contract_to_json(_exact())
        for forged in (
            encoded.replace('"contract_version":1', '"contract_version":true'),
            encoded.replace('"contract_version":1', '"contract_version":2'),
            encoded.replace('"contract_type":"TMSuggestion"', '"contract_type":"TMResult"'),
            "[]",
            "not-json",
        ):
            with self.subTest(forged=forged[:40]), self.assertRaises(ValueError):
                editor_tm_contract_from_json(forged)
        with self.assertRaises(TypeError):
            editor_tm_contract_from_json(cast(Any, b"{}"))


def _all_json_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(key)
            keys.update(_all_json_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            keys.update(_all_json_keys(nested))
    return keys


if __name__ == "__main__":
    unittest.main()
