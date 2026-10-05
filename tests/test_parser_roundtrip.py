from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import parser_composition as composition
import parser_contracts as contracts
from parser_contracts import (
    CodecIdentity, ContractViolation, DocumentHeader, EffectivePurpose,
    FOUNDATION_GUARDED_ISSUE_CODES, IssueSeverity, ParseIssue, ParsedSegment,
    RawSpeaker, ReadRequest, RoundTripTokenEnvelope, SelectionRequest,
    SourceReference, TargetPresence, TranslationState,
)
from parser_source import CancellationToken
from tests.test_parser_composition import _Provider
from tests.test_parser_registry import _descriptor


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


class _RoundTripCodec:
    def __init__(self, owner):
        self.owner = owner
        self.descriptor = owner.descriptor

    def iter_raw(self, source, request):
        self.owner.calls.append("read")
        source.read()
        yield DocumentHeader("fixture", "en", "zh", ())
        yield ParsedSegment(
            "slot", "source", "old", TargetPresence.PRESENT,
            TranslationState.UNCONFIRMED, RawSpeaker(""), (),
        )
        if self.owner.reader_failure:
            raise RuntimeError("secret reader body")

    def prepare(self, request):
        self.owner.calls.append("prepare")
        self.owner.last_request = request
        if self.owner.serializer_failure:
            raise self.owner.serializer_failure
        if self.owner.consume:
            self.owner.assertEqual(request.source.read(), b"original source\n")
        if self.owner.cancel_during:
            self.owner.cancellation.cancel()
        payload = self.owner.output
        return contracts.PreparedFormatBytes(
            codec_identity=self.descriptor.identity,
            format_id=self.descriptor.format_id,
            source_fingerprint=request.terminal.source.content_sha256,
            output_fingerprint=digest(payload),
            payload=payload,
            diagnostics=self.owner.diagnostics,
        ) if self.owner.override is None else self.owner.override(request)


class ParserRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "input.neutral"
        self.source.write_bytes(b"original source\n")
        self.target = self.root / "existing.target"
        self.target.write_bytes(b"leave target untouched")
        self.calls = []
        self.reader_failure = False
        self.serializer_failure = None
        self.consume = True
        self.cancel_during = False
        self.cancellation = CancellationToken()
        self.output = b"exact serialized bytes\r\n"
        self.diagnostics = ()
        self.override = None
        self.last_request = None
        base = _descriptor("round-trip", provider_id="plugin.roundtrip")
        profile = replace(
            base.limit_profile,
            declared_issue_codes=tuple(sorted(set(FOUNDATION_GUARDED_ISSUE_CODES) | {
                "PARSER.ROUNDTRIP.PROTECTION", "PARSER.ROUNDTRIP.WARNING",
            })),
        )
        self.descriptor = replace(
            base,
            capabilities=replace(base.capabilities, source_round_trip_write=True),
            limit_profile=profile,
            reader_factory=lambda: _RoundTripCodec(self),
        )
        self.descriptor = replace(
            self.descriptor, round_trip_serializer_factory=self.writer_factory,
            round_trip_limits=contracts.RoundTripLimits(1024, 1024),
        )
        self.surface = self.make_surface()

    def writer_factory(self):
        self.calls.append("factory")
        return _RoundTripCodec(self)

    def make_surface(self):
        return composition.create_parser_application_surface(providers=(
            composition.ProviderBinding(
                "plugin.roundtrip", _Provider("plugin.roundtrip", "1", (self.descriptor,)),
                True, ("1",),
            ),
        ))

    def opened(self, surface=None):
        surface = surface or self.surface
        opened = surface.open_input(
            SourceReference(str(self.root), str(self.source), "fixture"),
            SelectionRequest(EffectivePurpose.PROJECT_DOCUMENT, self.descriptor.format_id),
            ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, self.descriptor.format_id),
            cancellation=self.cancellation,
        )
        self.addCleanup(opened.close)
        return opened

    def token(self, opened, **changes):
        return replace(RoundTripTokenEnvelope(
            self.descriptor.identity, opened.source_identity.content_sha256,
            digest(b"opaque mapping"), b"opaque mapping",
        ), **changes)

    def edits(self):
        return (contracts.RoundTripSegmentEdit("slot", "new target"),)

    def prepare(self, opened=None, token=None):
        opened = opened or self.opened()
        prepared = self.surface.prepare_round_trip(
            opened, token or self.token(opened), self.edits(),
        )
        self.addCleanup(prepared.close)
        return prepared

    def assertCode(self, code, callback):
        with self.assertRaises(ContractViolation) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b"leave target untouched")
        return caught.exception

    def test_round_trip_prepare_verifies_source_before_factory_without_target_open(self):
        opened = self.opened()
        with mock.patch.object(composition, "_rooted_backend", side_effect=AssertionError("target open")), \
             mock.patch.object(composition, "_atomic_write_bytes", side_effect=AssertionError("publish")):
            prepared = self.surface.prepare_round_trip(opened, self.token(opened), self.edits())
        self.addCleanup(prepared.close)
        self.assertEqual(self.calls, ["read", "factory", "prepare"])
        self.assertEqual(prepared.source_identity, opened.source_identity)
        self.assertEqual(prepared.codec_identity, self.descriptor.identity)
        self.assertEqual(prepared.format_id, self.descriptor.format_id)
        self.assertEqual(prepared.output_fingerprint, digest(self.output))
        self.assertEqual(prepared.byte_count, len(self.output))
        self.assertEqual(self.last_request.edits, self.edits())
        self.assertEqual(self.last_request.terminal.record_count, 1)
        self.assertTrue(self.last_request.source.closed)
        self.assertFalse(hasattr(prepared, "payload"))
        self.assertFalse(hasattr(prepared, "write"))
        with self.assertRaises(AttributeError):
            prepared.byte_count = 1
        self.assertEqual(self.target.read_bytes(), b"leave target untouched")

    def test_foreign_version_stale_missing_and_state_tokens_fail_before_provider(self):
        opened = self.opened()
        changes = (
            {"codec_identity": CodecIdentity("foreign", "codec", "1")},
            {"codec_identity": replace(self.descriptor.identity, codec_version="2")},
            {"source_fingerprint": "0" * 64},
            {"format_state_fingerprint": "0" * 64},
            {"opaque_payload": b"tampered mapping"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertCode("PARSER.CAPABILITY.INVALID_TOKEN", lambda: self.prepare(opened, self.token(opened, **change)))
        self.assertCode("PARSER.CAPABILITY.INVALID_TOKEN", lambda: self.surface.prepare_round_trip(opened, None, self.edits()))
        self.assertEqual(self.calls, [])

    def test_missing_factory_is_explicit_unsupported_without_reading(self):
        self.descriptor = replace(self.descriptor, round_trip_serializer_factory=None)
        self.surface = self.make_surface()
        self.assertCode("PARSER.CAPABILITY.WRITE_UNSUPPORTED", self.prepare)
        self.assertEqual(self.calls, [])

    def test_source_requires_exact_live_input_issued_by_same_surface(self):
        self.assertCode("PARSER.SOURCE.UNVERIFIED", lambda: self.surface.prepare_round_trip(b"source", None, self.edits()))
        other = self.make_surface()
        self.assertCode("PARSER.SOURCE.UNVERIFIED", lambda: self.prepare(self.opened(other)))
        opened = self.opened()
        opened.close()
        self.assertCode("PARSER.SOURCE.SNAPSHOT_RELEASED", lambda: self.prepare(opened))
        forged = object.__new__(composition.OpenedParserInput)
        self.assertCode("PARSER.SOURCE.UNVERIFIED", lambda: self.surface.prepare_round_trip(forged, None, self.edits()))
        self.assertEqual(self.calls, [])

    def test_failed_full_source_verification_never_calls_serializer(self):
        self.reader_failure = True
        self.assertCode("PARSER.SYNTAX.MALFORMED", self.prepare)
        self.assertEqual(self.calls, ["read"])

    def test_factory_descriptor_and_method_are_pinned(self):
        codec = _RoundTripCodec(self)
        self.descriptor = replace(self.descriptor, round_trip_serializer_factory=lambda: codec)
        codec.descriptor = self.descriptor
        codec.prepare = lambda request: request
        self.surface = self.make_surface()
        serializer = self.surface._registry.create_round_trip_serializer(self.descriptor)
        codec.descriptor = replace(self.descriptor)
        codec.prepare = lambda request: self.fail("mutated method was called")
        self.assertIs(serializer.descriptor, self.descriptor)
        sentinel = object()
        self.assertIs(serializer.prepare(sentinel), sentinel)
        with self.assertRaises(AttributeError):
            serializer._prepare = lambda request: None
        self.assertCode("PARSER.SELECTION.FACTORY_MISMATCH", self.prepare)
        self.assertCode("PARSER.SELECTION.DESCRIPTOR_UNREGISTERED", lambda:
            self.surface._registry.create_round_trip_serializer(replace(self.descriptor)))

    def test_factory_failure_and_invalid_products_are_body_safe(self):
        def failed_factory():
            raise RuntimeError("secret factory body")
        for factory, code in (
            (failed_factory, "PARSER.SELECTION.FACTORY_FAILED"),
            (lambda: object(), "PARSER.SELECTION.FACTORY_MISMATCH"),
        ):
            with self.subTest(code=code):
                self.descriptor = replace(self.descriptor, round_trip_serializer_factory=factory)
                self.surface = self.make_surface()
                self.assertCode(code, self.prepare)

    def test_truncated_sealed_source_cannot_issue_preparation(self):
        opened = self.opened()
        # Fault at the actual sealed file, not a forged terminal/mock validator.
        opened._snapshot._temporary.truncate(1)
        self.assertCode("PARSER.SOURCE.READ_FAILED", lambda: self.prepare(opened))
        self.assertNotIn("factory", self.calls)

    def test_dropping_or_closing_preparation_releases_retained_bytes(self):
        import gc
        import weakref
        prepared = self.prepare()
        self.assertEqual(len(self.surface._prepared_round_trips), 1)
        prepared.close()
        self.assertEqual(len(self.surface._prepared_round_trips), 0)
        # A separate unretained preparation is discarded through weak ownership.
        opened = self.opened()
        prepared = self.surface.prepare_round_trip(opened, self.token(opened), self.edits())
        reference = weakref.ref(prepared)
        del prepared
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(len(self.surface._prepared_round_trips), 0)

    def test_serializer_failures_are_safe_and_leases_close(self):
        for failure, code in (
            (RuntimeError("secret body"), "PARSER.SOURCE.WRITE_FAILED"),
            (ContractViolation("PARSER.ROUNDTRIP.PROTECTION", "secret body"), "PARSER.ROUNDTRIP.PROTECTION"),
            (ContractViolation("PARSER.UNKNOWN.BODY", "secret body"), "PARSER.PLUGIN.ISSUE_UNDECLARED"),
        ):
            with self.subTest(code=code):
                self.serializer_failure = failure
                self.assertCode(code, self.prepare)
                self.assertTrue(self.last_request.source.closed)

    def test_serializer_must_consume_verified_source(self):
        self.consume = False
        self.assertCode("PARSER.SOURCE.UNVERIFIED", self.prepare)

    def test_output_identity_source_and_digest_are_reproved(self):
        for field, value in (
            ("codec_identity", CodecIdentity("foreign", "codec", "1")),
            ("format_id", contracts.FormatId("foreign")),
            ("source_fingerprint", "0" * 64),
            ("output_fingerprint", "0" * 64),
        ):
            with self.subTest(field=field):
                def override(request, field=field, value=value):
                    return replace(contracts.PreparedFormatBytes(
                        self.descriptor.identity, self.descriptor.format_id,
                        request.terminal.source.content_sha256, digest(self.output), self.output,
                    ), **{field: value})
                self.override = override
                self.assertCode("PARSER.SELECTION.FACTORY_MISMATCH", self.prepare)

    def test_output_and_private_payload_are_bounded(self):
        self.output = b"x" * 1025
        self.assertCode("PARSER.LIMIT.OUTPUT", self.prepare)
        opened = self.opened()
        opaque = b"p" * 1025
        self.assertCode("PARSER.LIMIT.OPAQUE_PAYLOAD", lambda: self.prepare(opened, self.token(
            opened, opaque_payload=opaque, format_state_fingerprint=digest(opaque),
        )))
        self.assertEqual(self.calls, ["read", "factory", "prepare"])

    def test_round_trip_limits_are_independent_of_input_limit(self):
        self.descriptor = replace(
            self.descriptor, round_trip_limits=contracts.RoundTripLimits(2048, 2048),
        )
        self.surface = self.make_surface()
        self.output = b"x" * 2048
        opened = self.opened()
        opaque = b"p" * 2048
        prepared = self.prepare(opened, self.token(
            opened, opaque_payload=opaque, format_state_fingerprint=digest(opaque),
        ))
        self.assertEqual(prepared.byte_count, 2048)
        self.assertEqual(self.last_request.limits.max_output_bytes, 2048)
        self.assertEqual(self.last_request.limits.max_opaque_payload_bytes, 2048)
        self.output += b"x"
        self.assertCode("PARSER.LIMIT.OUTPUT", self.prepare)

    def test_neutral_contracts_reject_invalid_types_and_missing_limits(self):
        for values in ((0, 1), (1, -1), (True, 1)):
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                contracts.RoundTripLimits(*values)
        with self.assertRaises(ValueError):
            replace(self.descriptor, round_trip_limits=None)
        with self.assertRaises(ValueError):
            replace(self.descriptor, capabilities=replace(
                self.descriptor.capabilities, source_round_trip_write=False,
            ))
        with self.assertRaises(TypeError):
            contracts.RoundTripSegmentEdit("slot", None)

    def test_edits_are_bounded_and_duplicate_ids_rejected(self):
        opened = self.opened()
        cases = (
            ((contracts.RoundTripSegmentEdit("slot", "x" * 257),), "PARSER.LIMIT.FIELD"),
            (tuple(contracts.RoundTripSegmentEdit(str(i), "") for i in range(33)), "PARSER.LIMIT.RECORD"),
            ((contracts.RoundTripSegmentEdit("slot", ""),) * 2, "PARSER.SYNTAX.DUPLICATE_LOCAL_ID"),
        )
        for edits, code in cases:
            with self.subTest(code=code):
                self.assertCode(code, lambda: self.surface.prepare_round_trip(opened, self.token(opened), edits))
        self.assertEqual(self.calls, [])

    def test_diagnostics_are_bounded_declared_and_body_safe(self):
        issue = ParseIssue("PARSER.ROUNDTRIP.WARNING", IssueSeverity.WARNING, "secret source body", line_number=1)
        self.diagnostics = (issue,)
        prepared = self.prepare()
        self.assertEqual(prepared.diagnostics[0].code, issue.code)
        self.assertEqual(prepared.diagnostics[0].line_number, 1)
        self.assertNotIn("secret", prepared.diagnostics[0].safe_summary)
        self.diagnostics = (replace(issue, code="PARSER.UNKNOWN.BODY"),)
        self.assertCode("PARSER.PLUGIN.ISSUE_UNDECLARED", self.prepare)
        self.diagnostics = (issue,) * 9
        self.assertCode("PARSER.LIMIT.DIAGNOSTICS", self.prepare)
        self.diagnostics = (replace(issue, severity=IssueSeverity.FATAL),)
        failure = self.assertCode(issue.code, self.prepare)
        self.assertEqual(failure.diagnostics[0].line_number, 1)

    def test_cancellation_before_or_after_provider_never_issues_preparation(self):
        opened = self.opened()
        self.cancellation.cancel()
        self.assertCode("PARSER.SOURCE.CANCELLED", lambda: self.prepare(opened))
        self.assertEqual(self.calls, [])
        self.cancellation = CancellationToken()
        self.cancel_during = True
        self.assertCode("PARSER.SOURCE.CANCELLED", self.prepare)
        self.assertTrue(self.last_request.source.closed)

    def test_prepared_authority_is_bound_to_surface_and_live_source_and_can_close(self):
        opened = self.opened()
        prepared = self.prepare(opened)
        binding = self.surface._require_round_trip_preparation(prepared)
        self.assertEqual(binding.serialized.payload, self.output)
        self.assertCode("PARSER.CAPABILITY.INVALID_PREPARATION", lambda: self.make_surface()._require_round_trip_preparation(prepared))
        self.assertCode("PARSER.CAPABILITY.INVALID_PREPARATION", lambda: self.surface._require_round_trip_preparation(object.__new__(composition.PreparedRoundTripWrite)))
        with self.assertRaises(ContractViolation):
            composition.PreparedRoundTripWrite()
        opened.close()
        self.assertCode("PARSER.SOURCE.SNAPSHOT_RELEASED", lambda: self.surface._require_round_trip_preparation(prepared))
        prepared.close()
        prepared.close()
        self.assertTrue(prepared.closed)
        self.assertCode("PARSER.CAPABILITY.INVALID_PREPARATION", lambda: self.surface._require_round_trip_preparation(prepared))


if __name__ == "__main__":
    unittest.main()
