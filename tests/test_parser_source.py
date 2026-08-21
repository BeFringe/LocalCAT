from __future__ import annotations

import gc
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class _FixtureMixin:
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="parser-source-test-")
        self.root = Path(self._temporary.name) / "safe"
        (self.root / "input" / "nested").mkdir(parents=True)
        (self.root / "output").mkdir()
        self.source = self.root / "input" / "nested" / "chapter.txt"
        self.source.write_bytes(b"first\nsecond\n")
        self.target = self.root / "output" / "project.json"
        self.target.write_bytes(b"old-target")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def source_reference(self):
        from parser_contracts import SourceReference

        return SourceReference(
            safe_root=str(self.root),
            selected_path=str(self.source),
            display_hint="chapter.txt",
        )

    def target_reference(self):
        from parser_contracts import TargetReference

        return TargetReference(
            safe_root=str(self.root),
            selected_path=str(self.target),
            display_hint="project.json",
        )

    @staticmethod
    def profile(
        *,
        max_input_bytes: int = 1024,
        max_records: int = 8,
        max_materialized_records: int = 8,
    ):
        from parser_contracts import LimitProfile

        return LimitProfile(
            profile_id="test-parser-v1",
            profile_version=1,
            max_input_bytes=max_input_bytes,
            max_decoded_field_chars=64,
            max_records=max_records,
            max_materialized_records=max_materialized_records,
            max_retained_issues=4,
            declared_issue_codes=tuple(
                sorted(
                    (
                        "PARSER.LIMIT.FIELD",
                        "PARSER.LIMIT.INPUT",
                        "PARSER.LIMIT.MATERIALIZATION",
                        "PARSER.LIMIT.METADATA",
                        "PARSER.LIMIT.RECORD",
                        "PARSER.PLUGIN.ISSUE_UNDECLARED",
                        "PARSER.SOURCE.CANCELLED",
                        "PARSER.SOURCE.READ_FAILED",
                        "PARSER.SOURCE.STALE",
                        "PARSER.SYNTAX.DUPLICATE_LOCAL_ID",
                        "PARSER.SYNTAX.INVALID_EVENT",
                        "PARSER.SYNTAX.INVALID_HEADER",
                        "PARSER.SYNTAX.MALFORMED",
                        "PARSER.TEST.WARNING",
                    )
                )
            ),
            max_metadata_entries_per_container=4,
            max_metadata_decoded_chars_per_container=64,
            max_metadata_decoded_chars_total=128,
            max_structure_depth=4,
        )

    @classmethod
    def descriptor(cls, *, purpose=None, profile=None, consumption_policy=None):
        from parser_contracts import (
            CodecCapabilities,
            CodecDescriptor,
            CodecIdentity,
            EffectivePurpose,
            FormatId,
            InputConsumptionPolicy,
        )

        purpose = purpose or EffectivePurpose.PROJECT_DOCUMENT
        profile = profile or cls.profile()
        consumption_policy = (
            consumption_policy or InputConsumptionPolicy.SEALED_BYTES_EOF
        )
        return CodecDescriptor(
            identity=CodecIdentity("tests", "scripted", "1"),
            purpose=purpose,
            format_id=FormatId("test-format-v1"),
            extensions=(".test",),
            mime_types=(),
            sniff_prefixes=(),
            capabilities=CodecCapabilities(
                readable=True,
                validatable=True,
                canonical_write=False,
                source_round_trip_write=False,
                streaming_input=True,
                iterator_view=True,
                materialized_view=True,
                format_profile=profile.profile_id,
            ),
            limit_profile=profile,
            input_consumption_policy=consumption_policy,
            reader_factory=lambda: None,
            canonical_serializer_factory=None,
        )

    @staticmethod
    def request(*, purpose=None):
        from parser_contracts import EffectivePurpose, FormatId, ReadRequest

        return ReadRequest(
            purpose=purpose or EffectivePurpose.PROJECT_DOCUMENT,
            format_id=FormatId("test-format-v1"),
        )


class SharedSourceContractTests(unittest.TestCase):
    def test_shared_descriptor_and_file_contracts_are_immutable_and_bounded(self) -> None:
        from dataclasses import FrozenInstanceError

        from parser_contracts import (
            CodecCapabilities,
            CodecDescriptor,
            CodecIdentity,
            EffectivePurpose,
            FormatId,
            InputConsumptionPolicy,
            LimitProfile,
            SourceReference,
            TargetReference,
            builtin_purpose_for_format,
        )

        profile = _FixtureMixin.profile()
        descriptor = CodecDescriptor(
            identity=CodecIdentity("builtin", "localcat-json", "1"),
            purpose=EffectivePurpose.PROJECT_DOCUMENT,
            format_id=FormatId("localcat-json-v1"),
            extensions=(".JSON",),
            mime_types=("Application/JSON",),
            sniff_prefixes=(b"[", b"{"),
            capabilities=CodecCapabilities(
                readable=True,
                validatable=True,
                canonical_write=False,
                source_round_trip_write=False,
                streaming_input=False,
                iterator_view=True,
                materialized_view=True,
                format_profile=profile.profile_id,
            ),
            limit_profile=profile,
            input_consumption_policy=InputConsumptionPolicy.SEALED_BYTES_EOF,
            reader_factory=lambda: None,
            canonical_serializer_factory=None,
        )
        self.assertEqual(descriptor.extensions, (".json",))
        self.assertEqual(descriptor.mime_types, ("application/json",))
        self.assertEqual(descriptor.declared_issue_codes, profile.declared_issue_codes)
        self.assertEqual(
            builtin_purpose_for_format(FormatId("localcat-json-v1")),
            EffectivePurpose.PROJECT_DOCUMENT,
        )
        self.assertIsNone(builtin_purpose_for_format(FormatId("plugin-format-v1")))
        with self.assertRaises(FrozenInstanceError):
            descriptor.purpose = EffectivePurpose.TERMBASE  # type: ignore[misc]
        from dataclasses import replace

        with self.assertRaises(ValueError):
            replace(descriptor, sniff_prefixes=(b"",))

        source = SourceReference("/safe", "/safe/chapter.txt", "chapter")
        target = TargetReference("/safe", "/safe/project.json", "project")
        self.assertEqual(source.safe_root, "/safe")
        self.assertEqual(target.selected_path, "/safe/project.json")
        for contract in (SourceReference, TargetReference):
            with self.assertRaises((TypeError, ValueError)):
                contract("", "/safe/file", "file")


