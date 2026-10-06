from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import parser_composition as composition
import parser_contracts as contracts
from parser_source import CancellationToken
from tests.test_parser_composition import _Provider
from tests.test_parser_registry import _descriptor


class NeutralFixture:
    def __init__(self):
        self.calls = []
        self.consume = True
        self.reader_consume = True
        self.failure = None
        self.during = lambda request: None
        self.payload = b'\x00opaque\xff mapping\r\n'
        self.last_request = None
        self.reader_issues = ()
        base = _descriptor('source-state', provider_id='test.handoff', extensions=('.neutral',))
        self.descriptor = replace(
            base,
            limit_profile=replace(base.limit_profile, declared_issue_codes=tuple(sorted(
                set(base.declared_issue_codes) | set(contracts.FOUNDATION_GUARDED_ISSUE_CODES)
            ))),
            reader_factory=lambda: NeutralCodec(self),
            source_state_factory=lambda: NeutralCodec(self),
            source_state_limits=contracts.SourceStateLimits(64),
        )

    def surface(self):
        return composition.create_parser_application_surface(providers=(composition.ProviderBinding(
            'test.handoff', _Provider('test.handoff', '1', (self.descriptor,)), True, ('1',),
        ),))


class NeutralCodec:
    def __init__(self, fixture):
        self.fixture = fixture
        self.descriptor = fixture.descriptor

    def iter_raw(self, source, request):
        self.fixture.calls.append('read')
        if self.fixture.reader_consume:
            source.read()
        yield contracts.DocumentHeader('neutral fixture', 'en', 'zh', ())
        yield from self.fixture.reader_issues
        for local_id in ('slot-b', 'slot-a'):
            yield contracts.ParsedSegment(
                local_id, 'same source', 'old target' if local_id == 'slot-b' else '',
                contracts.TargetPresence.PRESENT if local_id == 'slot-b' else contracts.TargetPresence.EXPLICIT_EMPTY,
                contracts.TranslationState.UNCONFIRMED,
                contracts.RawSpeaker('guide'), (),
            )

    def prepare_source_state(self, request):
        fixture = self.fixture
        fixture.calls.append('state')
        fixture.last_request = request
        if fixture.consume:
            request.source.read()
        fixture.during(request)
        if fixture.failure:
            raise fixture.failure
        return contracts.OpaqueSourceState(
            self.descriptor.identity, self.descriptor.format_id, 'neutral-state-v1', fixture.payload,
        )


class ParserSourceHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / 'source.neutral'
        self.bytes = b'original opaque source\r\n'
        self.path.write_bytes(self.bytes)

    def opened(self, surface, descriptor, cancellation=None):
        opened = surface.open_input(
            contracts.SourceReference(str(self.root), str(self.path), 'fixture'),
            contracts.SelectionRequest(contracts.EffectivePurpose.PROJECT_DOCUMENT, descriptor.format_id),
            contracts.ReadRequest(contracts.EffectivePurpose.PROJECT_DOCUMENT, descriptor.format_id),
            cancellation=cancellation,
        )
        self.addCleanup(opened.close)
        return opened

    def test_builtin_without_private_factory_handoff(self):
        surface = composition.create_parser_application_surface()
        descriptor = surface.select(contracts.SelectionRequest(
            contracts.EffectivePurpose.PROJECT_DOCUMENT, contracts.LINE_TEXT_V1,
        ))
        handoff = surface.materialize_handoff(self.opened(surface, descriptor))
        self.assertEqual(handoff.source_bytes, self.bytes)
        self.assertIsNone(handoff.private_state)
        self.assertEqual(handoff.materialized.terminal.source.content_sha256,
                         hashlib.sha256(self.bytes).hexdigest())

    def test_verified_same_snapshot_once_and_lease_expires(self):
        fixture = NeutralFixture()
        surface = fixture.surface()
        opened = self.opened(surface, fixture.descriptor)
        # The path no longer supplies the handoff bytes: the same sealed source does.
        self.path.write_bytes(b'later source')
        handoff = surface.materialize_handoff(opened)
        self.assertEqual(handoff.source_bytes, self.bytes)
        self.assertEqual(handoff.private_state.payload, fixture.payload)
        self.assertEqual(fixture.calls, ['read', 'state'])
        self.assertEqual(tuple(r.local_id for r in handoff.materialized.records), ('slot-b', 'slot-a'))
        self.assertEqual(fixture.last_request.terminal, handoff.materialized.terminal)
        self.assertEqual(fixture.last_request.source.source_identity, opened.source_identity)
        self.assertTrue(fixture.last_request.source.closed)
        with self.assertRaises(contracts.ContractViolation):
            surface.materialize_handoff(opened)

    def test_foreign_closed_cancelled_and_partial_inputs_never_issue(self):
        for fault in ('foreign', 'closed', 'cancelled', 'partial'):
            with self.subTest(fault=fault):
                fixture = NeutralFixture()
                surface = fixture.surface()
                cancellation = CancellationToken()
                opened = self.opened(surface, fixture.descriptor, cancellation)
                if fault == 'foreign':
                    surface = fixture.surface()
                elif fault == 'closed':
                    opened.close()
                elif fault == 'cancelled':
                    cancellation.cancel()
                else:
                    fixture.reader_consume = False
                with self.assertRaises(contracts.ContractViolation):
                    surface.materialize_handoff(opened)
                self.assertNotIn('state', fixture.calls)

    def test_private_failure_partial_close_cancel_and_limit_never_issue(self):
        for fault in ('partial', 'closed', 'cancel', 'failure', 'limit', 'opened-close'):
            with self.subTest(fault=fault):
                fixture = NeutralFixture()
                surface = fixture.surface()
                cancellation = CancellationToken()
                opened = self.opened(surface, fixture.descriptor, cancellation)
                if fault == 'partial': fixture.consume = False
                if fault == 'closed': fixture.during = lambda r: r.source.close()
                if fault == 'cancel': fixture.during = lambda r: cancellation.cancel()
                if fault == 'opened-close': fixture.during = lambda r: opened.close()
                if fault == 'failure': fixture.failure = RuntimeError('secret private content')
                if fault == 'limit': fixture.payload = b'x' * 65
                with self.assertRaises(contracts.ContractViolation) as rejected:
                    surface.materialize_handoff(opened)
                self.assertNotIn('secret', str(rejected.exception))
                self.assertTrue(fixture.last_request.source.closed)

    def test_private_factory_exact_descriptor_protocol_and_result(self):
        for fault in ('descriptor', 'method', 'result', 'identity'):
            with self.subTest(fault=fault):
                fixture = NeutralFixture()
                def factory():
                    codec = NeutralCodec(fixture)
                    if fault == 'descriptor': codec.descriptor = replace(fixture.descriptor)
                    if fault == 'method': codec.prepare_source_state = None
                    if fault == 'result': codec.prepare_source_state = lambda r: object()
                    if fault == 'identity':
                        original = codec.prepare_source_state
                        codec.prepare_source_state = lambda r: replace(original(r), codec_identity=contracts.CodecIdentity('other', 'codec', '1'))
                    return codec
                fixture.descriptor = replace(fixture.descriptor, source_state_factory=factory)
                surface = fixture.surface()
                with self.assertRaises(contracts.ContractViolation):
                    surface.materialize_handoff(self.opened(surface, fixture.descriptor))

    def test_source_state_factory_requires_verification_and_explicit_bounds(self):
        fixture = NeutralFixture()
        for changes in (
            {'source_state_limits': None}, {'source_state_factory': 1},
            {'capabilities': replace(fixture.descriptor.capabilities, validatable=False)},
        ):
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                replace(fixture.descriptor, **changes)

    def test_fatal_diagnostic_keeps_positions_but_discards_provider_body(self):
        fixture = NeutralFixture()
        fixture.reader_issues = (contracts.ParseIssue(
            'PARSER.SYNTAX.MALFORMED', contracts.IssueSeverity.FATAL, 'secret source text',
            line_number=7, byte_offset=23, record_number=2,
        ),)
        surface = fixture.surface()
        with self.assertRaises(composition.SourceHandoffError) as caught:
            surface.materialize_handoff(self.opened(surface, fixture.descriptor))
        issue = caught.exception.diagnostics[0]
        self.assertEqual((issue.line_number, issue.byte_offset, issue.record_number), (7, 23, 2))
        self.assertEqual(issue.code, 'PARSER.SYNTAX.MALFORMED')
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('secret', issue.safe_summary)
        self.assertNotIn('state', fixture.calls)

    def test_concurrent_handoff_issues_once(self):
        fixture = NeutralFixture()
        surface = fixture.surface()
        opened = self.opened(surface, fixture.descriptor)
        def attempt():
            try:
                return surface.materialize_handoff(opened)
            except contracts.ContractViolation:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: attempt(), range(2)))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(fixture.calls, ['read', 'state'])

    def test_sealed_byte_drift_cannot_issue_after_private_provider(self):
        fixture = NeutralFixture()
        surface = fixture.surface()
        opened = self.opened(surface, fixture.descriptor)
        original_read = opened._snapshot._read_at
        fixture.during = lambda request: mock.patch.object(
            type(opened._snapshot), '_read_at', lambda self, offset, size: b'x' * len(original_read(offset, size)),
        ).start()
        self.addCleanup(mock.patch.stopall)
        with self.assertRaises(contracts.ContractViolation):
            surface.materialize_handoff(opened)
        self.assertTrue(fixture.last_request.source.closed)

    def test_close_from_another_thread_during_private_work_cannot_issue(self):
        fixture = NeutralFixture()
        surface = fixture.surface()
        opened = self.opened(surface, fixture.descriptor)
        with ThreadPoolExecutor(max_workers=1) as executor:
            fixture.during = lambda request: executor.submit(opened.close).result(timeout=5)
            with self.assertRaises(contracts.ContractViolation):
                surface.materialize_handoff(opened)
        self.assertTrue(fixture.last_request.source.closed)


if __name__ == '__main__':
    unittest.main()
