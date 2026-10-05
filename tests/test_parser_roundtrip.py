from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
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
from platform_fs_contracts import (
    BoundDirectoryAuthority, CandidateFile, PendingPublication,
    PlatformFileError, PlatformFileErrorCode,
)
from tests.test_parser_composition import _Provider
from tests.test_parser_registry import _descriptor


if sys.platform == "win32":
    import platform_fs_windows as native_fs
    _PlatformAdapter = native_fs.WindowsPlatformAdapter
    _CandidateFile = native_fs._WindowsCandidateFile
    _BoundDirectory = native_fs._WindowsBoundDirectory
    _BoundRegularFile = native_fs._WindowsBoundRegularFile
    _POST_NAMING_FAULTS = ("publish_after_rename", "publish_after_destination_reopen")
else:
    import platform_fs_posix as native_fs
    _PlatformAdapter = native_fs.PosixPlatformAdapter
    _CandidateFile = native_fs._PosixCandidateFile
    _BoundDirectory = native_fs._PosixBoundDirectory
    _BoundRegularFile = native_fs._PosixBoundRegularFile
    _POST_NAMING_FAULTS = ("candidate_after_naming", "published_after_open")


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
        with mock.patch.object(composition, "_rooted_backend", side_effect=AssertionError("target open")) as target_open, \
             mock.patch.object(composition, "_atomic_write_bytes", side_effect=AssertionError("publish")) as publish:
            prepared = self.surface.prepare_round_trip(opened, self.token(opened), self.edits())
        self.addCleanup(prepared.close)
        target_open.assert_not_called()
        publish.assert_not_called()
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
        self.assertTrue(prepared.closed)
        self.assertCode("PARSER.CAPABILITY.INVALID_PREPARATION", lambda: self.surface._require_round_trip_preparation(prepared))
        prepared.close()
        prepared.close()
        self.assertTrue(prepared.closed)
        self.assertCode("PARSER.CAPABILITY.INVALID_PREPARATION", lambda: self.surface._require_round_trip_preparation(prepared))


    def target_reference(self, target=None):
        return contracts.TargetReference(str(self.root), str(target or self.target), "export")

    def bound_preparation(self, target=None, opened=None):
        prepared = self.prepare(opened)
        self.surface.bind_round_trip_target(prepared, self.target_reference(target))
        bound = self.surface._require_round_trip_preparation(prepared).target
        self.assertIsInstance(bound.parent, _BoundDirectory)
        return prepared

    def assert_failed_publication(self, prepared, code=None, surface=None):
        result = (surface or self.surface).write_prepared(prepared)
        self.assertEqual(result.outcome.value, "failed")
        self.assertIsNone(result.receipt)
        self.assertNotIn("secret", result.safe_summary)
        if code is not None:
            self.assertEqual(result.code, code)
        return result

    def test_publish_exact_prepared_bytes_existing_and_absent_without_reserialization(self):
        for existing in (True, False):
            with self.subTest(existing=existing):
                target = self.target if existing else self.root / "new.target"
                before = set(self.root.iterdir())
                prepared = self.bound_preparation(target)
                self.assertEqual(set(self.root.iterdir()), before)
                with mock.patch.object(type(self.surface._registry), "create_canonical_serializer", side_effect=AssertionError("canonical")) as canonical:
                    result = self.surface.write_prepared(prepared)
                canonical.assert_not_called()
                self.assertEqual(result.outcome.value, "published")
                self.assertIsNone(result.code)
                self.assertEqual(target.read_bytes(), self.output)
                self.assertEqual(result.receipt.content_sha256, digest(self.output))
                self.assertEqual(result.receipt.byte_count, len(self.output))
                self.assertTrue(prepared.closed)
                self.assert_failed_publication(prepared, "PARSER.CAPABILITY.INVALID_PREPARATION")
        self.assertEqual(self.calls.count("prepare"), 2)

    def test_publish_requires_issued_bound_once_preparation(self):
        prepared = self.prepare()
        self.assert_failed_publication(prepared, "PARSER.CAPABILITY.INVALID_PREPARATION")
        self.assertTrue(prepared.closed)
        prepared = self.bound_preparation()
        self.assert_failed_publication(prepared, "PARSER.CAPABILITY.INVALID_PREPARATION", self.make_surface())
        self.assertCode("PARSER.CAPABILITY.INVALID_PREPARATION", lambda: self.surface.bind_round_trip_target(prepared, self.target_reference()))
        for forged in (object(), object.__new__(composition.PreparedRoundTripWrite), self.surface._require_round_trip_preparation(prepared).serialized):
            self.assert_failed_publication(forged, "PARSER.CAPABILITY.INVALID_PREPARATION")
        prepared.close()
        self.assert_failed_publication(prepared, "PARSER.CAPABILITY.INVALID_PREPARATION")

    def test_publish_rejects_target_replaced_changed_removed_or_created_before_mutation(self):
        for change in ("replace", "edit", "remove", "create"):
            with self.subTest(change=change):
                self.target.write_bytes(b"old")
                if change == "create":
                    self.target.unlink()
                prepared = self.bound_preparation()
                if change == "replace":
                    replacement = self.root / "replacement"
                    replacement.write_bytes(b"changed")
                    mutate = lambda: replacement.replace(self.target)
                elif change in ("edit", "create"):
                    mutate = lambda: self.target.write_bytes(b"changed")
                else:
                    mutate = self.target.unlink
                code = "PARSER.SOURCE.STALE"
                if sys.platform == "win32" and change != "create":
                    # Retained SOURCE handles reject these external changes on
                    # Windows. Releasing preparation must release that guard.
                    with self.assertRaises(PermissionError) as caught:
                        mutate()
                    self.assertEqual(caught.exception.errno, 13)
                    self.assertIn(caught.exception.winerror, (None, 32) if change == "edit" else (32,))
                    self.assertEqual(self.target.read_bytes(), b"old")
                    prepared.close()
                    code = "PARSER.CAPABILITY.INVALID_PREPARATION"
                mutate()
                before = set(self.root.iterdir())
                with mock.patch.object(BoundDirectoryAuthority, "create_candidate", side_effect=AssertionError("candidate")) as candidate, \
                     mock.patch.object(_PlatformAdapter, "acquire", side_effect=AssertionError("lock mutation")) as acquire:
                    self.assert_failed_publication(prepared, code)
                candidate.assert_not_called()
                acquire.assert_not_called()
                self.assertTrue(prepared.closed)
                self.assertEqual(set(self.root.iterdir()), before)
                self.assertEqual(self.target.read_bytes() if self.target.exists() else None, None if change == "remove" else b"changed")

    def test_publish_rejects_parent_drift_without_writing_either_directory(self):
        parent = self.root / "parent"
        parent.mkdir()
        target = parent / "output"
        target.write_bytes(b"old")
        prepared = self.bound_preparation(target)
        retired = self.root / "retired"
        code = "PARSER.SOURCE.STALE"
        if sys.platform == "win32":
            with self.assertRaises(PermissionError) as caught:
                parent.rename(retired)
            self.assertEqual(caught.exception.winerror, 32)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertFalse(retired.exists())
            prepared.close()
            code = "PARSER.CAPABILITY.INVALID_PREPARATION"
        parent.rename(retired)
        parent.mkdir()
        with mock.patch.object(BoundDirectoryAuthority, "create_candidate", side_effect=AssertionError("candidate")) as candidate, \
             mock.patch.object(_PlatformAdapter, "acquire", side_effect=AssertionError("lock mutation")) as acquire:
            self.assert_failed_publication(prepared, code)
        candidate.assert_not_called()
        acquire.assert_not_called()
        self.assertTrue(prepared.closed)
        self.assertEqual(list(parent.iterdir()), [])
        self.assertEqual((retired / "output").read_bytes(), b"old")

    def test_publish_rechecks_target_after_lock_and_before_begin_publish(self):
        import parser_source
        seams = ("source-release", "flush") if sys.platform == "win32" else ("lock", "flush")
        for seam in seams:
            with self.subTest(seam=seam):
                self.target.write_bytes(b"old")
                if sys.platform == "win32" and seam == "flush":
                    # Existing SOURCE is still retained at flush on Windows;
                    # an absent destination allows a real external creation.
                    self.target.unlink()
                prepared = self.bound_preparation()
                owner, method = {
                    "source-release": (parser_source.BoundWriteTarget, "release_existing_for_publish"),
                    "lock": (_PlatformAdapter, "acquire"),
                    "flush": (CandidateFile, "flush_content"),
                }[seam]
                original = getattr(owner, method)
                def replace_after(*args, **kwargs):
                    value = original(*args, **kwargs)
                    self.target.write_bytes(b"competitor")
                    return value
                with mock.patch.object(owner, method, autospec=True, side_effect=replace_after) as drift, \
                     mock.patch.object(BoundDirectoryAuthority, "begin_publish", side_effect=AssertionError("publication")) as publish:
                    self.assert_failed_publication(prepared, "PARSER.SOURCE.STALE")
                drift.assert_called_once()
                publish.assert_not_called()
                self.assertTrue(prepared.closed)
                self.assertEqual(self.target.read_bytes(), b"competitor")
                self.assertEqual(list(self.root.glob(".parser-*.tmp")), [])

    def test_publish_source_invalidations_and_cancel_precede_target_mutation(self):
        for reason in ("source-closed", "source-identity", "source-content", "cancel"):
            with self.subTest(reason=reason):
                self.cancellation = CancellationToken()
                opened = self.opened()
                prepared = self.bound_preparation(opened=opened)
                if reason == "source-closed":
                    opened.close()
                elif reason == "source-identity":
                    opened._snapshot.identity = replace(opened.source_identity, content_sha256="0" * 64)
                elif reason == "source-content":
                    opened._snapshot._temporary.seek(0)
                    opened._snapshot._temporary.write(b"changed source!\n")
                    opened._snapshot._temporary.flush()
                else:
                    self.cancellation.cancel()
                before = set(self.root.iterdir())
                with mock.patch.object(BoundDirectoryAuthority, "create_candidate", side_effect=AssertionError("candidate")) as candidate, \
                     mock.patch.object(_PlatformAdapter, "acquire", side_effect=AssertionError("lock mutation")) as acquire:
                    self.assert_failed_publication(prepared)
                candidate.assert_not_called()
                acquire.assert_not_called()
                self.assertEqual(set(self.root.iterdir()), before)
                self.assertEqual(self.target.read_bytes(), b"leave target untouched")
                self.assertTrue(prepared.closed)

    def test_publish_prepublication_faults_preserve_old_cleanup_and_consume(self):
        # Windows arms recovery handling inside _rename_candidate, before its
        # native rename call. Fail at entry to prove a prepublication failure.
        rename_fault = (_BoundDirectory, "_rename_candidate") if sys.platform == "win32" else (native_fs.os, "replace")
        for owner, method in ((_CandidateFile, "_write_chunks"), (_CandidateFile, "_flush_content"), rename_fault):
            with self.subTest(method=method):
                prepared = self.bound_preparation()
                with mock.patch.object(owner, method, side_effect=OSError("secret failure")) as fault:
                    self.assert_failed_publication(prepared, "PARSER.SOURCE.WRITE_FAILED")
                fault.assert_called_once()
                self.assertTrue(prepared.closed)
                self.assertEqual(self.target.read_bytes(), b"leave target untouched")
                self.assertEqual(list(self.root.glob(".parser-*.tmp")), [])
                self.assert_failed_publication(prepared, "PARSER.CAPABILITY.INVALID_PREPARATION")

    def test_publish_after_naming_readback_and_terminal_faults_are_uncertain(self):
        for phase in (*_POST_NAMING_FAULTS, "readback", "terminal", "pending-factory", "pending-close"):
            with self.subTest(phase=phase):
                self.target.write_bytes(b"old before " + phase.encode("ascii"))
                fault_points = []
                def fault(point):
                    fault_points.append(point)
                    if point == phase:
                        raise RuntimeError("secret postpublication fault")
                self.surface._platform_backend_factory = lambda root: _PlatformAdapter(_fault_injector=fault)
                prepared = self.bound_preparation()
                bound = self.surface._require_round_trip_preparation(prepared).target
                patch = nullcontext()
                if phase == "readback":
                    patch = mock.patch.object(_BoundRegularFile, "read_all", side_effect=RuntimeError("secret readback"))
                elif phase == "terminal":
                    patch = mock.patch.object(PendingPublication, "terminal_reproof", side_effect=RuntimeError("secret terminal"))
                elif phase == "pending-factory":
                    patch = mock.patch.object(_BoundDirectory, "_create_pending_publication", side_effect=PlatformFileError(PlatformFileErrorCode.PUBLISH_FAILED, retryable=True))
                elif phase == "pending-close":
                    original_close = PendingPublication.close
                    def close_then_fail(pending):
                        original_close(pending)
                        raise RuntimeError("secret close callback")
                    patch = mock.patch.object(PendingPublication, "close", autospec=True, side_effect=close_then_fail)
                with patch as injected:
                    result = self.surface.write_prepared(prepared)
                if phase in _POST_NAMING_FAULTS:
                    self.assertEqual(fault_points.count(phase), 1)
                else:
                    injected.assert_called_once()
                self.assertEqual(result.outcome.value, "uncertain")
                self.assertEqual(result.code, "PARSER.SOURCE.WRITE_RECOVERY_REQUIRED")
                self.assertIsNone(result.receipt)
                self.assertNotIn("secret", result.safe_summary)
                self.assertEqual(self.target.read_bytes(), self.output)
                self.assertTrue(prepared.closed)
                self.assertTrue(bound.parent.closed)
                self.assertTrue(bound.existing.closed)
                self.assertEqual(list(self.root.glob(".parser-*.tmp")), [])
                self.assert_failed_publication(prepared, "PARSER.CAPABILITY.INVALID_PREPARATION")

    def test_publish_cancellation_during_candidate_stops_next_mutation(self):
        prepared = self.bound_preparation()
        original = CandidateFile.write_all
        def cancel_after_write(*args, **kwargs):
            result = original(*args, **kwargs)
            self.cancellation.cancel()
            return result
        with mock.patch.object(CandidateFile, "write_all", autospec=True, side_effect=cancel_after_write) as write, \
             mock.patch.object(CandidateFile, "flush_content", side_effect=AssertionError("new mutation")) as flush:
            self.assert_failed_publication(prepared, "PARSER.SOURCE.CANCELLED")
        write.assert_called_once()
        flush.assert_not_called()
        self.assertTrue(prepared.closed)
        self.assertEqual(self.target.read_bytes(), b"leave target untouched")
        self.assertEqual(list(self.root.glob(".parser-*.tmp")), [])

    def test_publish_cancellation_at_existing_handle_release_does_not_start_naming(self):
        import parser_source
        prepared = self.bound_preparation()
        original = parser_source.BoundWriteTarget.release_existing_for_publish
        def cancel_after_release(bound):
            original(bound)
            self.cancellation.cancel()
        with mock.patch.object(parser_source.BoundWriteTarget, "release_existing_for_publish", autospec=True, side_effect=cancel_after_release) as release, \
             mock.patch.object(BoundDirectoryAuthority, "begin_publish", side_effect=AssertionError("new mutation")) as publish:
            self.assert_failed_publication(prepared, "PARSER.SOURCE.CANCELLED")
        release.assert_called_once()
        publish.assert_not_called()
        self.assertTrue(prepared.closed)
        self.assertEqual(self.target.read_bytes(), b"leave target untouched")
        self.assertEqual(list(self.root.glob(".parser-*.tmp")), [])

    def test_publish_source_snapshot_is_independent_of_original_path(self):
        prepared = self.bound_preparation()
        self.source.unlink()
        result = self.surface.write_prepared(prepared)
        self.assertEqual(result.outcome.value, "published")
        self.assertEqual(self.target.read_bytes(), self.output)

    def test_publish_rejects_modified_output_even_with_matching_recomputed_digest(self):
        prepared = self.bound_preparation()
        private = self.surface._require_round_trip_preparation(prepared).serialized
        object.__setattr__(private, "payload", b"forged output")
        object.__setattr__(private, "output_fingerprint", digest(b"forged output"))
        self.assert_failed_publication(prepared, "PARSER.CAPABILITY.INVALID_PREPARATION")
        self.assertEqual(self.target.read_bytes(), b"leave target untouched")

    def test_target_binding_failure_discards_preparation_without_creating_directories(self):
        prepared = self.prepare()
        missing = self.root / "missing" / "export"
        with self.assertRaises(ContractViolation):
            self.surface.bind_round_trip_target(prepared, self.target_reference(missing))
        self.assertTrue(prepared.closed)
        self.assertFalse(missing.parent.exists())
        self.assert_failed_publication(prepared, "PARSER.CAPABILITY.INVALID_PREPARATION")

    def test_publish_concurrent_reuse_is_rejected_while_first_write_is_in_flight(self):
        prepared = self.bound_preparation()
        writing = threading.Event()
        release = threading.Event()
        original = CandidateFile.write_all
        def paused_write(*args, **kwargs):
            writing.set()
            if not release.wait(5):
                raise RuntimeError("test release timeout")
            return original(*args, **kwargs)
        with mock.patch.object(CandidateFile, "write_all", autospec=True, side_effect=paused_write) as write, ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.surface.write_prepared, prepared)
            try:
                self.assertTrue(writing.wait(5))
                second = pool.submit(self.surface.write_prepared, prepared)
                rejected = second.result(2)
                self.assertEqual(rejected.outcome.value, "failed")
                self.assertEqual(rejected.code, "PARSER.CAPABILITY.INVALID_PREPARATION")
                self.assertIsNone(rejected.receipt)
            finally:
                release.set()
            self.assertEqual(first.result(5).outcome.value, "published")
        write.assert_called_once()
        self.assertTrue(prepared.closed)
        self.assertEqual(self.target.read_bytes(), self.output)

    def test_input_close_releases_every_binding_even_when_one_close_reports_failure(self):
        import parser_source
        opened = self.opened()
        preparations = [self.bound_preparation(opened=opened) for _ in range(2)]
        bindings = [self.surface._require_round_trip_preparation(item).target for item in preparations]
        original = parser_source.BoundWriteTarget.close
        def fail_after_close(bound):
            original(bound)
            if bound is bindings[0]:
                raise RuntimeError("close failure")
        with mock.patch.object(parser_source.BoundWriteTarget, "close", autospec=True, side_effect=fail_after_close) as close:
            with self.assertRaises(RuntimeError):
                opened.close()
        close.assert_has_calls([mock.call(bound) for bound in bindings], any_order=True)
        self.assertTrue(opened._snapshot.released)
        for prepared, bound in zip(preparations, bindings):
            self.assertTrue(prepared.closed)
            self.assertTrue(bound.parent.closed)
            self.assertTrue(bound.existing.closed)
            self.assertNotIn(prepared, self.surface._prepared_round_trips)

    def test_publication_result_contract_never_allows_unproved_success(self):
        outcome = contracts.PreparedWriteOutcome
        result = contracts.PreparedWriteResult
        for value in (outcome.FAILED, outcome.UNCERTAIN):
            with self.assertRaises((TypeError, ValueError)):
                result(value)
        with self.assertRaises(TypeError):
            result(outcome.PUBLISHED)
        with self.assertRaises(TypeError):
            result("published")
        published = self.surface.write_prepared(self.bound_preparation())
        for value in (outcome.FAILED, outcome.UNCERTAIN):
            with self.assertRaises(ValueError):
                result(value, receipt=published.receipt, code="PARSER.SOURCE.WRITE_FAILED")
        with self.assertRaises(ValueError):
            result(outcome.PUBLISHED, receipt=published.receipt, code="PARSER.SOURCE.WRITE_FAILED")

    def test_bound_target_lifecycle_close_and_publish_release_retained_authorities(self):
        for action in ("close", "publish", "input-close"):
            with self.subTest(action=action):
                opened = self.opened()
                prepared = self.bound_preparation(opened=opened)
                bound = self.surface._require_round_trip_preparation(prepared).target
                parent, existing = bound.parent, bound.existing
                if action == "close":
                    prepared.close()
                elif action == "input-close":
                    opened.close()
                else:
                    self.assertEqual(self.surface.write_prepared(prepared).outcome.value, "published")
                self.assertTrue(parent.closed)
                self.assertTrue(existing.closed)
                self.assertNotIn(prepared, self.surface._prepared_round_trips)


if __name__ == "__main__":
    unittest.main()