@unittest.skipUnless(os.name == "posix", "rooted dirfd contract is POSIX-specific")
class RootedSourceAndSnapshotTests(_FixtureMixin, unittest.TestCase):
    def test_rooted_open_and_snapshot_bind_actual_bytes_once(self) -> None:
        from parser_source import create_sealed_snapshot, open_rooted_regular_file

        with open_rooted_regular_file(self.source_reference()) as opened:
            self.assertTrue(opened.is_regular_file)
            self.assertEqual(opened.relative_path, "input/nested/chapter.txt")

        snapshot = create_sealed_snapshot(
            self.source_reference(),
            limit_profile=self.profile(),
        )
        expected = self.source.read_bytes()
        self.assertEqual(snapshot.identity.content_sha256, hashlib.sha256(expected).hexdigest())
        self.assertEqual(snapshot.identity.byte_count, len(expected))
        with snapshot.lease(self.descriptor()) as lease:
            self.assertEqual(lease.read(), expected)
        snapshot.close()
        self.assertTrue(snapshot.released)

    def test_rooted_open_rejects_escape_symlink_and_non_regular_without_consuming(self) -> None:
        from parser_contracts import SourceReference
        from parser_source import ParserSourceError, open_rooted_regular_file

        outside = self.root.parent / "outside.txt"
        outside.write_bytes(b"secret")
        link = self.root / "input" / "link.txt"
        link.symlink_to(outside)
        linked_directory = self.root / "input" / "linked-directory"
        linked_directory.symlink_to(outside.parent, target_is_directory=True)
        fifo = self.root / "input" / "named-pipe"
        os.mkfifo(fifo)
        cases = (
            (
                SourceReference(str(self.root), str(outside), "outside"),
                "PARSER.SOURCE.OUTSIDE_ROOT",
            ),
            (
                SourceReference(str(self.root), str(link), "link"),
                "PARSER.SOURCE.NOT_REGULAR",
            ),
            (
                SourceReference(
                    str(self.root),
                    str(linked_directory / "outside.txt"),
                    "linked-directory",
                ),
                "PARSER.SOURCE.NOT_REGULAR",
            ),
            (
                SourceReference(str(self.root), str(self.root / "input"), "dir"),
                "PARSER.SOURCE.NOT_REGULAR",
            ),
            (
                SourceReference(str(self.root), str(fifo), "fifo"),
                "PARSER.SOURCE.NOT_REGULAR",
            ),
            (
                SourceReference(
                    str(self.root),
                    str(self.root / "input" / ".." / "nested" / "chapter.txt"),
                    "dotdot",
                ),
                "PARSER.SOURCE.OUTSIDE_ROOT",
            ),
        )
        for reference, code in cases:
            with self.subTest(code=code), self.assertRaises(ParserSourceError) as caught:
                open_rooted_regular_file(reference)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("secret", str(caught.exception))

    def test_rooted_open_fails_closed_when_platform_binding_is_unavailable(self) -> None:
        from parser_source import ParserSourceError, open_rooted_regular_file

        with mock.patch("parser_source._rooted_handles_available", return_value=False):
            with self.assertRaises(ParserSourceError) as caught:
                open_rooted_regular_file(self.source_reference())
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE")

    def test_snapshot_rejects_input_limit_and_fstat_drift_and_cleans_temp(self) -> None:
        from parser_source import ParserSourceError, create_sealed_snapshot

        tiny = self.profile(max_input_bytes=2)
        with self.assertRaises(ParserSourceError) as caught:
            create_sealed_snapshot(self.source_reference(), limit_profile=tiny)
        self.assertEqual(caught.exception.code, "PARSER.LIMIT.INPUT")

        original_fstat = os.fstat
        calls = 0

        def drifting_fstat(fd: int):
            nonlocal calls
            status = original_fstat(fd)
            calls += 1
            if calls >= 2:
                values = list(status)
                values[6] += 1
                return os.stat_result(values)
            return status

        with mock.patch("parser_source.os.fstat", side_effect=drifting_fstat):
            with self.assertRaises(ParserSourceError) as caught:
                create_sealed_snapshot(self.source_reference(), limit_profile=self.profile())
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.STALE")

    def test_snapshot_rejects_real_source_mutation_during_its_single_copy(self) -> None:
        from parser_source import ParserSourceError, create_sealed_snapshot

        original_read = os.read
        mutated = False

        def mutate_after_read(descriptor: int, size: int) -> bytes:
            nonlocal mutated
            chunk = original_read(descriptor, size)
            if chunk and not mutated:
                mutated = True
                self.source.write_bytes(b"concurrent replacement with another size")
            return chunk

        with mock.patch("parser_source.os.read", side_effect=mutate_after_read):
            with self.assertRaises(ParserSourceError) as caught:
                create_sealed_snapshot(self.source_reference(), limit_profile=self.profile())
        self.assertTrue(mutated)
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.STALE")

    def test_sequential_leases_are_independent_offset_zero_and_non_seekable(self) -> None:
        from parser_source import create_sealed_snapshot

        snapshot = create_sealed_snapshot(self.source_reference(), limit_profile=self.profile())
        descriptor = self.descriptor()
        first = snapshot.lease(descriptor)
        second = snapshot.lease(descriptor)
        self.assertEqual(first.tell(), 0)
        self.assertEqual(second.tell(), 0)
        self.assertEqual(first.read(5), b"first")
        self.assertEqual(second.read(5), b"first")
        self.assertFalse(first.seekable())
        self.assertFalse(hasattr(first, "seek"))
        self.assertFalse(first.consumption_proved)
        self.assertEqual(first.read(), b"\nsecond\n")
        self.assertTrue(first.consumption_proved)
        first.close()
        second.close()
        snapshot.close()

    def test_seekable_lease_is_single_active_and_needs_explicit_consumption_proof(self) -> None:
        from parser_source import ParserSourceError, create_sealed_snapshot

        snapshot = create_sealed_snapshot(self.source_reference(), limit_profile=self.profile())
        from parser_contracts import InputConsumptionPolicy

        descriptor = self.descriptor(
            consumption_policy=InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET
        )
        lease = snapshot.lease(descriptor)
        self.assertTrue(lease.seekable())
        self.assertEqual(lease.read(5), b"first")
        lease.seek(0)
        self.assertEqual(lease.read(5), b"first")
        self.assertFalse(lease.consumption_proved)
        with self.assertRaises(ParserSourceError) as caught:
            snapshot.lease(descriptor)
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.LEASE_CONFLICT")
        lease.close()
        another = snapshot.lease(descriptor)
        another.close()
        snapshot.close()

    def test_cancel_release_and_stale_profile_fail_without_new_lease(self) -> None:
        from parser_source import (
            CancellationToken,
            ParserSourceError,
            create_sealed_snapshot,
            reopen_sealed_snapshot,
        )

        cancelled = CancellationToken()
        cancelled.cancel()
        with self.assertRaises(ParserSourceError) as caught:
            create_sealed_snapshot(
                self.source_reference(),
                limit_profile=self.profile(),
                cancellation=cancelled,
            )
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.CANCELLED")

        profile = self.profile()
        snapshot = create_sealed_snapshot(self.source_reference(), limit_profile=profile)
        expectation = snapshot.expectation
        snapshot.close()
        with self.assertRaises(ParserSourceError) as caught:
            snapshot.lease(self.descriptor(profile=profile))
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.SNAPSHOT_RELEASED")

        changed_profile = self.profile()
        object.__setattr__(changed_profile, "profile_version", 2)
        with self.assertRaises(ParserSourceError) as caught:
            reopen_sealed_snapshot(
                self.source_reference(),
                limit_profile=changed_profile,
                expected=expectation,
            )
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.STALE")

        self.source.write_bytes(b"changed bytes")
        with self.assertRaises(ParserSourceError) as caught:
            reopen_sealed_snapshot(
                self.source_reference(),
                limit_profile=profile,
                expected=expectation,
            )
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.STALE")

    def test_snapshot_close_waits_for_active_lease_then_cleans_up(self) -> None:
        from parser_source import create_sealed_snapshot

        snapshot = create_sealed_snapshot(self.source_reference(), limit_profile=self.profile())
        lease = snapshot.lease(self.descriptor())
        snapshot.close()
        self.assertTrue(snapshot.release_requested)
        self.assertFalse(snapshot.released)
        self.assertEqual(lease.read(1), b"f")
        lease.close()
        gc.collect()
        self.assertTrue(snapshot.released)


