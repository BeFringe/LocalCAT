"""Core 10.6a：真实未命名取消、家族停止和终态重放。"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import hashlib
from pathlib import Path
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from platform_fs_contracts import CandidateFile, PendingPublication, MutableFileReservation, BoundRegularFile
import tm_activation_journal as journal
import tm_activation_recovery as recovery
import tm_migration as migration
import tm_sqlite_store as store
from tm_contracts import CanonicalResourceIdentity, MigrationFailure, MigrationReport
from tm_engine import open_canonical_tm_store


@unittest.skipIf(sys.platform == 'win32', 'POSIX 未命名取消')
class PosixUnpublishedCancellationTests(unittest.TestCase):
    @contextmanager
    def resource(self, replacement):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory).resolve() / 'memory.jsonl'
            source.write_text('{"source":"hello","target":"prior"}\n')
            identity = CanonicalResourceIdentity.from_configured_jsonl('tm.cancel', source)
            owner = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id='store.cancel')
            service = migration.TMMigrationService(resource_identity=identity, canonical_store_id='store.cancel', coordinator=owner)
            peer = None
            if replacement:
                self.assertIs(type(service.activate_initial(source, identity.resource_id)), MigrationReport)
                peer = open_canonical_tm_store(source)
                self.assertIsNotNone(peer)
                self.assertIs(peer.coordinator._family, owner._family)
            try:
                yield source, identity, owner, service, peer
            finally:
                if peer is not None:
                    peer.coordinator.close_family_session()
                owner.close_family_session()

    def assets(self, source, identity):
        return tuple(hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
                     for p in (source, identity.canonical_sidecar_path, identity.snapshot_manifest_path))

    @contextmanager
    def fault(self, owner, identity, window):
        root = owner._family._root
        create, begin = type(root).create_candidate, type(root).begin_publish
        write, flush, close = CandidateFile.write_all, CandidateFile.flush_content, PendingPublication.close
        candidates, pendings, hits, destinations = [], [], [], []
        def observe_create(authority, name, *args, **kwargs):
            result = create(authority, name, *args, **kwargs)
            if str(name) == journal._activation_journal_temp_path(journal._activation_journal_path(identity)).name:
                self.assertIs(authority, root)
                candidates.append(result)
            return result
        def observe_begin(authority, candidate, destination, *args, **kwargs):
            result = begin(authority, candidate, destination, *args, **kwargs)
            if authority is root:
                destinations.append(str(destination))
                if str(destination) == journal._activation_journal_path(identity).name:
                    pendings.append(result)
            return result
        def fail_write(candidate, payload):
            result = write(candidate, payload)
            if window == 'write' and candidate in candidates and not hits:
                hits.append(window)
                raise OSError('primary journal write')
            return result
        def fail_flush(candidate):
            result = flush(candidate)
            if window == 'flush' and candidate in candidates and not hits:
                hits.append(window)
                raise OSError('primary journal flush')
            return result
        def fail_close(pending):
            result = close(pending)
            if window == 'close' and pending in pendings and not hits:
                hits.append(window)
                raise OSError('primary journal close')
            return result
        with ExitStack() as stack:
            for cls, name, replacement in ((type(root), 'create_candidate', observe_create),
                    (type(root), 'begin_publish', observe_begin), (CandidateFile, 'write_all', fail_write),
                    (CandidateFile, 'flush_content', fail_flush), (PendingPublication, 'close', fail_close)):
                stack.enter_context(patch.object(cls, name, replacement))
            yield hits, destinations
        self.assertEqual(hits, [window])
        self.assertNotIn(identity.canonical_sidecar_path.name, destinations)

    def invoke(self, replacement, source, identity, service):
        return (service.import_snapshot if replacement else service.activate_initial)(source, identity.resource_id)

    def initial_recovery_error(self, source, identity, service):
        errors = []
        operation = service._activate_initial_with_resource_reservation
        def observe_error(*args, **kwargs):
            try:
                return operation(*args, **kwargs)
            except BaseException as error:
                errors.append(error)
                raise
        with patch.object(service, '_activate_initial_with_resource_reservation', observe_error):
            outcome = self.invoke(False, source, identity, service)
        self.assertIs(type(outcome), MigrationFailure)
        self.assertEqual(outcome.stage, 'RECOVERY')
        self.assertEqual(outcome.error_code, 'MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE')
        self.assertFalse(outcome.retryable)
        self.assertIsNone(outcome.active_generation)
        self.assertFalse(outcome.canonical_authority_published)
        self.assertTrue(outcome.canonical_authority_ambiguous)
        self.assertEqual(outcome.recovery_locators, ())
        self.assertEqual(len(errors), 1)
        self.assertIs(type(errors[0]), migration.MigrationPreflightError)
        self.assertEqual(errors[0].error_code, 'MIGRATION.INITIAL_RECOVERY_REQUIRED')
        return errors[0]

    def test_actual_journal_failure_restores_prior_or_legacy(self):
        for replacement in (False, True):
            for window in ('write', 'flush', 'close'):
                with self.subTest(replacement=replacement, window=window), self.resource(replacement) as data:
                    source, identity, owner, service, peer = data
                    before = self.assets(source, identity)
                    root = owner._family._root
                    with self.fault(owner, identity, window):
                        result = self.invoke(replacement, source, identity, service)
                    self.assertIs(type(result), MigrationFailure)
                    self.assertNotEqual(result.stage, 'RECOVERY')
                    self.assertEqual(self.assets(source, identity), before)
                    self.assertIs(owner._family._root, root)
                    root.reprove_location()
                    self.assertEqual(owner.state, 'READY')
                    self.assertFalse(owner._family._recovery_required)
                    self.assertEqual(owner._family._active_count, 0)
                    self.assertEqual(owner._family._operation_tokens, {})
                    self.assertIsNone(owner._preparation)
                    self.assertFalse(journal._activation_journal_path(identity).exists())
                    self.assertFalse(journal._activation_journal_temp_path(journal._activation_journal_path(identity)).exists())
                    self.assertTrue(journal._activation_terminal_path(identity).exists())
                    for _ in range(2):
                        fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
                        try:
                            fresh.recover_durable_activation()
                            self.assertEqual(fresh.current_generation, 0 if replacement else None)
                        finally:
                            fresh.close_family_session()
                    if peer is not None:
                        self.assertEqual(peer.exact_records('hello')[0].target_raw, 'prior')
                        self.assertEqual(store.SQLiteTMStore.from_coordinator(owner).exact_records('hello')[0].target_raw, 'prior')
                    else:
                        self.assertIsNone(open_canonical_tm_store(source))

    def test_cancel_cleanup_failure_stops_peer_until_replay(self):
        with self.resource(True) as data:
            source, identity, owner, service, peer = data
            with self.fault(owner, identity, 'close'), patch.object(recovery, '_retire_cancelled_candidate_assets', side_effect=OSError('secondary cleanup')):
                with self.assertRaises(migration.MigrationPreflightError) as raised:
                    self.invoke(True, source, identity, service)
            self.assertIn('primary journal close', repr(raised.exception.__cause__) + repr(getattr(raised.exception.__cause__, '__cause__', None)))
            self.assertTrue(owner._family._recovery_required)
            with self.assertRaises(store.SQLiteStoreLifecycleError):
                peer.exact_records('hello')
            fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
            try:
                fresh.recover_durable_activation()
                self.assertFalse(owner._family._recovery_required)
                self.assertEqual(peer.exact_records('hello')[0].target_raw, 'prior')
                self.assertEqual(store.SQLiteTMStore.from_coordinator(owner).exact_records('hello')[0].target_raw, 'prior')
                self.assertIsNone(owner._preparation)
            finally:
                fresh.close_family_session()

    @contextmanager
    def secondary_terminal_fault(self, owner, identity, window, primary_hits):
        root = owner._family._root
        create, begin = type(root).create_candidate, type(root).begin_publish
        write, flush, close = CandidateFile.write_all, CandidateFile.flush_content, PendingPublication.close
        candidates, pendings, hits = [], [], []
        terminal = journal._activation_terminal_path(identity)
        def created(authority, name, *args, **kwargs):
            value = create(authority, name, *args, **kwargs)
            if authority is root and str(name) == journal._activation_terminal_temp_path(terminal).name:
                candidates.append(value)
            return value
        def published(authority, candidate, name, *args, **kwargs):
            value = begin(authority, candidate, name, *args, **kwargs)
            if authority is root and str(name) == terminal.name:
                pendings.append(value)
            return value
        def inject(value, actual, kind, *args):
            result = actual(value, *args)
            if primary_hits and kind == window and value in (pendings if kind == 'close' else candidates) and not hits:
                hits.append(kind)
                raise OSError('secondary terminal ' + kind)
            return result
        with ExitStack() as stack:
            stack.enter_context(patch.object(type(root), 'create_candidate', created))
            stack.enter_context(patch.object(type(root), 'begin_publish', published))
            stack.enter_context(patch.object(CandidateFile, 'write_all', lambda obj, data: inject(obj, write, 'write', data)))
            stack.enter_context(patch.object(CandidateFile, 'flush_content', lambda obj: inject(obj, flush, 'flush')))
            stack.enter_context(patch.object(PendingPublication, 'close', lambda obj: inject(obj, close, 'close')))
            yield hits
        self.assertEqual(hits, [window])

    def assert_stopped(self, source, owner, peer, *, factory_error='TM.CANONICAL_UNHEALTHY'):
        self.assertTrue(owner._family._recovery_required)
        self.assertEqual(owner._family._active_count, 0)
        self.assertEqual(owner._family._operation_tokens, {})
        with self.assertRaises(store.SQLiteStoreLifecycleError):
            store.SQLiteTMStore.from_coordinator(owner).exact_records('hello')
        if peer is not None:
            with self.assertRaises(store.SQLiteStoreLifecycleError):
                peer.exact_records('hello')
        with self.assertRaisesRegex(ValueError, '^' + factory_error.replace('.', r'\.') + '$'):
            open_canonical_tm_store(source)

    def test_secondary_terminal_failures_preserve_primary_and_recover_every_owner(self):
        for replacement in (False, True):
            for window in ('write', 'flush', 'close'):
                with self.subTest(replacement=replacement, window=window), self.resource(replacement) as data:
                    source, identity, owner, service, peer = data
                    before = self.assets(source, identity)
                    with self.fault(owner, identity, 'close') as (primary_hits, _), self.secondary_terminal_fault(owner, identity, window, primary_hits):
                        if replacement:
                            with self.assertRaises(migration.MigrationPreflightError) as raised:
                                self.invoke(replacement, source, identity, service)
                            self.assertIn('primary journal close', repr(raised.exception.__cause__.__cause__))
                        else:
                            result = self.invoke(replacement, source, identity, service)
                            self.assertEqual(result.stage, 'RECOVERY')
                    self.assert_stopped(source, owner, peer)
                    fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
                    try:
                        for _ in range(2):
                            fresh.recover_durable_activation()
                        self.assertFalse(owner._family._recovery_required)
                        self.assertEqual(self.assets(source, identity), before)
                        self.assertEqual(owner.state, 'READY')
                        self.assertIsNone(owner._preparation)
                        self.assertFalse(owner._initial_activation_authority_is_unavailable())
                        if peer is not None:
                            self.assertEqual(peer.exact_records('hello')[0].target_raw, 'prior')
                            self.assertEqual(store.SQLiteTMStore.from_coordinator(owner).exact_records('hello')[0].target_raw, 'prior')
                        else:
                            self.assertIsNone(open_canonical_tm_store(source))
                    finally:
                        fresh.close_family_session()

    def test_final_source_and_stage_close_failure_stops_family_inside_outer_except(self):
        for replacement in (False, True):
            for authority in ('source', 'stage', 'both'):
                for window in ('before', 'after'):
                    with self.subTest(replacement=replacement, authority=authority, window=window), self.resource(replacement) as data:
                        source, identity, owner, service, peer = data
                        before = self.assets(source, identity)
                        targets = ('source', 'stage') if authority == 'both' else (authority,)
                        hits, errors = {target: [] for target in targets}, []
                        def close_fault(target, actual):
                            def closing(value):
                                if primary_hits and not hits[target]:
                                    hits[target].append(value)
                                    if window == 'after':
                                        actual(value)
                                    raise OSError('secondary final ' + target + ' close ' + window)
                                return actual(value)
                            return closing
                        def install_closes(stack):
                            for target in targets:
                                cls = migration._PortableRootedRead if target == 'source' else MutableFileReservation
                                stack.enter_context(patch.object(cls, 'close', close_fault(target, cls.close)))
                        try:
                            raise OSError('unrelated caller exception')
                        except OSError:
                            with self.fault(owner, identity, 'close') as (primary_hits, _), ExitStack() as stack:
                                install_closes(stack)
                                if not replacement and authority == 'stage':
                                    errors.append(self.initial_recovery_error(source, identity, service))
                                else:
                                    try:
                                        self.invoke(replacement, source, identity, service)
                                    except BaseException as error:
                                        errors.append(error)
                        self.assertEqual({target: len(values) for target, values in hits.items()},
                                         {target: 1 for target in targets})
                        self.assert_stopped(source, owner, peer)
                        self.assertEqual(self.assets(source, identity), before)
                        self.assertEqual(len(errors), 1)
                        chain = errors[0]
                        messages = []
                        while chain is not None:
                            messages.append(repr(chain))
                            messages.extend(getattr(chain, '__notes__', ()))
                            chain = chain.__cause__
                        self.assertIn('primary journal close', ' '.join(messages))
                        for target in targets:
                            self.assertIn('secondary final ' + target + ' close ' + window, ' '.join(messages))
                        # A separate family remains usable while this family stops.
                        with self.resource(True) as healthy:
                            self.assertEqual(healthy[-1].exact_records('hello')[0].target_raw, 'prior')
                        if 'stage' in targets:
                            self.assertTrue(hits['stage'][0].closed)
                        owner.recover_durable_activation()
                        self.assertFalse(owner._family._recovery_required)
                        reopened = open_canonical_tm_store(source)
                        if replacement:
                            self.assertEqual(reopened.exact_records('hello')[0].target_raw, 'prior')
                            reopened.coordinator.close_family_session()
                        else:
                            self.assertIsNone(reopened)

    def test_source_wrapper_and_authority_close_keep_both_failure_facts(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement), self.resource(replacement) as data:
                source, identity, owner, service, peer = data
                retained, wrapper_hits, bound_hits = [], [], []
                source_close, bound_close = migration._PortableRootedRead.close, BoundRegularFile.close
                def source_closing(value):
                    if primary_hits:
                        retained.append(value.authority)
                        wrapper_hits.append(value)
                        raise OSError('source wrapper failed before delegation')
                    return source_close(value)
                def bound_closing(value):
                    if any(authority is value for authority in retained):
                        bound_hits.append(value)
                        raise OSError('underlying source authority close failed')
                    return bound_close(value)
                with self.fault(owner, identity, 'close') as (primary_hits, _), patch.object(migration._PortableRootedRead, 'close', source_closing), patch.object(BoundRegularFile, 'close', bound_closing):
                    with self.assertRaises(BaseException) as raised:
                        self.invoke(replacement, source, identity, service)
                self.assertEqual(len(wrapper_hits), 1)
                self.assertEqual(len(bound_hits), 1)
                facts, error = [], raised.exception
                while error is not None:
                    facts.extend([repr(error), *getattr(error, '__notes__', ())])
                    error = error.__cause__
                self.assertIn('primary journal close', ' '.join(facts))
                self.assertIn('source wrapper failed before delegation', ' '.join(facts))
                self.assertIn('underlying source authority close failed', ' '.join(facts))
                self.assertFalse(retained[0].closed)
                self.assert_stopped(source, owner, peer)
                owner.recover_durable_activation()
                self.assertTrue(retained[0].closed)
                self.assertFalse(owner._family._recovery_required)

    def test_persistent_owned_close_retains_authority_until_explicit_recovery(self):
        for replacement in (False, True):
            for selection in ('source', 'stage', 'both'):
                with self.subTest(replacement=replacement, selection=selection), self.resource(replacement) as data:
                    source, identity, owner, service, peer = data
                    before = self.assets(source, identity)
                    targets = ('source', 'stage') if selection == 'both' else (selection,)
                    retained, hits = {}, {target: [] for target in targets}
                    source_close, bound_close, stage_close = migration._PortableRootedRead.close, BoundRegularFile.close, MutableFileReservation.close
                    def source_closing(value):
                        if primary_hits and 'source' in targets and 'source' not in retained:
                            retained['source'] = value.authority
                        return source_close(value)
                    def bound_closing(value):
                        if retained.get('source') is value:
                            hits['source'].append(value)
                            raise OSError('persistent source close before')
                        return bound_close(value)
                    def stage_closing(value):
                        if primary_hits and 'stage' in targets and ('stage' not in retained or retained['stage'] is value):
                            retained.setdefault('stage', value)
                            hits['stage'].append(value)
                            raise OSError('persistent stage close before')
                        return stage_close(value)
                    with self.resource(True) as healthy, self.fault(owner, identity, 'close') as (primary_hits, _), patch.object(migration._PortableRootedRead, 'close', source_closing), patch.object(BoundRegularFile, 'close', bound_closing), patch.object(MutableFileReservation, 'close', stage_closing):
                        if not replacement and selection == 'stage':
                            error = self.initial_recovery_error(source, identity, service)
                        else:
                            with self.assertRaises(BaseException) as raised:
                                self.invoke(replacement, source, identity, service)
                            error = raised.exception
                        facts = []
                        while error is not None:
                            facts.extend([repr(error), *getattr(error, '__notes__', ())])
                            error = error.__cause__
                        for target in targets:
                            self.assertGreaterEqual(len(hits[target]), 2)
                            self.assertFalse(retained[target].closed)
                            self.assertIn('persistent ' + target + ' close before', ' '.join(facts))
                        self.assert_stopped(source, owner, peer)
                        self.assertEqual(healthy[-1].exact_records('hello')[0].target_raw, 'prior')
                        for _ in range(2):
                            calls_before = {target: len(hits[target]) for target in targets}
                            backend, original_root = owner._family._backend, owner._family._root
                            acquire, acquisitions = type(backend).acquire, []
                            def acquired(current, root, *args, **kwargs):
                                self.assertIs(root, original_root)
                                acquisitions.append(root)
                                return acquire(current, root, *args, **kwargs)
                            with patch.object(type(backend), 'acquire', acquired), patch.object(type(backend), 'bind_root', side_effect=AssertionError('fresh bind before owned close')), patch.object(store, 'recover_durable_activation', side_effect=AssertionError('raw recovery before owned close')):
                                with self.assertRaisesRegex(journal.ActivationPreparationError, 'ACTIVATION.RECOVERY_REQUIRED'):
                                    owner.recover_durable_activation()
                            self.assertEqual(acquisitions, [original_root])
                            for target in targets:
                                self.assertGreater(len(hits[target]), calls_before[target])
                                self.assertFalse(retained[target].closed)
                            self.assert_stopped(source, owner, peer)
                            self.assertEqual(self.assets(source, identity), before)
                    owner.recover_durable_activation()
                    self.assertTrue(all(authority.closed for authority in retained.values()))
                    self.assertFalse(owner._family._recovery_required)
                    reopened = open_canonical_tm_store(source)
                    if replacement:
                        self.assertEqual(reopened.exact_records('hello')[0].target_raw, 'prior')
                        reopened.coordinator.close_family_session()
                    else:
                        self.assertIsNone(reopened)

    def _exercise_internal_owned_close(self, replacement, consumer, window, *, multiple=False):
        import platform_fs_contracts as fs
        import platform_fs_posix as posix
        scopes = {
            'capture': (journal._CallerHeldPosixActivationBorrow, 'capture', fs.BoundSynchronizedRegularFile, 1),
            'read_journal': (journal._CallerHeldPosixActivationBorrow, 'read_journal', fs.BoundSynchronizedRegularFile, 1),
            'exit_sql': (journal._CallerHeldPosixActivationBorrow, 'exit_sql', fs.BoundExistingFileMutationGuard, 1),
            'remove': (journal._CallerHeldPosixActivationBorrow, 'remove', fs.BoundRegularFile, 1),
            'terminal_pending': (journal._CallerHeldPosixActivationBorrow, 'publish_bytes', fs.PendingPublication, 1),
            'terminal_candidate': (journal._CallerHeldPosixActivationBorrow, 'publish_bytes', fs.CandidateFile, 1),
            'observation_reader': (journal, '_observe_posix_cancelled_quarantine', fs.BoundRegularFile, 1),
            'observation_descendant': (journal, '_observe_posix_cancelled_quarantine', fs.BoundDirectoryAuthority, 1),
            'observation_parent': (journal, '_observe_posix_cancelled_quarantine', fs.BoundDirectoryAuthority, 2),
            'retirement_reader': (journal, '_retire_posix_cancelled_candidates', fs.BoundRegularFile, 1),
            'retirement_source_parent': (journal, '_retire_posix_cancelled_candidates', fs.RetirementSourceDirectoryAuthority, 1),
            'retirement_source': (journal, '_retire_posix_cancelled_candidates', fs.ExistingRetirementSource, 1),
            'retirement_retained': (journal, '_retire_posix_cancelled_candidates', fs.RetainedRetirement, 1),
            'retirement_target': (journal, '_retire_posix_cancelled_candidates', fs.RetirementDirectoryAuthority, 1),
            'retirement_root': (journal, '_retire_posix_cancelled_candidates', fs.RetirementDirectoryAuthority, 2),
            'journal_candidate': (journal, '_write_posix_activation_journal', fs.CandidateFile, 1),
            'journal_pending': (journal, '_write_posix_activation_journal', fs.PendingPublication, 1),
            'failed_temporary_reader': (journal._CallerHeldPosixActivationBorrow, 'retain_failed_journal_temporary', fs.BoundRegularFile, 1),
        }
        target, method_name, authority_type, ordinal = scopes[consumer]
        with self.resource(replacement) as data, self.resource(True) as healthy:
            source, identity, owner, service, peer = data
            before = self.assets(source, identity)
            root, backend = owner._family._root, owner._family._backend
            entered, observing, candidates_seen, retained, close_hits, extra_hits, errors = [], [], [], [], [], [], []
            method, actual_close = getattr(target, method_name), authority_type.close
            observation = journal._observe_posix_cancelled_quarantine
            def observed(*args, **kwargs):
                observing.append(True)
                try:
                    return observation(*args, **kwargs)
                finally:
                    observing.pop()
            def watched(*args, **kwargs):
                entered.append(True)
                try:
                    return method(*args, **kwargs)
                except BaseException as error:
                    errors.append(error)
                    raise
                finally:
                    entered.pop()
            def closing(value):
                eligible = primary_hits and entered and value is not root
                if consumer.startswith('retirement_') and observing:
                    eligible = False
                if eligible and not any(value is seen for seen in candidates_seen):
                    candidates_seen.append(value)
                    if multiple or len(candidates_seen) == ordinal:
                        retained.append(value)
                if any(value is held for held in retained):
                    close_hits.append(value)
                    if window == 'after':
                        actual_close(value)
                    raise OSError('owned close ' + consumer + ' ' + str(next(i for i, held in enumerate(retained) if held is value)))
                return actual_close(value)
            def fail_extra_flush(candidate):
                result = actual_flush(candidate)
                if primary_hits and not extra_hits and consumer in ('terminal_candidate', 'failed_temporary_reader'):
                    if consumer == 'failed_temporary_reader' or entered:
                        extra_hits.append(candidate)
                        raise OSError('secondary real candidate flush')
                return result
            def fail_retirement(injector, name):
                if consumer == 'retirement_source' and name == 'existing_retirement_before_arm' and not extra_hits:
                    extra_hits.append(name)
                    raise OSError('secondary real retirement arm')
                return actual_fault(injector, name)
            def record_writer(*args, **kwargs):
                try:
                    return writer(*args, **kwargs)
                except BaseException as error:
                    errors.append(error)
                    raise
            try:
                with self.fault(owner, identity, 'write' if consumer == 'journal_candidate' else 'close') as (primary_hits, _), ExitStack() as stack:
                    actual_close = authority_type.close
                    actual_flush, actual_fault = CandidateFile.flush_content, posix._hit_fault
                    writer = journal._write_posix_activation_journal
                    if method_name != '_write_posix_activation_journal':
                        stack.enter_context(patch.object(journal, '_write_posix_activation_journal', record_writer))
                    if consumer.startswith('retirement_'):
                        stack.enter_context(patch.object(journal, '_observe_posix_cancelled_quarantine', observed))
                    stack.enter_context(patch.object(target, method_name, watched))
                    stack.enter_context(patch.object(authority_type, 'close', closing))
                    stack.enter_context(patch.object(CandidateFile, 'flush_content', fail_extra_flush))
                    stack.enter_context(patch.object(posix, '_hit_fault', fail_retirement))
                    try:
                        outcome = self.invoke(replacement, source, identity, service)
                    except BaseException as error:
                        errors.append(error)
                    else:
                        self.assertIs(type(outcome), MigrationFailure)
                    self.assertTrue(retained, 'actual owned close must be reached')
                    if consumer in ('terminal_candidate', 'failed_temporary_reader', 'retirement_source'):
                        self.assertEqual(len(extra_hits), 1)
                    if multiple:
                        self.assertGreaterEqual(len(retained), 2)
                    self.assertTrue(all(value.closed == (window == 'after') for value in retained))
                    self.assert_stopped(source, owner, peer)
                    self.assertEqual(self.assets(source, identity), before)
                    self.assertEqual(healthy[-1].exact_records('hello')[0].target_raw, 'prior')
                    messages = []
                    for error in errors[:1]:
                        while error is not None:
                            messages.extend([str(error), *getattr(error, '__notes__', ())])
                            error = error.__cause__
                    self.assertIn('primary journal', ' '.join(messages))
                    for index in range(len(retained)):
                        self.assertIn('owned close ' + consumer + ' ' + str(index), ' '.join(messages))
                    for _ in range(2):
                        previous = {id(value): sum(hit is value for hit in close_hits) for value in retained}
                        acquire, acquisitions = type(backend).acquire, []
                        def acquired(current, rooted, *args, **kwargs):
                            self.assertIs(rooted, root)
                            acquisitions.append(rooted)
                            return acquire(current, rooted, *args, **kwargs)
                        with patch.object(type(backend), 'acquire', acquired), patch.object(type(backend), 'bind_root', side_effect=AssertionError('fresh root before owned close')), patch.object(store, 'recover_durable_activation', side_effect=AssertionError('raw recovery before owned close')):
                            with self.assertRaisesRegex(journal.ActivationPreparationError, 'ACTIVATION.RECOVERY_REQUIRED'):
                                owner.recover_durable_activation()
                        self.assertEqual(acquisitions, [root])
                        for value in retained:
                            self.assertGreater(sum(hit is value for hit in close_hits), previous[id(value)])
                        self.assert_stopped(source, owner, peer)
                        self.assertEqual(self.assets(source, identity), before)
                owner.recover_durable_activation()
                self.assertTrue(all(value.closed for value in retained))
                self.assertFalse(owner._family._pending_activation_closes)
                self.assertFalse(owner._family._recovery_required)
                reopened = open_canonical_tm_store(source)
                if replacement:
                    self.assertEqual(reopened.exact_records('hello')[0].target_raw, 'prior')
                    reopened.coordinator.close_family_session()
                else:
                    self.assertIsNone(reopened)
            finally:
                for value in retained:
                    actual_close(value)

    def test_internal_owned_close_inventory(self):
        consumers = ('capture', 'read_journal', 'exit_sql', 'remove', 'terminal_pending', 'terminal_candidate',
                     'observation_reader', 'observation_descendant', 'observation_parent', 'retirement_reader',
                     'retirement_source_parent', 'retirement_source', 'retirement_retained', 'retirement_target',
                     'retirement_root', 'journal_candidate', 'journal_pending', 'failed_temporary_reader')
        for replacement in (False, True):
            for consumer in consumers:
                for window in ('before', 'after'):
                    with self.subTest(replacement=replacement, consumer=consumer, window=window):
                        self._exercise_internal_owned_close(replacement, consumer, window)

    def test_multiple_cancel_owned_closes_keep_every_reference_and_error(self):
        for replacement in (False, True):
            for window in ('before', 'after'):
                with self.subTest(replacement=replacement, window=window):
                    self._exercise_internal_owned_close(replacement, 'observation_reader', window, multiple=True)

    def test_sql_body_and_both_close_errors_preserve_original_primary(self):
        from platform_fs_contracts import BoundExistingFileMutationGuard
        with self.resource(True) as data:
            source, identity, owner, service, peer = data
            close, retained, hits = BoundExistingFileMutationGuard.close, [], []
            def guard_close(value):
                if not retained:
                    retained.append(value)
                if retained[0] is value:
                    hits.append(value)
                    raise OSError('guard close secondary')
                return close(value)
            def connection_close(connection):
                connection.close()
                raise OSError('connection close secondary')
            try:
                with patch.object(BoundExistingFileMutationGuard, 'close', guard_close):
                    with self.assertRaisesRegex(OSError, 'SQL body primary') as raised:
                        with owner._posix_recovery_port() as port:
                            with store._open_configured_connection(identity.canonical_sidecar_path, require_existing=True,
                                    _stage_borrow=port.posix_activation_borrow, purpose='activation-validation',
                                    _close_connection=connection_close) as connection:
                                connection.execute('SELECT 1').fetchone()
                                raise OSError('SQL body primary')
                    facts = ' '.join(getattr(raised.exception, '__notes__', ()))
                    self.assertIn('connection close secondary', facts)
                    self.assertIn('guard close secondary', facts)
                    self.assertTrue(hits)
                    self.assert_stopped(source, owner, peer)
                owner.recover_durable_activation()
                self.assertTrue(retained[0].closed)
                self.assertFalse(owner._family._recovery_required)
            finally:
                for value in retained:
                    close(value)

    def test_backup_direct_owned_closes_return_to_original_family(self):
        for consumer, authority_type in (('reader', BoundRegularFile), ('candidate', CandidateFile), ('pending', PendingPublication)):
            for window in ('before', 'after'):
                with self.subTest(consumer=consumer, window=window), self.resource(True) as data:
                    source, identity, owner, service, peer = data
                    before = self.assets(source, identity)
                    method, close, flush = journal._create_recovery_backup, authority_type.close, CandidateFile.flush_content
                    active, held, hits, flush_hits, errors = [], [], [], [], []
                    def watched(*args, **kwargs):
                        active.append(True)
                        try:
                            return method(*args, **kwargs)
                        except BaseException as error:
                            errors.append(error)
                            raise
                        finally:
                            active.pop()
                    def closing(value):
                        if active and not held:
                            held.append(value)
                        if any(value is prior for prior in held):
                            hits.append(value)
                            if window == 'after':
                                close(value)
                            raise OSError('direct backup ' + consumer + ' close')
                        return close(value)
                    def flushing(value):
                        result = flush(value)
                        if consumer == 'candidate' and active and not flush_hits:
                            flush_hits.append(value)
                            raise OSError('direct backup flush primary')
                        return result
                    try:
                        with patch.object(journal, '_create_recovery_backup', watched), patch.object(authority_type, 'close', closing), patch.object(CandidateFile, 'flush_content', flushing):
                            outcome = self.invoke(True, source, identity, service)
                            self.assertIs(type(outcome), MigrationFailure)
                            self.assertEqual(len(held), 1)
                            self.assertEqual(held[0].closed, window == 'after')
                            self.assertTrue(errors)
                            facts = str(errors[0]) + ' '.join(getattr(errors[0], '__notes__', ()))
                            self.assertIn('direct backup ' + consumer + ' close', facts)
                            if consumer == 'candidate':
                                self.assertEqual(len(flush_hits), 1)
                                self.assertIn('direct backup flush primary', facts)
                            self.assert_stopped(source, owner, peer)
                            calls = len(hits)
                            with patch.object(store, 'recover_durable_activation', side_effect=AssertionError('raw recovery before owned backup close')):
                                with self.assertRaisesRegex(journal.ActivationPreparationError, 'ACTIVATION.RECOVERY_REQUIRED'):
                                    owner.recover_durable_activation()
                            self.assertGreater(len(hits), calls)
                            self.assertEqual(self.assets(source, identity), before)
                        owner.recover_durable_activation()
                        self.assertTrue(held[0].closed)
                        self.assertFalse(owner._family._pending_activation_closes)
                        self.assertFalse(owner._family._recovery_required)
                        self.assertEqual(peer.exact_records('hello')[0].target_raw, 'prior')
                    finally:
                        for value in held:
                            close(value)

    def test_retired_replay_parent_close_remains_owned(self):
        from platform_fs_contracts import BoundDirectoryAuthority, RetainedRetirement
        for replacement in (False, True):
            for window in ('before', 'after'):
                with self.subTest(replacement=replacement, window=window), self.resource(replacement) as data:
                    source, identity, owner, service, peer = data
                    method, observation = journal._retire_posix_cancelled_candidates, journal._observe_posix_cancelled_quarantine
                    close, retained_close = BoundDirectoryAuthority.close, RetainedRetirement.close
                    held, hits, active, observing, seed_hits = [], [], [], [], []
                    def seed(value):
                        result = retained_close(value)
                        if not seed_hits:
                            seed_hits.append(value)
                            raise OSError('retirement replay seed close')
                        return result
                    with self.fault(owner, identity, 'close'), patch.object(RetainedRetirement, 'close', seed):
                        if replacement:
                            with self.assertRaises(migration.MigrationPreflightError):
                                self.invoke(True, source, identity, service)
                        else:
                            self.assertEqual(self.invoke(False, source, identity, service).stage, 'RECOVERY')
                    self.assertEqual(len(seed_hits), 1)
                    def watched(*args, **kwargs):
                        active.append(True)
                        try:
                            return method(*args, **kwargs)
                        finally:
                            active.pop()
                    def observed(*args, **kwargs):
                        observing.append(True)
                        try:
                            return observation(*args, **kwargs)
                        finally:
                            observing.pop()
                    def closing(value):
                        if active and not observing and not held:
                            held.append(value)
                        if any(value is prior for prior in held):
                            hits.append(value)
                            if window == 'after':
                                close(value)
                            raise OSError('retired replay parent close')
                        return close(value)
                    try:
                        with patch.object(journal, '_retire_posix_cancelled_candidates', watched), patch.object(journal, '_observe_posix_cancelled_quarantine', observed), patch.object(BoundDirectoryAuthority, 'close', closing):
                            with self.assertRaisesRegex(OSError, 'retired replay parent close'):
                                owner.recover_durable_activation()
                            self.assertEqual(len(held), 1)
                            self.assertEqual(held[0].closed, window == 'after')
                            self.assert_stopped(source, owner, peer)
                            calls = len(hits)
                            with patch.object(store, 'recover_durable_activation', side_effect=AssertionError('raw recovery before retired parent close')):
                                with self.assertRaisesRegex(journal.ActivationPreparationError, 'ACTIVATION.RECOVERY_REQUIRED'):
                                    owner.recover_durable_activation()
                            self.assertGreater(len(hits), calls)
                        owner.recover_durable_activation()
                        self.assertTrue(held[0].closed)
                        self.assertFalse(owner._family._pending_activation_closes)
                        self.assertFalse(owner._family._recovery_required)
                    finally:
                        for value in held:
                            close(value)

    def test_cancelled_terminal_prior_close_preserves_cause_and_recovery(self):
        from platform_fs_contracts import BoundExistingFileMutationGuard
        for window in ('before', 'after'):
            with self.subTest(window=window), self.resource(True) as data:
                source, identity, owner, service, peer = data
                with self.fault(owner, identity, 'write'):
                    self.assertIs(type(self.invoke(True, source, identity, service)), MigrationFailure)
                self.assertTrue(journal._activation_terminal_path(identity).exists())
                self.assertFalse(journal._activation_journal_path(identity).exists())
                before = self.assets(source, identity)
                root, backend = owner._family._root, owner._family._backend
                close, prior = BoundExistingFileMutationGuard.close, recovery._revalidate_recovered_prior_set
                entered, held, hits = [], [], []
                def validated(*args, **kwargs):
                    entered.append(True)
                    try:
                        return prior(*args, **kwargs)
                    finally:
                        entered.pop()
                def closing(value):
                    if entered and not held:
                        held.append(value)
                    if held and value is held[0]:
                        hits.append(value)
                        if window == 'after':
                            close(value)
                        error = OSError('terminal replay prior guard close ' + window)
                        error.add_note('terminal replay owned guard close fact')
                        raise error
                    return close(value)
                acquire, acquisitions = type(backend).acquire, []
                def acquired(current, rooted, *args, **kwargs):
                    self.assertIs(rooted, root)
                    acquisitions.append(rooted)
                    return acquire(current, rooted, *args, **kwargs)
                try:
                    with patch.object(recovery, '_revalidate_recovered_prior_set', validated), patch.object(BoundExistingFileMutationGuard, 'close', closing):
                        with patch.object(type(backend), 'acquire', acquired), patch.object(type(backend), 'bind_root', side_effect=AssertionError('fresh bind during cancelled replay')):
                            with self.assertRaises(journal.ActivationPreparationError) as caught:
                                owner.recover_durable_activation()
                        self.assertEqual(caught.exception.code, 'ACTIVATION.RECOVERY_PRIOR_SET_INVALID')
                        self.assertEqual(acquisitions, [root])
                        self.assertEqual(len(held), 1)
                        self.assertEqual(held[0].closed, window == 'after')
                        self.assertTrue(hits)
                        self.assertTrue(owner._family._pending_activation_closes)
                        self.assert_stopped(source, owner, peer)
                        facts, seen, cycle = [], set(), False
                        error = caught.exception
                        while error is not None:
                            if id(error) in seen:
                                cycle = True
                                break
                            seen.add(id(error))
                            facts.extend([str(error), *getattr(error, '__notes__', ())])
                            error = error.__cause__
                        for _ in range(2):
                            calls = len(hits)
                            acquisitions.clear()
                            with patch.object(type(backend), 'acquire', acquired), patch.object(type(backend), 'bind_root', side_effect=AssertionError('fresh bind before original guard close')), patch.object(store, 'recover_durable_activation', side_effect=AssertionError('raw recovery before original guard close')):
                                with self.assertRaisesRegex(journal.ActivationPreparationError, 'ACTIVATION.RECOVERY_REQUIRED'):
                                    owner.recover_durable_activation()
                            self.assertEqual(acquisitions, [root])
                            self.assertGreater(len(hits), calls)
                            self.assertTrue(owner._family._pending_activation_closes)
                            self.assert_stopped(source, owner, peer)
                            self.assertEqual(self.assets(source, identity), before)
                    acquisitions.clear()
                    with patch.object(type(backend), 'acquire', acquired), patch.object(type(backend), 'bind_root', side_effect=AssertionError('fresh bind replacing original guard')):
                        owner.recover_durable_activation()
                    self.assertEqual(acquisitions, [root])
                    self.assertTrue(held[0].closed)
                    self.assertFalse(owner._family._pending_activation_closes)
                    self.assertFalse(owner._family._recovery_required)
                    self.assertEqual(peer.exact_records('hello')[0].target_raw, 'prior')
                    reopened = open_canonical_tm_store(source)
                    self.assertIsNotNone(reopened)
                    try:
                        self.assertEqual(reopened.exact_records('hello')[0].target_raw, 'prior')
                    finally:
                        reopened.coordinator.close_family_session()
                    self.assertFalse(cycle, 'cancelled terminal must preserve the original cause chain')
                    self.assertIn('terminal replay prior guard close ' + window, ' '.join(facts))
                    self.assertIn('terminal replay owned guard close fact', ' '.join(facts))
                finally:
                    for value in held:
                        close(value)

    def test_lineage_pair_uses_original_resource_lock_in_direct_consumers(self):
        import os
        for consumer in ('preparation', 'cancelled-replay', 'completed-replay', 'completion'):
            with self.subTest(consumer=consumer), self.resource(True) as data:
                source, identity, owner, service, peer = data
                marker = journal._activation_lineage_marker_path(identity)
                temporary = journal._activation_lineage_marker_temp_path(marker)
                before = marker.read_bytes()
                backend, rooted = owner._family._backend, owner._family._root
                complete = type(backend).complete_existing_link_pair
                hits = []
                def completed(current, root, lease, final_name, temporary_name, content):
                    self.assertIs(root, rooted)
                    lease.reprove_binding(root, owner._family._lock_name,
                        ('localcat-initial-activation-lock-v1\0' + identity.target_identity + '\n').encode('ascii'))
                    self.assertEqual((final_name, temporary_name), (marker.name, temporary.name))
                    self.assertEqual(content.content_sha256.hex(), hashlib.sha256(before).hexdigest())
                    hits.append(True)
                    return complete(current, root, lease, final_name, temporary_name, content)
                if consumer == 'cancelled-replay':
                    with self.fault(owner, identity, 'close'):
                        self.invoke(True, source, identity, service)
                ensure = recovery._ensure_activation_lineage_marker
                def paired_at_completion(current_identity, **kwargs):
                    self.assertIs(kwargs['caller_borrow'].root, rooted)
                    os.link(marker, temporary)
                    return ensure(current_identity, **kwargs)
                with patch.object(type(backend), 'complete_existing_link_pair', completed):
                    if consumer == 'completion':
                        with patch.object(recovery, '_ensure_activation_lineage_marker', paired_at_completion):
                            self.assertIs(type(self.invoke(True, source, identity, service)), MigrationReport)
                    else:
                        os.link(marker, temporary)
                        if consumer == 'preparation':
                            self.assertIs(type(self.invoke(True, source, identity, service)), MigrationReport)
                        else:
                            owner.recover_durable_activation()
                self.assertEqual(hits, [True])
                self.assertFalse(temporary.exists())
                self.assertEqual(marker.read_bytes(), before)
                self.assertEqual(marker.stat().st_nlink, 1)
                self.assertFalse(owner._family._recovery_required)

    def test_rollback_terminal_preserves_receipt_bytes_and_replays_complete_readonly_closure(self):
        from tests.test_tm_activation_journal import _first_prepared, _existing_fixture
        from tests.test_tm_activation_rollback import _manifest_published_window
        for replacement, attack in ((prior, attack) for prior in (False, True) for attack in (None, 'extra', 'source', 'manifest', 'clone', 'missing', 'prior')):
            with self.subTest(replacement=replacement, attack=attack), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                if replacement:
                    identity, _, owner, _, sealed = _existing_fixture(root, fts5_available=True)
                    prepared = owner.activate(sealed)
                    handle = owner.publish_prepared_activation(prepared)
                else:
                    identity, owner, _, prepared, handle = _first_prepared(root)
                fresh = None
                try:
                    _manifest_published_window(owner, prepared, handle)
                    self.assertEqual(owner.rollback_durable_activation().action, 'ROLLED_BACK')
                    terminal = journal._activation_terminal_path(identity)
                    record = journal._parse_activation_journal_bytes(terminal.read_bytes(), expected_journal_path=handle.journal_path)
                    quarantine = journal._activation_quarantine_directory(identity, record)
                    database = quarantine / identity.canonical_sidecar_path.name
                    self.assertNotEqual(hashlib.sha256(database.read_bytes()).hexdigest(), record.sealed_content_attestation.database.sha256)
                    if attack == 'prior' and replacement:
                        record.prior_db_path.write_bytes(b'changed prior')
                    elif attack == 'prior':
                        identity.canonical_sidecar_path.write_bytes(b'foreign active DB')
                    elif attack == 'extra':
                        (quarantine / 'foreign').write_bytes(b'unknown fifth role')
                    elif attack == 'source':
                        identity.configured_jsonl_path.write_bytes(b'changed source')
                    elif attack == 'manifest':
                        (quarantine / identity.snapshot_manifest_path.name).write_bytes(b'changed manifest')
                    elif attack == 'clone':
                        clone = root / 'replacement'
                        clone.write_bytes(database.read_bytes())
                        clone.replace(database)
                    elif attack == 'missing':
                        database.unlink()
                    before = {p.name: p.read_bytes() for p in quarantine.iterdir()}
                    terminal_before = terminal.read_bytes()
                    fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
                    backend = fresh._family._backend
                    observe = type(backend).observe_descendant_entries
                    hits = []
                    def observed(current, rooted, lease, descendant, limits):
                        self.assertIs(rooted, fresh._family._root)
                        lease.reprove_binding(rooted, fresh._family._lock_name, ('localcat-initial-activation-lock-v1\0' + identity.target_identity + '\n').encode('ascii'))
                        hits.append(True)
                        return observe(current, rooted, lease, descendant, limits)
                    with patch.object(type(backend), 'observe_descendant_entries', observed):
                        if attack is None:
                            fresh.recover_durable_activation()
                            fresh.recover_durable_activation()
                            self.assertEqual(fresh.current_generation, 0 if replacement else None)
                            self.assertTrue(hits)
                            self.assertFalse(fresh._family._recovery_required)
                        else:
                            with self.assertRaises(journal.ActivationPreparationError):
                                fresh.recover_durable_activation()
                            self.assertTrue(fresh._family._recovery_required)
                    self.assertEqual({p.name: p.read_bytes() for p in quarantine.iterdir()}, before)
                    self.assertEqual(terminal.read_bytes(), terminal_before)
                    self.assertEqual(identity.canonical_sidecar_path.exists(), attack == 'prior' and not replacement)
                    self.assertEqual(identity.snapshot_manifest_path.exists(), replacement)
                finally:
                    if fresh is not None:
                        fresh.close_family_session()
                    owner.close_family_session()

    def test_ordinary_cancelled_quarantine_rejects_unknown_extra_entry(self):
        with self.resource(True) as data:
            source, identity, owner, service, peer = data
            with self.fault(owner, identity, 'close'):
                self.invoke(True, source, identity, service)
            terminal = journal._activation_terminal_path(identity)
            record = journal._parse_activation_journal_bytes(terminal.read_bytes(), expected_journal_path=journal._activation_journal_path(identity))
            quarantine = journal._activation_quarantine_directory(identity, record)
            (quarantine / 'foreign').write_bytes(b'foreign')
            before = {p.name: p.read_bytes() for p in quarantine.iterdir()}
            with self.assertRaisesRegex(journal.ActivationPreparationError, 'ACTIVATION.QUARANTINE_FOREIGN'):
                owner.recover_durable_activation()
            self.assert_stopped(source, owner, peer, factory_error='TM.CANONICAL_RECOVERY_FAILED:ACTIVATION.QUARANTINE_FOREIGN')
            self.assertEqual({p.name: p.read_bytes() for p in quarantine.iterdir()}, before)

    def test_prejournal_second_persistence_failure_keeps_live_recovery_authority(self):
        for replacement, kind in ((False, 'journal'), (True, 'journal'), (True, 'mirror')):
            with self.subTest(replacement=replacement, kind=kind), self.resource(replacement) as data:
                source, identity, owner, service, peer = data
                root = owner._family._root
                create, flush = type(root).create_candidate, CandidateFile.flush_content
                candidates, hits = [], []
                failed_path = (journal._activation_terminal_temp_path(journal._activation_terminal_path(identity))
                               if kind == 'mirror' else journal._activation_journal_temp_path(journal._activation_journal_path(identity)))
                def created(authority, name, *args, **kwargs):
                    value = create(authority, name, *args, **kwargs)
                    if authority is root and str(name) == failed_path.name:
                        candidates.append(value)
                    return value
                def failed(value):
                    result = flush(value)
                    if value in candidates and len(hits) < 2:
                        hits.append('real flush')
                        raise OSError('persistent journal failure')
                    return result
                with patch.object(type(root), 'create_candidate', created), patch.object(CandidateFile, 'flush_content', failed):
                    if replacement:
                        with self.assertRaises(migration.MigrationPreflightError):
                            self.invoke(replacement, source, identity, service)
                    else:
                        self.assertEqual(self.invoke(replacement, source, identity, service).stage, 'RECOVERY')
                self.assertEqual(len(hits), 2)
                self.assert_stopped(source, owner, peer)
                # The original live preparation is the only complete authority
                # when both journal attempts failed before naming. Recovery must
                # retry it, not clear the family based on the old/absent terminal.
                fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
                try:
                    fresh.recover_durable_activation()
                    self.assertFalse(owner._family._recovery_required)
                    self.assertEqual(owner.state, 'READY')
                    self.assertIsNone(owner._preparation)
                    self.assertTrue(journal._activation_terminal_path(identity).exists())
                    if peer is not None:
                        self.assertEqual(peer.exact_records('hello')[0].target_raw, 'prior')
                finally:
                    fresh.close_family_session()

    def test_prepared_rewrite_failure_retries_only_live_owned_temporary(self):
        with self.resource(True) as data:
            source, identity, owner, service, peer = data
            flush = CandidateFile.flush_content
            hits = []
            temporary = journal._activation_journal_temp_path(journal._activation_journal_path(identity))
            def failed(candidate):
                result = flush(candidate)
                current = owner._family._root.inspect_entry(temporary.name)
                if primary_hits and current is not None and current.identity == candidate.identity() and not hits:
                    hits.append(True)
                    raise OSError('secondary prepared journal flush')
                return result
            with self.fault(owner, identity, 'close') as (primary_hits, _), patch.object(CandidateFile, 'flush_content', failed):
                with self.assertRaises(migration.MigrationPreflightError) as raised:
                    self.invoke(True, source, identity, service)
            self.assertEqual(hits, [True])
            self.assertTrue(temporary.exists())
            self.assertIn('primary journal close', repr(raised.exception.__cause__.__cause__))
            self.assert_stopped(source, owner, peer)
            owner.recover_durable_activation()
            self.assertFalse(temporary.exists())
            self.assertFalse(owner._family._recovery_required)
            self.assertEqual(peer.exact_records('hello')[0].target_raw, 'prior')

    def test_original_owner_can_explicitly_recover_durable_cancel(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement), self.resource(replacement) as data:
                source, identity, owner, service, peer = data
                with self.fault(owner, identity, 'close'), patch.object(recovery, '_retire_cancelled_candidate_assets', side_effect=OSError('secondary')):
                    if replacement:
                        with self.assertRaises(migration.MigrationPreflightError):
                            self.invoke(replacement, source, identity, service)
                    else:
                        self.assertEqual(self.invoke(replacement, source, identity, service).stage, 'RECOVERY')
                owner.recover_durable_activation()
                self.assertFalse(owner._family._recovery_required)
                self.assertEqual(owner.state, 'READY')
                self.assertIsNone(owner._preparation)
                self.assertFalse(owner._initial_activation_authority_is_unavailable())

    def test_named_database_stays_recovery_only_until_existing_completion(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement), self.resource(replacement) as data:
                source, identity, owner, service, peer = data
                source_before = source.read_bytes()
                primary = OSError('after real receipt commit')
                phases = []
                def fail_manifest(*args, **kwargs):
                    pending = journal._parse_activation_journal_bytes(journal._activation_journal_path(identity).read_bytes(), expected_journal_path=journal._activation_journal_path(identity))
                    phases.append(pending.phase.value)
                    raise primary
                code = migration._initial_activation_error_code
                primary_seen = []
                def observe_primary(error):
                    primary_seen.append(error)
                    return code(error)
                with patch.object(recovery, '_publish_activation_manifest', side_effect=fail_manifest), patch.object(migration, '_initial_activation_error_code', observe_primary):
                    if replacement:
                        with self.assertRaises(migration.MigrationPreflightError):
                            self.invoke(replacement, source, identity, service)
                    else:
                        self.assertEqual(self.invoke(replacement, source, identity, service).stage, 'PUBLISH')
                self.assertEqual(phases, ['DB_REPLACED'])
                if not replacement:
                    self.assertIn(primary, primary_seen)
                    self.assertEqual(source.read_bytes(), source_before)
                    self.assertFalse(journal._activation_journal_path(identity).exists())
                    terminal = journal._parse_activation_journal_bytes(journal._activation_terminal_path(identity).read_bytes(), expected_journal_path=journal._activation_journal_path(identity))
                    quarantine = journal._activation_quarantine_directory(identity, terminal)
                    self.assertEqual({path.name for path in quarantine.iterdir()}, {identity.canonical_sidecar_path.name, terminal.candidate_manifest_temp_path.name})
                    with sqlite3.connect(quarantine / identity.canonical_sidecar_path.name) as connection:
                        self.assertEqual(connection.execute('SELECT status FROM tm_snapshot_receipt').fetchall(), [('completed',)])
                    connection.close()
                    self.assertFalse(owner._family._recovery_required)
                    self.assertIsNone(owner.current_generation)
                    self.assertIsNone(owner.recover_durable_activation())
                    self.assertIsNone(owner.recover_durable_activation())
                    self.assertFalse(identity.canonical_sidecar_path.exists())
                    self.assertFalse(identity.snapshot_manifest_path.exists())
                    self.assertEqual(source.read_bytes(), source_before)
                    continue
                record = journal._parse_activation_journal_bytes(journal._activation_journal_path(identity).read_bytes(), expected_journal_path=journal._activation_journal_path(identity))
                self.assertEqual(record.phase.value, 'DB_REPLACED')
                self.assertFalse(record.candidate_stage_db_path.exists())
                self.assertTrue(owner._family._recovery_required)
                fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
                try:
                    report = fresh.recover_durable_activation()
                    self.assertEqual(report.action, 'COMPLETED')
                    self.assertEqual(owner.current_generation, 1 if replacement else 0)
                    self.assertFalse(owner._family._recovery_required)
                    self.assertIsNone(owner._preparation)
                    self.assertEqual(store.SQLiteTMStore.from_coordinator(owner).exact_records('hello')[0].target_raw, 'prior')
                finally:
                    fresh.close_family_session()

    def test_retained_close_failure_preserves_primary_and_replays_whole_pair(self):
        from platform_fs_contracts import RetainedRetirement
        with self.resource(True) as data:
            source, identity, owner, service, peer = data
            close, hits = RetainedRetirement.close, []
            def failed(value):
                result = close(value)
                if not hits:
                    hits.append(True)
                    raise OSError('retained cancellation close')
                return result
            with self.fault(owner, identity, 'close'), patch.object(RetainedRetirement, 'close', failed):
                with self.assertRaises(migration.MigrationPreflightError) as raised:
                    self.invoke(True, source, identity, service)
            self.assertEqual(hits, [True])
            self.assertIn('primary journal close', repr(raised.exception.__cause__.__cause__))
            self.assert_stopped(source, owner, peer)
            fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
            try:
                fresh.recover_durable_activation()
                self.assertFalse(owner._family._recovery_required)
                self.assertEqual(owner.state, 'READY')
                self.assertEqual(peer.exact_records('hello')[0].target_raw, 'prior')
            finally:
                fresh.close_family_session()

    def test_local_read_and_sql_close_failure_cannot_restore_family(self):
        from platform_fs_contracts import BoundSynchronizedRegularFile, BoundExistingFileMutationGuard
        for replacement in (False, True):
            for kind in ('capture', 'read_journal', 'sql'):
                with self.subTest(replacement=replacement, kind=kind), self.resource(replacement) as data:
                    source, identity, owner, service, peer = data
                    root, active, hits = owner._family._root, [], []
                    capture = journal._CallerHeldPosixActivationBorrow.capture
                    read = journal._CallerHeldPosixActivationBorrow.read_journal
                    read_close = BoundSynchronizedRegularFile.close
                    guard_close = BoundExistingFileMutationGuard.close
                    def watched(borrow, method, name, *args, **kwargs):
                        self.assertIs(borrow.root, root)
                        if primary_hits:
                            active.append(name)
                        try:
                            return method(borrow, *args, **kwargs)
                        finally:
                            if primary_hits and active:
                                active.pop()
                    def failed(value, actual, name):
                        result = actual(value)
                        if primary_hits and not hits and (kind == name or (name == 'reader' and active and active[-1] == kind)):
                            hits.append(kind)
                            raise OSError('local ' + kind + ' close')
                        return result
                    with self.fault(owner, identity, 'close') as (primary_hits, _), ExitStack() as stack:
                        stack.enter_context(patch.object(journal._CallerHeldPosixActivationBorrow, 'capture', lambda b, *a, **k: watched(b, capture, 'capture', *a, **k)))
                        stack.enter_context(patch.object(journal._CallerHeldPosixActivationBorrow, 'read_journal', lambda b, *a, **k: watched(b, read, 'read_journal', *a, **k)))
                        stack.enter_context(patch.object(BoundSynchronizedRegularFile, 'close', lambda v: failed(v, read_close, 'reader')))
                        stack.enter_context(patch.object(BoundExistingFileMutationGuard, 'close', lambda v: failed(v, guard_close, 'sql')))
                        if replacement:
                            with self.assertRaises(migration.MigrationPreflightError) as raised:
                                self.invoke(True, source, identity, service)
                            self.assertIn('primary journal close', repr(raised.exception.__cause__.__cause__))
                        else:
                            self.assertEqual(self.invoke(False, source, identity, service).stage, 'RECOVERY')
                    self.assertEqual(hits, [kind])
                    self.assert_stopped(source, owner, peer)
                    fresh = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=owner.canonical_store_id)
                    try:
                        fresh.recover_durable_activation()
                        self.assertFalse(owner._family._recovery_required)
                        self.assertEqual(owner.state, 'READY')
                        self.assertIsNone(owner._preparation)
                        if peer is not None:
                            self.assertEqual(peer.exact_records('hello')[0].target_raw, 'prior')
                    finally:
                        fresh.close_family_session()

    def test_successful_recovery_outer_close_failure_ignores_ambient_exception(self):
        from platform_fs_contracts import LockLease
        with self.resource(True) as data:
            source, identity, owner, service, peer = data
            with self.fault(owner, identity, 'close'), patch.object(recovery, '_retire_cancelled_candidate_assets', side_effect=OSError('secondary')):
                with self.assertRaises(migration.MigrationPreflightError):
                    self.invoke(True, source, identity, service)
            close, hits = LockLease.close, []
            def failed(lease):
                result = close(lease)
                hits.append(True)
                raise OSError('outer successful recovery close')
            try:
                raise OSError('unrelated ambient primary')
            except OSError:
                with patch.object(LockLease, 'close', failed), self.assertRaisesRegex(OSError, 'outer successful recovery close'):
                    owner.recover_durable_activation()
            self.assertEqual(hits, [True])
            self.assertEqual(owner._family._state, 'FAILED')
            self.assert_stopped(source, owner, peer)

    def test_unknown_terminal_temporary_is_preserved(self):
        with self.resource(True) as data:
            source, identity, owner, service, peer = data
            temp = journal._activation_terminal_temp_path(journal._activation_terminal_path(identity))
            with self.fault(owner, identity, 'close'), patch.object(recovery, '_retire_cancelled_candidate_assets', side_effect=OSError('secondary')):
                with self.assertRaises(migration.MigrationPreflightError):
                    self.invoke(True, source, identity, service)
            temp.write_bytes(b'unknown foreign temporary')
            with self.assertRaises(journal.ActivationPreparationError):
                owner.recover_durable_activation()
            self.assertEqual(temp.read_bytes(), b'unknown foreign temporary')
            self.assert_stopped(source, owner, peer)

    def child(self, directory, mode, replacement, window):
        import subprocess
        result = subprocess.run([
            sys.executable, '-B', '-c',
            'from tests.test_tm_posix_unpublished_cancellation import _cancellation_child; _cancellation_child()',
            str(directory), mode, str(int(replacement)), window,
        ], capture_output=True, text=True, timeout=40)
        expected = 73 if mode.startswith('produce') else 74 if mode.startswith('reject') else 0
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        import json
        return json.loads(result.stdout)

    def test_real_crash_matrix_replays_twice_in_fresh_processes(self):
        for replacement in (False, True):
            for window in ('before', 'one', 'two', 'close-before', 'close-after'):
                with self.subTest(replacement=replacement, window=window), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    producer = self.child(root, 'produce', replacement, window)
                    first = self.child(root, 'recover', replacement, window)
                    second = self.child(root, 'recover', replacement, window)
                    self.assertEqual(len({producer['pid'], first['pid'], second['pid']}), 3)
                    self.assertEqual(first['generation'], 0 if replacement else None)
                    self.assertEqual(first['files'], second['files'])
                    self.assertEqual(producer['retired'], {'before': 0, 'one': 1, 'two': 2, 'close-before': 2, 'close-after': 2}[window])

    def test_cold_replay_rejects_same_inode_changed_content_and_clones(self):
        import json
        for window in ('before', 'one', 'two'):
            for mutation in ('same-inode', 'clone'):
                with self.subTest(window=window, mutation=mutation), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    self.child(root, 'produce', True, window)
                    evidence = json.loads((root / 'evidence.json').read_text())
                    candidate = evidence['candidates'][0]
                    path = root / candidate['name']
                    if not path.exists():
                        path = root / evidence['quarantine'] / candidate['name']
                    old = path.stat()
                    payload = path.read_bytes()
                    if mutation == 'same-inode':
                        path.write_bytes(b'changed same inode')
                        self.assertEqual(path.stat().st_ino, old.st_ino)
                    else:
                        replacement = path.with_name(path.name + '.clone')
                        replacement.write_bytes(payload)
                        replacement.replace(path)
                        self.assertNotEqual(path.stat().st_ino, old.st_ino)
                    before = path.read_bytes()
                    self.child(root, 'reject', True, window)
                    self.assertEqual(path.read_bytes(), before)



    def test_terminal_with_recomputed_digest_still_requires_complete_v2_prior(self):
        import json
        import tm_contracts
        for field in ('generation', 'receipt', 'phase'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                self.child(root, 'produce', True, 'two')
                identity = CanonicalResourceIdentity.from_configured_jsonl('tm.cold.cancel', root / 'memory.jsonl')
                terminal = journal._activation_terminal_path(identity)
                envelope = json.loads(terminal.read_text())
                envelope.pop('record_digest')
                if field == 'generation':
                    envelope.update(prior_generation=42, expected_prior_generation=42)
                elif field == 'receipt':
                    envelope['prior_receipt_digest'] = '0' * 64
                else:
                    envelope['phase'] = 'DB_REPLACED'
                envelope['record_digest'] = tm_contracts._stable_digest(envelope)
                terminal.write_text(json.dumps(envelope))
                before = terminal.read_bytes()
                self.child(root, 'reject', True, 'two')
                self.assertEqual(terminal.read_bytes(), before)

    def test_double_persistence_failure_cold_factory_preserves_and_refuses_temporary(self):
        for replacement, kind in ((False, 'journal'), (True, 'journal'), (True, 'mirror')):
            with self.subTest(replacement=replacement, kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                self.child(root, 'produce-double', replacement, kind)
                self.child(root, 'reject-double', replacement, kind)
                self.child(root, 'reject-double', replacement, kind)
        with tempfile.TemporaryDirectory() as directory:
            self.child(Path(directory).resolve(), 'legacy', False, '')

    def test_root_w1_are_borrowed_without_legacy_replay(self):
        with self.resource(True) as data:
            source, identity, owner, service, peer = data
            backend, root = owner._family._backend, owner._family._root
            acquire, bind = type(backend).acquire, type(backend).bind_root
            hits = []
            def bound(current, *args, **kwargs):
                self.assertFalse(primary_hits, 'cancellation rebound root')
                return bind(current, *args, **kwargs)
            def acquired(current, authority, *args, **kwargs):
                self.assertIs(authority, root)
                hits.append(True)
                return acquire(current, authority, *args, **kwargs)
            with self.fault(owner, identity, 'close') as (primary_hits, _), patch.object(type(backend), 'acquire', acquired), patch.object(type(backend), 'bind_root', bound), patch.object(recovery, '_replay_activation_journal', side_effect=AssertionError('legacy raw replay')):
                result = self.invoke(True, source, identity, service)
                self.assertIs(type(result), MigrationFailure)
            self.assertEqual(hits, [True])

    def test_outer_close_preserves_primary_and_cannot_restore_admission(self):
        from platform_fs_contracts import LockLease
        with self.resource(True) as data:
            source, identity, owner, service, peer = data
            close = LockLease.close
            hits = []
            def failed(lease):
                result = close(lease)
                hits.append(True)
                raise OSError('outer W1 close')
            with self.fault(owner, identity, 'close'), patch.object(recovery, '_retire_cancelled_candidate_assets', side_effect=OSError('secondary cancellation')), patch.object(LockLease, 'close', failed):
                with self.assertRaises(migration.MigrationPreflightError) as raised:
                    self.invoke(True, source, identity, service)
            self.assertEqual(hits, [True])
            self.assertIn('primary journal close', repr(raised.exception.__cause__.__cause__))
            self.assertEqual(owner._family._state, 'FAILED')
            self.assertTrue(owner._family._recovery_required)
            self.assertEqual(owner._family._active_count, 0)
            self.assertFalse(owner._family._operation_tokens)
            with self.assertRaises(store.SQLiteStoreLifecycleError):
                peer.exact_records('hello')

    def test_other_resource_remains_available_while_family_is_stopped(self):
        with self.resource(True) as other, self.resource(True) as data:
            source, identity, owner, service, peer = data
            with self.fault(owner, identity, 'close'), patch.object(recovery, '_retire_cancelled_candidate_assets', side_effect=OSError('secondary cancellation')):
                with self.assertRaises(migration.MigrationPreflightError):
                    self.invoke(True, source, identity, service)
            self.assert_stopped(source, owner, peer)
            self.assertEqual(other[4].exact_records('hello')[0].target_raw, 'prior')


def _cancellation_child():
    """真实进程退出测试入口；仅通过生产public owner与公开平台端口造窗口。"""
    import json
    import os
    from platform_fs_contracts import RetainedRetirement
    root, mode, replacement, window = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == '1', sys.argv[4]
    source = root / 'memory.jsonl'
    identity = CanonicalResourceIdentity.from_configured_jsonl('tm.cold.cancel', source)
    owner = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id='store.cold.cancel')
    service = migration.TMMigrationService(resource_identity=identity, canonical_store_id=owner.canonical_store_id, coordinator=owner)
    def assets():
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                for path in (source, identity.canonical_sidecar_path, identity.snapshot_manifest_path)}
    def emit(**facts):
        print(json.dumps(dict(pid=os.getpid(), **facts)), flush=True)
    if mode == 'legacy':
        source.write_text('{"source":"hello","target":"prior"}\n')
        owner.close_family_session()
        assert open_canonical_tm_store(source) is None
        emit(legacy=True)
        owner.close_family_session()
        return
    if mode == 'produce-double':
        source.write_text('{"source":"hello","target":"prior"}\n')
        if replacement:
            assert type(service.activate_initial(source, identity.resource_id)) is MigrationReport
        before = assets()
        root_authority = owner._family._root
        create, flush = type(root_authority).create_candidate, CandidateFile.flush_content
        candidates, hits = [], []
        temporary = (journal._activation_terminal_temp_path(journal._activation_terminal_path(identity))
                     if window == 'mirror' else journal._activation_journal_temp_path(journal._activation_journal_path(identity)))
        def created(current, name, *args, **kwargs):
            value = create(current, name, *args, **kwargs)
            if current is root_authority and str(name) == temporary.name:
                candidates.append(value)
            return value
        def failed(candidate):
            result = flush(candidate)
            if candidate in candidates and len(hits) < 2:
                hits.append(True)
                raise OSError('real double flush')
            return result
        with patch.object(type(root_authority), 'create_candidate', created), patch.object(CandidateFile, 'flush_content', failed):
            if replacement:
                try:
                    service.import_snapshot(source, identity.resource_id)
                except migration.MigrationPreflightError:
                    pass
                else:
                    raise AssertionError('double failure unexpectedly succeeded')
            else:
                assert service.activate_initial(source, identity.resource_id).stage == 'RECOVERY'
        assert len(hits) == 2 and temporary.exists()
        assert owner._family._recovery_required and assets() == before
        (root / 'evidence.json').write_text(json.dumps(dict(before=before, temporary=temporary.name, digest=hashlib.sha256(temporary.read_bytes()).hexdigest())))
        emit(retained_temporary=True)
        os._exit(73)
    if mode == 'reject-double':
        evidence = json.loads((root / 'evidence.json').read_text())
        temporary = root / evidence['temporary']
        owner.close_family_session()
        try:
            open_canonical_tm_store(source)
        except ValueError:
            pass
        else:
            raise AssertionError('cold factory fell back to old or legacy authority')
        owner = store.ResourceStoreCoordinator(resource_identity=identity, canonical_store_id='store.cold.cancel')
        try:
            owner.recover_durable_activation()
        except journal.ActivationPreparationError:
            pass
        else:
            raise AssertionError('unproven temporary was accepted as recovery authority')
        assert hashlib.sha256(temporary.read_bytes()).hexdigest() == evidence['digest']
        assert assets() == evidence['before'] and owner._family._recovery_required
        emit(rejected=True)
        owner.close_family_session()
        raise SystemExit(74)
    if mode == 'produce':
        source.write_text('{"source":"hello","target":"prior"}\n')
        if replacement:
            assert type(service.activate_initial(source, identity.resource_id)) is MigrationReport
        before = assets()
        authority = owner._family._root
        backend = owner._family._backend
        create, flush = type(authority).create_candidate, CandidateFile.flush_content
        retire = type(backend).retire_existing_exclusive
        close = RetainedRetirement.close
        cancel = recovery._retire_cancelled_candidate_assets
        candidates, hits, retired, evidence = [], [], [], []
        def created(current, name, *args, **kwargs):
            value = create(current, name, *args, **kwargs)
            if current is authority and str(name) == journal._activation_journal_temp_path(journal._activation_journal_path(identity)).name:
                candidates.append(value)
            return value
        def failed(candidate):
            result = flush(candidate)
            if candidate in candidates and not hits:
                hits.append(True)
                raise OSError('real primary flush')
            return result
        def exit_here():
            assert hits == [True]
            assert owner._family._recovery_required
            assert assets() == before
            emit(retired=len(retired), window=window)
            os._exit(73)
        def cancelling(record, *, identity, caller_borrow):
            assert caller_borrow.root is authority
            caller_borrow.reprove()
            assert not caller_borrow.exists(journal._activation_journal_path(identity))
            payload, _ = caller_borrow.read_journal(journal._activation_terminal_path(identity), None)
            assert journal._parse_activation_journal_bytes(payload, expected_journal_path=record.journal_path) == record
            assert record.phase.value == 'PREPARED'
            value = dict(before=before, terminal=hashlib.sha256(payload).hexdigest(),
                         quarantine=str(journal._activation_quarantine_directory(identity, record).relative_to(root)),
                         candidates=[dict(name=path.name, device=proof.device, inode=proof.inode, size=proof.size, sha256=proof.sha256)
                                     for path, proof in ((record.candidate_stage_db_path, record.sealed_content_attestation.database),
                                                         (record.candidate_manifest_temp_path, record.sealed_content_attestation.manifest))])
            (root / 'evidence.json').write_text(json.dumps(value))
            evidence.append(value)
            if window == 'before':
                exit_here()
            return cancel(record, identity=identity, caller_borrow=caller_borrow)
        def retiring(current, *args, **kwargs):
            value = retire(current, *args, **kwargs)
            retired.append(value)
            if window == 'one' and len(retired) == 1 or window == 'two' and len(retired) == 2:
                exit_here()
            return value
        def closing(current):
            if evidence and current in retired and len(retired) == 2 and window == 'close-before':
                exit_here()
            result = close(current)
            if evidence and current in retired and len(retired) == 2 and window == 'close-after':
                exit_here()
            return result
        with patch.object(type(authority), 'create_candidate', created), patch.object(CandidateFile, 'flush_content', failed), patch.object(recovery, '_retire_cancelled_candidate_assets', cancelling), patch.object(type(backend), 'retire_existing_exclusive', retiring), patch.object(RetainedRetirement, 'close', closing):
            (service.import_snapshot if replacement else service.activate_initial)(source, identity.resource_id)
        raise AssertionError('real cancellation never reached the requested crash window')
    evidence = json.loads((root / 'evidence.json').read_text())
    if mode == 'reject':
        try:
            owner.recover_durable_activation()
        except journal.ActivationPreparationError:
            assert owner._family._recovery_required
            assert owner._family._active_count == 0 and not owner._family._operation_tokens
            emit(rejected=True)
            owner.close_family_session()
            raise SystemExit(74)
        raise AssertionError('foreign candidate accepted')
    owner.recover_durable_activation()
    assert owner.current_generation == (0 if replacement else None)
    assert not owner._family._recovery_required
    assert owner._family._active_count == 0 and not owner._family._operation_tokens
    assert assets() == evidence['before']
    assert not journal._activation_journal_path(identity).exists()
    assert hashlib.sha256(journal._activation_terminal_path(identity).read_bytes()).hexdigest() == evidence['terminal']
    files = {}
    for candidate in evidence['candidates']:
        assert not (root / candidate['name']).exists()
        path = root / evidence['quarantine'] / candidate['name']
        observed = path.stat()
        assert (observed.st_dev, observed.st_ino, observed.st_size) == (candidate['device'], candidate['inode'], candidate['size'])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == candidate['sha256']
        files[path.name] = candidate
    peer = open_canonical_tm_store(source)
    if replacement:
        assert peer.exact_records('hello')[0].target_raw == 'prior'
        assert store.SQLiteTMStore.from_coordinator(owner).exact_records('hello')[0].target_raw == 'prior'
        peer.coordinator.close_family_session()
    else:
        assert peer is None
    emit(files=files, generation=owner.current_generation)
    owner.close_family_session()