class _ScriptedCodec:
    def __init__(self, descriptor, events, *, consume=True, fail_after=False):
        self.descriptor = descriptor
        self._events = tuple(events)
        self._consume = consume
        self._fail_after = fail_after
        self.iter_raw_calls = 0

    def iter_raw(self, source, request):
        self.iter_raw_calls += 1
        if self._consume:
            source.read()
        yield from self._events
        if self._fail_after:
            raise RuntimeError("raw body must not escape")


class _EventAfterEofIterator:
    def __init__(self, events, late_event):
        self._events = iter(events)
        self._late_event = late_event
        self._reported_eof = False
        self._late_emitted = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._events)
        except StopIteration:
            if not self._reported_eof:
                self._reported_eof = True
                raise
            if not self._late_emitted:
                self._late_emitted = True
                return self._late_event
            raise


class _HostileEofCodec:
    def __init__(self, descriptor, events, late_event):
        self.descriptor = descriptor
        self._events = events
        self._late_event = late_event

    def iter_raw(self, source, request):
        del request
        source.read()
        return _EventAfterEofIterator(self._events, self._late_event)


@unittest.skipUnless(os.name == "posix", "snapshot contract is POSIX-specific")
class GuardedSessionTests(_FixtureMixin, unittest.TestCase):
    @staticmethod
    def header(*, metadata=()):
        from parser_contracts import DocumentHeader

        return DocumentHeader("chapter", "en-US", "zh-CN", metadata)

    @staticmethod
    def segment(local_id="s-1", *, source="source", metadata=()):
        from parser_contracts import ParsedSegment, RawSpeaker, TargetPresence

        return ParsedSegment(
            local_id=local_id,
            source=source,
            target=None,
            target_presence=TargetPresence.MISSING,
            translation_state=None,
            speaker=RawSpeaker(""),
            format_metadata=metadata,
        )

    @staticmethod
    def record(local_id="r-1"):
        from parser_contracts import RawSpeaker, ResourceRecord

        return ResourceRecord(local_id, "source", "target", RawSpeaker(""), ())

    @staticmethod
    def issue(code="PARSER.TEST.WARNING", *, fatal=False):
        from parser_contracts import IssueSeverity, ParseIssue

        return ParseIssue(
            code=code,
            severity=IssueSeverity.FATAL if fatal else IssueSeverity.WARNING,
            safe_summary="a body-safe diagnostic",
        )

    def make_snapshot(self, profile=None):
        from parser_source import create_sealed_snapshot

        return create_sealed_snapshot(
            self.source_reference(),
            limit_profile=profile or self.profile(),
        )

    def test_valid_project_stream_signs_terminal_once_after_true_eof(self) -> None:
        from parser_source import GuardedParseSession, ParserSessionError

        snapshot = self.make_snapshot()
        codec = _ScriptedCodec(
            self.descriptor(),
            (self.header(), self.segment(), self.issue()),
        )
        session = GuardedParseSession(codec, snapshot, self.request())
        self.assertEqual(tuple(session), (self.header(), self.segment(), self.issue()))
        terminal = session.verified_terminal()
        self.assertEqual(terminal.record_count, 1)
        self.assertEqual(terminal.warning_counts[0].count, 1)
        with self.assertRaises(ParserSessionError) as caught:
            session.verified_terminal()
        self.assertEqual(caught.exception.code, "PARSER.SESSION.TERMINAL_ALREADY_ISSUED")
        session.close()
        snapshot.close()

    def test_header_cardinality_purpose_event_kind_and_ids_fail_closed(self) -> None:
        from parser_contracts import EffectivePurpose
        from parser_source import GuardedParseSession, ParserSessionError

        project_cases = (
            ((), "PARSER.SYNTAX.INVALID_HEADER"),
            ((self.segment(),), "PARSER.SYNTAX.INVALID_HEADER"),
            ((self.header(), self.header()), "PARSER.SYNTAX.INVALID_HEADER"),
            ((self.header(), self.record()), "PARSER.SYNTAX.INVALID_EVENT"),
            (
                (self.header(), self.segment("same"), self.segment("same")),
                "PARSER.SYNTAX.DUPLICATE_LOCAL_ID",
            ),
        )
        for events, code in project_cases:
            with self.subTest(code=code):
                snapshot = self.make_snapshot()
                session = GuardedParseSession(
                    _ScriptedCodec(self.descriptor(), events),
                    snapshot,
                    self.request(),
                )
                with self.assertRaises(ParserSessionError) as caught:
                    tuple(session)
                self.assertEqual(caught.exception.code, code)
                with self.assertRaises(ParserSessionError):
                    session.verified_terminal()
                session.close()
                snapshot.close()

        purpose = EffectivePurpose.TRANSLATION_MEMORY
        snapshot = self.make_snapshot()
        session = GuardedParseSession(
            _ScriptedCodec(
                self.descriptor(purpose=purpose),
                (self.header(), self.record()),
            ),
            snapshot,
            self.request(purpose=purpose),
        )
        with self.assertRaises(ParserSessionError) as caught:
            tuple(session)
        self.assertEqual(caught.exception.code, "PARSER.SYNTAX.INVALID_HEADER")
        session.close()
        snapshot.close()

    def test_unknown_terminal_fatal_tail_unconsumed_and_raw_exception_never_sign(self) -> None:
        from parser_contracts import _issue_terminal_success
        from parser_source import GuardedParseSession, ParserSessionError

        descriptor = self.descriptor()
        terminal_snapshot = self.make_snapshot()
        fake_terminal = _issue_terminal_success(
            source=terminal_snapshot.identity,
            codec_identity=descriptor.identity,
            limit_profile=descriptor.limit_profile,
            record_count=0,
            warning_counts=(),
            issues_truncated=False,
        )
        terminal_snapshot.close()
        cases = (
            ((self.header(), fake_terminal), True, False, "PARSER.SYNTAX.INVALID_EVENT"),
            (
                (self.header(), self.segment(), self.issue("PARSER.SYNTAX.MALFORMED", fatal=True)),
                True,
                False,
                "PARSER.SYNTAX.MALFORMED",
            ),
            ((self.header(), self.segment()), False, False, "PARSER.SOURCE.READ_FAILED"),
            ((self.header(), self.segment()), True, True, "PARSER.SYNTAX.MALFORMED"),
        )
        for events, consume, fail_after, code in cases:
            with self.subTest(code=code):
                snapshot = self.make_snapshot()
                session = GuardedParseSession(
                    _ScriptedCodec(
                        descriptor,
                        events,
                        consume=consume,
                        fail_after=fail_after,
                    ),
                    snapshot,
                    self.request(),
                )
                with self.assertRaises(ParserSessionError) as caught:
                    tuple(session)
                self.assertEqual(caught.exception.code, code)
                with self.assertRaises(ParserSessionError):
                    session.verified_terminal()
                session.close()
                snapshot.close()

    def test_event_after_observed_eof_is_detected_before_terminal(self) -> None:
        from parser_source import GuardedParseSession, ParserSessionError

        snapshot = self.make_snapshot()
        session = GuardedParseSession(
            _HostileEofCodec(
                self.descriptor(),
                (self.header(), self.segment("first")),
                self.segment("late"),
            ),
            snapshot,
            self.request(),
        )
        with self.assertRaises(ParserSessionError) as caught:
            tuple(session)
        self.assertEqual(caught.exception.code, "PARSER.SYNTAX.INVALID_EVENT")
        with self.assertRaises(ParserSessionError):
            session.verified_terminal()
        session.close()
        snapshot.close()

    def test_field_record_metadata_issue_allowlist_and_retention_limits(self) -> None:
        from parser_contracts import MetadataEntry
        from parser_source import GuardedParseSession, ParserSessionError

        profile = self.profile(max_records=1, max_materialized_records=1)
        cases = (
            (
                (self.header(), self.segment(source="x" * 65)),
                "PARSER.LIMIT.FIELD",
            ),
            (
                (self.header(), self.segment("one"), self.segment("two")),
                "PARSER.LIMIT.RECORD",
            ),
            (
                (
                    self.header(metadata=(MetadataEntry("key", "x" * 63),)),
                    self.segment(),
                ),
                "PARSER.LIMIT.METADATA",
            ),
            (
                (self.header(), self.issue("PARSER.UNKNOWN.CODE")),
                "PARSER.PLUGIN.ISSUE_UNDECLARED",
            ),
        )
        for events, code in cases:
            with self.subTest(code=code):
                snapshot = self.make_snapshot(profile)
                descriptor = self.descriptor(profile=profile)
                session = GuardedParseSession(
                    _ScriptedCodec(descriptor, events),
                    snapshot,
                    self.request(),
                )
                with self.assertRaises(ParserSessionError) as caught:
                    tuple(session)
                self.assertEqual(caught.exception.code, code)
                session.close()
                snapshot.close()

        many_warnings = tuple(self.issue() for _ in range(7))
        snapshot = self.make_snapshot()
        descriptor = self.descriptor()
        session = GuardedParseSession(
            _ScriptedCodec(descriptor, (self.header(), *many_warnings)),
            snapshot,
            self.request(),
        )
        tuple(session)
        terminal = session.verified_terminal()
        self.assertTrue(terminal.issues_truncated)
        self.assertEqual(session.issue_counts[0].count, 7)
        self.assertEqual(len(session.retained_issues), 4)
        session.close()
        snapshot.close()

    def test_early_close_consumer_exception_and_cancellation_abort_session(self) -> None:
        from parser_source import CancellationToken, GuardedParseSession, ParserSessionError

        snapshot = self.make_snapshot()
        session = GuardedParseSession(
            _ScriptedCodec(self.descriptor(), (self.header(), self.segment())),
            snapshot,
            self.request(),
        )
        iterator = iter(session)
        self.assertEqual(next(iterator), self.header())
        iterator.close()
        with self.assertRaises(ParserSessionError) as caught:
            session.verified_terminal()
        self.assertEqual(caught.exception.code, "PARSER.SESSION.ABORTED")
        session.close()
        snapshot.close()

        snapshot = self.make_snapshot()
        session = GuardedParseSession(
            _ScriptedCodec(self.descriptor(), (self.header(), self.segment())),
            snapshot,
            self.request(),
        )
        with self.assertRaises(RuntimeError):
            for _event in session:
                raise RuntimeError("consumer body must not enter Parser error")
        with self.assertRaises(ParserSessionError):
            session.verified_terminal()
        session.close()
        snapshot.close()

        token = CancellationToken()
        snapshot = self.make_snapshot()
        session = GuardedParseSession(
            _ScriptedCodec(self.descriptor(), (self.header(), self.segment())),
            snapshot,
            self.request(),
            cancellation=token,
        )
        iterator = iter(session)
        self.assertEqual(next(iterator), self.header())
        token.cancel()
        with self.assertRaises(ParserSessionError) as caught:
            next(iterator)
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.CANCELLED")
        with self.assertRaises(ParserSessionError):
            session.verified_terminal()
        session.close()
        snapshot.close()

    def test_validate_iterator_materialize_use_same_raw_grammar_and_order(self) -> None:
        from parser_contracts import ValidationOutcome
        from parser_source import create_sealed_snapshot, materialize, validate

        snapshot = create_sealed_snapshot(self.source_reference(), limit_profile=self.profile())
        events = (self.header(), self.segment("one"), self.issue(), self.segment("two"))
        codec = _ScriptedCodec(self.descriptor(), events)
        report = validate(codec, snapshot, self.request())
        result = materialize(codec, snapshot, self.request())
        self.assertIs(report.outcome, ValidationOutcome.SUCCESS)
        self.assertEqual(result.header, self.header())
        self.assertEqual(result.records, (self.segment("one"), self.segment("two")))
        self.assertEqual(result.issues, (self.issue(),))
        self.assertEqual(result.terminal.record_count, report.terminal.record_count)
        self.assertEqual(result.terminal.warning_counts, report.terminal.warning_counts)
        snapshot.close()

    def test_validation_maps_fatal_and_cancellation_without_terminal(self) -> None:
        from parser_contracts import ValidationOutcome
        from parser_source import CancellationToken, create_sealed_snapshot, validate

        profile = self.profile()
        descriptor = self.descriptor(profile=profile)
        fatal_codec = _ScriptedCodec(
            descriptor,
            (
                self.header(),
                self.segment(),
                self.issue("PARSER.SYNTAX.MALFORMED", fatal=True),
            ),
        )
        snapshot = create_sealed_snapshot(self.source_reference(), limit_profile=profile)
        failed = validate(fatal_codec, snapshot, self.request())
        self.assertIs(failed.outcome, ValidationOutcome.FAILED)
        self.assertIsNone(failed.terminal)
        self.assertEqual(failed.provisional_record_count, 1)
        self.assertEqual(failed.issue_counts[0].code, "PARSER.SYNTAX.MALFORMED")

        cancellation = CancellationToken()
        cancellation.cancel()
        cancelled = validate(
            _ScriptedCodec(descriptor, (self.header(), self.segment())),
            snapshot,
            self.request(),
            cancellation=cancellation,
        )
        self.assertIs(cancelled.outcome, ValidationOutcome.CANCELLED)
        self.assertIsNone(cancelled.terminal)
        self.assertEqual(cancelled.issue_counts[0].code, "PARSER.SOURCE.CANCELLED")
        snapshot.close()

    def test_materialize_limit_is_fatal_without_terminal_but_validation_succeeds(self) -> None:
        from parser_contracts import ValidationOutcome
        from parser_source import ParserSessionError, create_sealed_snapshot, materialize, validate

        profile = self.profile(max_records=3, max_materialized_records=1)
        descriptor = self.descriptor(profile=profile)
        events = (self.header(), self.segment("one"), self.segment("two"))
        codec = _ScriptedCodec(descriptor, events)
        snapshot = create_sealed_snapshot(self.source_reference(), limit_profile=profile)
        report = validate(codec, snapshot, self.request())
        self.assertIs(report.outcome, ValidationOutcome.SUCCESS)
        with self.assertRaises(ParserSessionError) as caught:
            materialize(codec, snapshot, self.request())
        self.assertEqual(caught.exception.code, "PARSER.LIMIT.MATERIALIZATION")
        snapshot.close()


@unittest.skipUnless(os.name == "posix", "rooted dirfd contract is POSIX-specific")
class AtomicWriterTests(_FixtureMixin, unittest.TestCase):
    def test_atomic_write_returns_digest_bound_receipt(self) -> None:
        from parser_source import atomic_write_bytes

        payload = b'{"schema_version":1}'
        receipt = atomic_write_bytes(self.target_reference(), payload)
        self.assertEqual(self.target.read_bytes(), payload)
        self.assertEqual(receipt.content_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(receipt.byte_count, len(payload))
        status = self.target.stat()
        self.assertEqual(
            receipt.regular_file_identity,
            f"{status.st_dev}:{status.st_ino}",
        )

    def test_writer_failures_before_replace_preserve_target_and_issue_no_receipt(self) -> None:
        from parser_source import ParserSourceError, atomic_write_bytes

        payload = b"new-target"
        cases = (
            ("_write_all", OSError("write failed"), "PARSER.SOURCE.WRITE_FAILED"),
            ("os.fsync", OSError("fsync failed"), "PARSER.SOURCE.WRITE_FAILED"),
            ("_validate_temp_payload", ValueError("invalid body"), "PARSER.SOURCE.WRITE_VALIDATION_FAILED"),
            ("os.replace", OSError("replace failed"), "PARSER.SOURCE.WRITE_FAILED"),
        )
        for patch_name, side_effect, expected_code in cases:
            with self.subTest(patch_name=patch_name):
                self.target.write_bytes(b"old-target")
                qualified = patch_name if "." in patch_name else f"parser_source.{patch_name}"
                if qualified == "os.fsync":
                    qualified = "parser_source.os.fsync"
                elif qualified == "os.replace":
                    qualified = "parser_source.os.replace"
                with mock.patch(qualified, side_effect=side_effect):
                    with self.assertRaises(ParserSourceError) as caught:
                        atomic_write_bytes(self.target_reference(), payload)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(self.target.read_bytes(), b"old-target")
                self.assertEqual(tuple((self.root / "output").glob(".parser-*.tmp")), ())

    def test_writer_rejects_escape_symlink_and_non_regular_target(self) -> None:
        from parser_contracts import TargetReference
        from parser_source import ParserSourceError, atomic_write_bytes

        outside = self.root.parent / "outside.json"
        outside.write_bytes(b"outside")
        link = self.root / "output" / "link.json"
        link.symlink_to(outside)
        cases = (
            TargetReference(str(self.root), str(outside), "outside"),
            TargetReference(str(self.root), str(link), "link"),
            TargetReference(str(self.root), str(self.root / "output"), "directory"),
        )
        for reference in cases:
            with self.subTest(reference=reference), self.assertRaises(ParserSourceError):
                atomic_write_bytes(reference, b"new")
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_wave0_fault_fixture_observes_no_authority_change_or_receipt(self) -> None:
        from parser_contracts import TargetReference
        from parser_source import ParserSourceError, atomic_write_bytes
        from tests.parser_io_test_support import ParserIOFaultFixture

        with ParserIOFaultFixture() as fixture:
            reference = TargetReference(
                safe_root=str(fixture.safe_root),
                selected_path=str(fixture.target),
                display_hint="project.json",
            )
            before = fixture.capture_authority_state()
            with mock.patch("parser_source.os.fsync", side_effect=OSError("fault")):
                with self.assertRaises(ParserSourceError):
                    atomic_write_bytes(reference, b"replacement")
            after = fixture.capture_authority_state()
            fixture.assert_failed_preserving_authority(before, after)


@unittest.skipUnless(os.name == "posix", "snapshot contract is POSIX-specific")
class ReviewerRemediationTests(_FixtureMixin, unittest.TestCase):
    @staticmethod
    def header():
        from parser_contracts import DocumentHeader

        return DocumentHeader("chapter", "en-US", "zh-CN", ())

    @staticmethod
    def segment(local_id="s-1"):
        from parser_contracts import ParsedSegment, RawSpeaker, TargetPresence

        return ParsedSegment(
            local_id,
            "source",
            None,
            TargetPresence.MISSING,
            None,
            RawSpeaker(""),
            (),
        )

    def test_session_accepts_snapshot_not_structural_fake_lease(self) -> None:
        from parser_source import GuardedParseSession, ParserSessionError

        codec = _ScriptedCodec(self.descriptor(), (self.header(), self.segment()))

        class FakeLease:
            closed = False
            consumption_proved = True

            def read(self, size=-1):
                del size
                return b""

        with self.assertRaises((TypeError, ParserSessionError)):
            GuardedParseSession(codec, FakeLease(), self.request())
        self.assertEqual(codec.iter_raw_calls, 0)

    def test_same_profile_identity_with_budget_drift_fails_before_raw_event(self) -> None:
        from dataclasses import replace

        from parser_source import (
            GuardedParseSession,
            ParserSessionError,
            create_sealed_snapshot,
        )

        snapshot_profile = self.profile(max_input_bytes=1024)
        active_profile = replace(snapshot_profile, max_input_bytes=1)
        codec = _ScriptedCodec(
            self.descriptor(profile=active_profile),
            (self.header(), self.segment()),
        )
        snapshot = create_sealed_snapshot(
            self.source_reference(),
            limit_profile=snapshot_profile,
        )
        with self.assertRaises(ParserSessionError) as caught:
            GuardedParseSession(codec, snapshot, self.request())
        self.assertIn(caught.exception.code, {"PARSER.LIMIT.INPUT", "PARSER.SOURCE.STALE"})
        self.assertEqual(codec.iter_raw_calls, 0)
        snapshot.close()

    def test_policy_derives_seekability_and_codec_cannot_mark_consumption(self) -> None:
        from parser_contracts import InputConsumptionPolicy
        from parser_source import (
            GuardedParseSession,
            ParserSessionError,
            ParserSourceError,
            create_sealed_snapshot,
        )

        sequential_snapshot = create_sealed_snapshot(
            self.source_reference(),
            limit_profile=self.profile(),
        )
        sequential_codec = _ScriptedCodec(
            self.descriptor(),
            (self.header(), self.segment()),
        )
        sequential_session = GuardedParseSession(
            sequential_codec,
            sequential_snapshot,
            self.request(),
        )
        self.assertFalse(sequential_session.source.seekable())
        self.assertFalse(hasattr(sequential_session.source, "seek"))
        self.assertFalse(hasattr(sequential_session.source, "mark_consumption_complete"))
        tuple(sequential_session)
        sequential_session.verified_terminal()
        sequential_session.close()
        sequential_snapshot.close()

        # A seekable policy is not a license for Foundation to scan arbitrary
        # bytes on the codec's behalf.  A plain reader without the structural
        # preflight behavior is rejected before iter_raw can run.
        xlsx_descriptor = self.descriptor(
            consumption_policy=InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET
        )
        plain_codec = _ScriptedCodec(
            xlsx_descriptor,
            (self.header(), self.segment()),
        )
        xlsx_snapshot = create_sealed_snapshot(
            self.source_reference(),
            limit_profile=self.profile(),
        )
        with self.assertRaises(ParserSessionError) as caught:
            GuardedParseSession(plain_codec, xlsx_snapshot, self.request())
        self.assertEqual(caught.exception.code, "PARSER.CAPABILITY.PREFLIGHT_UNSUPPORTED")
        self.assertEqual(plain_codec.iter_raw_calls, 0)
        xlsx_snapshot.close()

        # The positive case delegates the archive semantics to the XLSX support
        # boundary.  Foundation observes that the real preflight read the same
        # lease completely and restored it to offset zero; no self-signed token
        # or Foundation supplement is accepted.
        from parser_xlsx_support import XlsxPreflightError, preflight_xlsx
        from tests.test_parser_xlsx_support import TEST_LIMITS, _archive_bytes

        self.source.write_bytes(_archive_bytes())
        xlsx_profile = self.profile(max_input_bytes=1_000_000)
        xlsx_descriptor = self.descriptor(
            profile=xlsx_profile,
            consumption_policy=InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET,
        )
        xlsx_snapshot = create_sealed_snapshot(
            self.source_reference(),
            limit_profile=xlsx_profile,
        )
        observed = {}

        class SeekingCodec:
            descriptor = xlsx_descriptor

            def preflight_input(self, source, request):
                del request
                try:
                    preflight_xlsx(source, TEST_LIMITS)
                except XlsxPreflightError as exc:
                    raise ParserSourceError(
                        "PARSER.SOURCE.READ_FAILED",
                        "XLSX input did not pass the bounded archive preflight",
                    ) from exc

            def iter_raw(self, source, request):
                del request
                observed["seekable"] = source.seekable()
                observed["marker"] = hasattr(source, "mark_consumption_complete")
                observed["origin"] = source.tell()
                yield ReviewerRemediationTests.header()
                yield ReviewerRemediationTests.segment()

        xlsx_session = GuardedParseSession(
            SeekingCodec(),
            xlsx_snapshot,
            self.request(),
        )
        tuple(xlsx_session)
        xlsx_session.verified_terminal()
        self.assertTrue(observed["seekable"])
        self.assertFalse(observed["marker"])
        self.assertEqual(observed["origin"], 0)
        xlsx_session.close()
        xlsx_snapshot.close()

    def test_seekable_preflight_rejects_tokens_partial_coverage_and_hostile_errors(self) -> None:
        from parser_contracts import InputConsumptionPolicy
        from parser_source import GuardedParseSession, ParserSessionError, create_sealed_snapshot

        descriptor = self.descriptor(
            consumption_policy=InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET
        )

        class InvalidPreflightCodec:
            def __init__(self, behavior):
                self.descriptor = descriptor
                self.behavior = behavior
                self.iter_raw_calls = 0

            def preflight_input(self, source, request):
                del request
                return self.behavior(source)

            def iter_raw(self, source, request):
                del source, request
                self.iter_raw_calls += 1
                yield ReviewerRemediationTests.header()
                yield ReviewerRemediationTests.segment()

        def authority_token(source):
            source.read()
            source.seek(0)
            return True

        def partial_coverage(source):
            source.read(1)
            source.seek(0)
            return None

        def hostile_failure(source):
            del source
            raise RuntimeError("private workbook contents")

        for behavior in (authority_token, partial_coverage, hostile_failure):
            with self.subTest(behavior=behavior.__name__):
                snapshot = create_sealed_snapshot(
                    self.source_reference(),
                    limit_profile=self.profile(),
                )
                codec = InvalidPreflightCodec(behavior)
                session = GuardedParseSession(codec, snapshot, self.request())
                try:
                    with self.assertRaises(ParserSessionError) as caught:
                        tuple(session)
                    self.assertEqual(caught.exception.code, "PARSER.SOURCE.READ_FAILED")
                    self.assertNotIn("private workbook contents", str(caught.exception))
                    self.assertEqual(codec.iter_raw_calls, 0)
                    with self.assertRaises(ParserSessionError):
                        session.verified_terminal()
                finally:
                    session.close()
                    snapshot.close()

    def test_close_and_abort_are_irreversible_for_suspended_iterator(self) -> None:
        from parser_source import GuardedParseSession, ParserSessionError, create_sealed_snapshot

        for action in ("close", "abort"):
            with self.subTest(action=action):
                snapshot = create_sealed_snapshot(
                    self.source_reference(),
                    limit_profile=self.profile(),
                )
                session = GuardedParseSession(
                    _ScriptedCodec(
                        self.descriptor(),
                        (self.header(), self.segment("one"), self.segment("two")),
                    ),
                    snapshot,
                    self.request(),
                )
                iterator = iter(session)
                self.assertEqual(next(iterator), self.header())
                if action == "close":
                    session.close()
                else:
                    session.abort(
                        "PARSER.SYNTAX.MALFORMED",
                        "Foundation test abort",
                    )
                with self.assertRaises(ParserSessionError):
                    next(iterator)
                with self.assertRaises(ParserSessionError):
                    session.verified_terminal()
                snapshot.close()

    def test_retention_full_still_preserves_primary_duplicate_fatal(self) -> None:
        from dataclasses import replace

        from parser_source import GuardedParseSession, ParserSessionError, create_sealed_snapshot

        profile = replace(self.profile(), max_retained_issues=1)
        warnings = tuple(
            GuardedSessionTests.issue("PARSER.TEST.WARNING") for _ in range(3)
        )
        codec = _ScriptedCodec(
            self.descriptor(profile=profile),
            (
                self.header(),
                *warnings,
                self.segment("duplicate"),
                self.segment("duplicate"),
            ),
        )
        snapshot = create_sealed_snapshot(
            self.source_reference(),
            limit_profile=profile,
        )
        session = GuardedParseSession(codec, snapshot, self.request())
        with self.assertRaises(ParserSessionError) as caught:
            tuple(session)
        self.assertEqual(caught.exception.code, "PARSER.SYNTAX.DUPLICATE_LOCAL_ID")
        self.assertEqual(session.primary_fatal.code, "PARSER.SYNTAX.DUPLICATE_LOCAL_ID")
        self.assertIn(
            "PARSER.SYNTAX.DUPLICATE_LOCAL_ID",
            {issue.code for issue in session.retained_issues},
        )
        count_by_code = {item.code: item.count for item in session.issue_counts}
        self.assertEqual(count_by_code["PARSER.SYNTAX.DUPLICATE_LOCAL_ID"], 1)
        session.close()
        snapshot.close()

        from parser_contracts import ValidationOutcome
        from parser_source import validate

        validation_codec = _ScriptedCodec(
            self.descriptor(profile=profile),
            (
                self.header(),
                *warnings,
                self.segment("duplicate"),
                self.segment("duplicate"),
            ),
        )
        validation_snapshot = create_sealed_snapshot(
            self.source_reference(),
            limit_profile=profile,
        )
        report = validate(validation_codec, validation_snapshot, self.request())
        self.assertIs(report.outcome, ValidationOutcome.FAILED)
        self.assertIsNone(report.terminal)
        self.assertIn(
            "PARSER.SYNTAX.DUPLICATE_LOCAL_ID",
            {issue.code for issue in report.issues},
        )
        validation_snapshot.close()

    def test_missing_mandatory_foundation_issue_code_fails_before_raw_grammar(self) -> None:
        from dataclasses import replace

        from parser_source import GuardedParseSession, ParserSessionError, create_sealed_snapshot

        profile = self.profile()
        incomplete = replace(
            profile,
            declared_issue_codes=tuple(
                code
                for code in profile.declared_issue_codes
                if code != "PARSER.SYNTAX.DUPLICATE_LOCAL_ID"
            ),
        )
        codec = _ScriptedCodec(
            self.descriptor(profile=incomplete),
            (self.header(), self.segment()),
        )
        snapshot = create_sealed_snapshot(
            self.source_reference(),
            limit_profile=incomplete,
        )
        with self.assertRaises(ParserSessionError) as caught:
            GuardedParseSession(codec, snapshot, self.request())
        self.assertEqual(caught.exception.code, "PARSER.CAPABILITY.DESCRIPTOR_INVALID")
        self.assertEqual(codec.iter_raw_calls, 0)
        snapshot.close()

    def test_view_capabilities_fail_before_calling_raw_grammar(self) -> None:
        from dataclasses import replace

        from parser_contracts import CodecCapabilities
        from parser_source import (
            GuardedParseSession,
            ParserSessionError,
            create_sealed_snapshot,
            materialize,
            validate,
        )

        profile = self.profile()
        base = self.descriptor(profile=profile)
        cases = (
            ("validation", replace(base.capabilities, validatable=False)),
            ("materialized", replace(base.capabilities, materialized_view=False)),
            ("iterator", replace(base.capabilities, iterator_view=False)),
        )
        for view, capabilities in cases:
            with self.subTest(view=view):
                descriptor = replace(base, capabilities=capabilities)
                codec = _ScriptedCodec(descriptor, (self.header(), self.segment()))
                snapshot = create_sealed_snapshot(
                    self.source_reference(),
                    limit_profile=profile,
                )
                with self.assertRaises(ParserSessionError):
                    if view == "validation":
                        validate(codec, snapshot, self.request())
                    elif view == "materialized":
                        materialize(codec, snapshot, self.request())
                    else:
                        GuardedParseSession(codec, snapshot, self.request())
                self.assertEqual(codec.iter_raw_calls, 0)
                snapshot.close()

    def test_post_replace_proof_failure_is_fail_closed_without_claiming_rollback(self) -> None:
        from parser_source import ParserSourceError, atomic_write_bytes

        with mock.patch(
            "parser_source._prove_replaced_target",
            side_effect=ParserSourceError(
                "PARSER.SOURCE.WRITE_PROOF_FAILED",
                "actual replaced target proof failed",
            ),
        ):
            with self.assertRaises(ParserSourceError) as caught:
                atomic_write_bytes(self.target_reference(), b"new-target")
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.WRITE_PROOF_FAILED")
        # Replace already happened.  The contract is fail-closed/no receipt, not a
        # fabricated promise that external races or post-replace proof can roll back.
        self.assertEqual(self.target.read_bytes(), b"new-target")

    def test_zero_short_write_and_short_readback_fail_before_replace(self) -> None:
        from parser_source import ParserSourceError, atomic_write_bytes

        with mock.patch("parser_source.os.write", return_value=0):
            with self.assertRaises(ParserSourceError) as caught:
                atomic_write_bytes(self.target_reference(), b"new-target")
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.WRITE_FAILED")
        self.assertEqual(self.target.read_bytes(), b"old-target")

        with mock.patch("parser_source.os.pread", return_value=b""):
            with self.assertRaises(ParserSourceError) as caught:
                atomic_write_bytes(self.target_reference(), b"new-target")
        self.assertEqual(caught.exception.code, "PARSER.SOURCE.WRITE_VALIDATION_FAILED")
        self.assertEqual(self.target.read_bytes(), b"old-target")


if __name__ == "__main__":
    unittest.main()
