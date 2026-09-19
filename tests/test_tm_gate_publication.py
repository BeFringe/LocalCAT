"""Publication ordering through real Core closed-snapshot transitions.

Provisional frozen cases exercise lifetime only: they never issue a production
receipt, an available capability or native frozen authority.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager, nullcontext
from threading import Event, RLock, Thread
from unittest import mock
import unittest

import tm_gate_inputs as inputs
import tm_retrieval_capability as retrieval

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)
ROOT = Path(__file__).absolute().parents[1]


class _References(metaclass=retrieval._publication_reference_type):
    __slots__ = ("handoff", "status")

    def __init__(self):
        self.handoff = object()
        self.status = object()


def _transition(publisher, session, prepare):
    def prepare_window(candidate):
        prepared = prepare(candidate)
        session._prepare_publication()
        return prepared

    return publisher._validated_transition(
        None, evaluated_at_utc=NOW, expected_current=publisher.snapshot(),
        prepare=prepare_window,
        _snapshot_descriptor=retrieval._RETRIEVAL_CAPABILITY_SNAPSHOT_DESCRIPTOR,
        _publication_window=session._publication_window(),
    )


class GatePublicationLifetimeTests(unittest.TestCase):
    def test_actual_gate_d_rejects_readonly_member_before_locked_observer_can_see_any_write(self) -> None:
        from tests import test_tm_benchmark_gate as fixtures
        import tm_benchmark_gate as gate

        publisher = fixtures._capability_publisher()
        prior_snapshot = publisher.snapshot()
        run_result = fixtures.GateDPublicationTests._run_result(fixtures._combined_bundle())
        owner = gate._gate_d_receipt_owner(run_result)
        self.addCleanup(owner.close)
        refs, readonly = _References(), timedelta(days=1)
        old, new = refs.handoff, object()
        observation_lock, prepared = RLock(), Event()
        observed, created = [], []

        def observer():
            if prepared.wait(5):
                with observation_lock:
                    observed.append((refs.handoff, publisher.snapshot()))

        thread = Thread(target=observer)
        thread.start()
        try:
            with owner.open_session() as session:
                def prepare(result):
                    try:
                        plan = session._prepare_reference_publication((
                            (_References.handoff, refs, old, new),
                            (timedelta.days, readonly, readonly.days, 2),
                        ), result)
                        created.append(plan)
                        return plan
                    finally:
                        prepared.set()

                with observation_lock:
                    with self.assertRaises((gate.BenchmarkGateDError, AttributeError)):
                        gate._publish_retrieval_capability_gate_d_prepared(
                            fixtures._base_capability_manifest(), run_result, publisher,
                            generated_at_utc=fixtures._GENERATED_AT,
                            valid_until_utc=fixtures._VALID_UNTIL,
                            evaluated_at_utc=fixtures._EVALUATED_AT,
                            prepare_publication=prepare,
                            _publication_bindings=gate._GATE_D_PUBLICATION_BINDINGS,
                            _input_session=session,
                        )
        finally:
            prepared.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [(old, prior_snapshot)])
        self.assertEqual(created, [], "unsupported member must be rejected during plan admission")
        self.assertTrue(session._publication_window().failed())
        self.assertFalse(session._publication_completed())
        with self.assertRaises(RuntimeError):
            session._prepare_publication()

    def test_memory_window_constructor_failure_exits_proof_and_cannot_reuse_session(self) -> None:
        events, failed_sessions = [], []
        original = inputs._TestFrozenInputAdapter.proof_window

        @contextmanager
        def observed_window(adapter):
            with original(adapter):
                events.append("entered")
                try:
                    yield
                finally:
                    events.append("exited")

        def fail_construct(lifetime, guards):
            failed_sessions.append(next(target for _, target, _, _ in guards if type(target) is inputs._GateInputSession))
            raise RuntimeError("memory window construction failed")

        with inputs._compose_test_frozen_input_owner((("input.txt", b"exact"),)) as owner:
            with mock.patch.object(inputs._TestFrozenInputAdapter, "proof_window", observed_window), mock.patch.object(retrieval._InputPublicationLifetime, "window", fail_construct):
                with self.assertRaisesRegex(RuntimeError, "construction failed"):
                    with owner.open_session():
                        self.fail("failed session must never be yielded")
            self.assertEqual(events, ["entered", "exited"])
            self.assertEqual(len(failed_sessions), 1)
            with self.assertRaises(RuntimeError):
                failed_sessions[0].read_bytes("input.txt")
            with self.assertRaises(RuntimeError):
                failed_sessions[0]._completed_owner()
            with owner.open_session() as fresh:
                self.assertEqual(fresh.read_bytes("input.txt"), b"exact")

    def test_reference_layout_protocol_rejects_native_bases_custom_metaclass_and_supplied_members(self) -> None:
        calls = []

        class CustomMeta(type):
            def __new__(mcls, *args, **kwargs):
                calls.append("custom metaclass")
                raise AssertionError("custom metaclass ran")

        with self.assertRaises(TypeError):
            retrieval._publication_reference_type("NativeBase", (timedelta,), {"__slots__": ("value",)})
        self.assertRaises(TypeError, retrieval._publication_reference_type,
            "CustomConstruction", (), {"__slots__": ("value",)}, metaclass=CustomMeta)
        with self.assertRaises(TypeError):
            retrieval._publication_reference_type("ClaimedSlots", (), {"__slots__": ["value"]})
        supplied = retrieval._publication_reference_type("SuppliedNative", (), {
            "__slots__": ("value",), "native": timedelta.days,
        })
        self.assertIs(type(supplied), type)
        self.assertEqual(calls, [])
        holder = supplied()
        descriptor = vars(supplied)["value"]
        descriptor.__set__(holder, object())
        prior, candidate = descriptor.__get__(holder, supplied), object()
        with inputs._compose_test_frozen_input_owner(()) as owner:
            with owner.open_session() as session:
                # The generated ordinary slot is usable; the supplied native
                # descriptor is not enrolled by the same class construction.
                plan = session._prepare_reference_publication(((descriptor, holder, prior, candidate),), None)
                session._prepare_publication()
                plan.commit()
                self.assertIs(descriptor.__get__(holder, supplied), candidate)
            with owner.open_session() as session:
                readonly = timedelta(days=1)
                with self.assertRaises(TypeError):
                    session._prepare_reference_publication(((vars(supplied)["native"], readonly, 1, 2),), None)

    def test_unsupported_storage_rejects_whole_plan_without_value_conversion_or_partial_write(self) -> None:
        class Unregistered:
            __slots__ = ("value",)

            def __init__(self):
                self.value = object()

        calls = []

        class Candidate:
            def __index__(self):
                calls.append("index")
                raise AssertionError("native conversion ran")

            def __bool__(self):
                calls.append("bool")
                raise AssertionError("native conversion ran")

        unregistered = Unregistered()
        native = timedelta(days=1, seconds=2)
        for descriptor, target, prior in (
            (Unregistered.value, unregistered, unregistered.value),
            (timedelta.days, native, native.days),
            (timedelta.seconds, native, native.seconds),
            (slice.start, slice(1, 2), 1),
        ):
            with self.subTest(descriptor=descriptor):
                refs = _References()
                old = (refs.handoff, refs.status)
                with inputs._compose_test_frozen_input_owner(()) as owner:
                    with owner.open_session() as session:
                        with self.assertRaises(TypeError):
                            session._prepare_reference_publication((
                                (_References.handoff, refs, old[0], object()),
                                (descriptor, target, prior, Candidate()),
                                (_References.status, refs, old[1], object()),
                            ), None)
                        self.assertEqual((refs.handoff, refs.status), old)
                        self.assertTrue(session._publication_window().failed())
                        with self.assertRaises(RuntimeError):
                            session._prepare_reference_publication((), None)
        self.assertEqual(calls, [])

    def test_defensive_rollback_attempts_every_slot_and_records_failure_before_restore_error(self) -> None:
        # Deliberately bypass admission for a legacy/corrupted plan. Production
        # now rejects this member before writing anything; this independently
        # exercises the secondary recovery path using the actual C setter.
        refs, readonly = _References(), timedelta(days=1)
        old = (refs.handoff, refs.status)
        publisher = retrieval.default_retrieval_capability_publisher(NOW)
        prior_snapshot = publisher.snapshot()
        validation = retrieval._validate_reference_assignments

        def admit_corrupted(assignments):
            if any(assignment[0] is timedelta.days for assignment in assignments):
                return assignments
            return validation(assignments)

        with inputs._compose_test_frozen_input_owner(()) as owner:
            with owner.open_session() as session:
                plans = []
                prior_valid_plan = session._prepare_reference_publication((), None)

                def prepare(candidate):
                    plan = session._prepare_reference_publication((
                        (_References.handoff, refs, old[0], object()),
                        (_References.status, refs, old[1], object()),
                    ), candidate)
                    object.__setattr__(plan, "_assignments", plan._assignments + ((timedelta.days, readonly, 1, 2),))
                    plans.append(plan)
                    return plan

                with mock.patch.object(retrieval, "_validate_reference_assignments", admit_corrupted):
                    with self.assertRaises(BaseExceptionGroup) as caught:
                        _transition(publisher, session, prepare)
                self.assertEqual(len(caught.exception.exceptions), 2)
                self.assertTrue(all(type(error) is AttributeError for error in caught.exception.exceptions))
                self.assertEqual((refs.handoff, refs.status), old)
                self.assertIs(publisher.snapshot(), prior_snapshot)
                self.assertTrue(session._publication_window().failed())
                self.assertFalse(session._publication_completed())
                with self.assertRaises(RuntimeError):
                    prior_valid_plan.commit()
            with self.assertRaises(RuntimeError):
                with owner.open_session():
                    self.fail("incomplete rollback must revoke the owner lifetime")

    def test_terminal_then_close_prevents_snapshot_install(self) -> None:
        publisher = retrieval.default_retrieval_capability_publisher(NOW)
        prior = publisher.snapshot()
        with inputs._compose_test_frozen_input_owner((("input.txt", b"exact"),)) as owner:
            with owner.open_session() as session:
                session.read_bytes("input.txt")

                def prepare(candidate):
                    session.terminal_reproof()
                    owner.close()
                    return candidate

                with self.assertRaises(RuntimeError):
                    publisher._validated_transition(
                        None, evaluated_at_utc=NOW, expected_current=prior,
                        prepare=prepare,
                        _snapshot_descriptor=retrieval._RETRIEVAL_CAPABILITY_SNAPSHOT_DESCRIPTOR,
                        _publication_window=session._publication_window(),
                    )
                self.assertIs(publisher.snapshot(), prior)

    def test_dispatch_thread_revokes_after_terminal_without_waiting_for_worker(self) -> None:
        ready, proceed, done = Event(), Event(), Event()
        state = {}
        publisher = retrieval.default_retrieval_capability_publisher(NOW)
        prior = publisher.snapshot()

        def worker():
            try:
                with inputs._compose_test_frozen_input_owner((("input.txt", b"exact"),)) as owner:
                    state["owner"] = owner
                    with owner.open_session() as session:
                        def prepare(candidate):
                            session.terminal_reproof()
                            ready.set()
                            if not proceed.wait(5):
                                raise AssertionError("coordination deadline")
                            return candidate
                        _transition(publisher, session, prepare)
            except BaseException as error:
                state["error"] = error
            finally:
                done.set()

        thread = Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            state["owner"]._revoke_publication()
            self.assertFalse(done.is_set())
        finally:
            proceed.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(state.get("error"), RuntimeError)
        self.assertIs(publisher.snapshot(), prior)

    def test_source_commit_is_memory_only_and_close_does_not_reverse_completion(self) -> None:
        publisher = retrieval.default_retrieval_capability_publisher(NOW)
        prior = publisher.snapshot()
        with inputs._compose_source_input_owner(ROOT) as owner:
            with owner.open_session() as session:
                session.read_bytes("tm_gate_inputs.py")
                session._prepare_publication()
                with mock.patch.object(inputs._GateInputOwner, "_require_epoch", side_effect=AssertionError("native reproof in commit")), mock.patch.object(inputs._GateInputSession, "_completed_owner", side_effect=AssertionError("native receipt reproof in commit")):
                    # Already-prepared windows remain usable only for this final swap.
                    observed = _transition(publisher, session, lambda candidate: candidate)
                owner.close()
                self.assertTrue(session._publication_completed())
            self.assertIs(publisher.snapshot(), observed)
            self.assertIsNot(observed, prior)
            assert type(observed) is retrieval.RetrievalCapabilitySnapshot
            self.assertFalse(observed.context.available)
            self.assertFalse(observed.fuzzy_core.available)

    def test_terminal_window_exit_failure_preserves_snapshot(self) -> None:
        publisher = retrieval.default_retrieval_capability_publisher(NOW)
        prior = publisher.snapshot()
        for fail_after_terminal in (False, True):
            with self.subTest(exit=fail_after_terminal):
                @contextmanager
                def failed_window(adapter):
                    yield
                    raise RuntimeError("window cleanup failed")

                with inputs._compose_test_frozen_input_owner((("input.txt", b"exact"),), terminal_results=(False,)) as owner:
                    with self.assertRaises(RuntimeError):
                        with mock.patch.object(inputs._TestFrozenInputAdapter, "proof_window", failed_window) if fail_after_terminal else nullcontext():
                            with owner.open_session() as session:
                                _transition(publisher, session, lambda candidate: candidate)
                self.assertIs(publisher.snapshot(), prior)

    def test_prebuilt_reference_plan_commits_same_snapshot_and_rolls_back_install_failure(self) -> None:
        for fail_at in (None, 0, 1, 2):
            with self.subTest(fail_at=fail_at):
                references = _References()
                old = (references.handoff, references.status)
                new = (object(), object())
                publisher = retrieval.default_retrieval_capability_publisher(NOW)
                prior = publisher.snapshot()
                with inputs._compose_test_frozen_input_owner(()) as owner:
                    with owner.open_session() as session:
                        def prepare(candidate):
                            return session._prepare_reference_publication((
                                (_References.handoff, references, old[0], new[0]),
                                (_References.status, references, old[1], new[1]),
                            ), candidate)

                        original = retrieval._install_publication_reference
                        seen = []

                        def install(assignment):
                            original(assignment)
                            seen.append(assignment)
                            if len(seen) - 1 == fail_at:
                                raise RuntimeError("injected install failure")

                        with mock.patch.object(retrieval, "_install_publication_reference", side_effect=install):
                            if fail_at is not None:
                                with self.assertRaisesRegex(RuntimeError, "injected"):
                                    _transition(publisher, session, prepare)
                                self.assertIs(publisher.snapshot(), prior)
                                self.assertEqual((references.handoff, references.status), old)
                                self.assertFalse(session._publication_completed())
                            else:
                                result = _transition(publisher, session, prepare)
                                self.assertIs(result, publisher.snapshot())
                                self.assertEqual((references.handoff, references.status), new)
                                self.assertTrue(session._publication_completed())

    def test_foreign_expired_aborted_and_repeated_publications_rejected(self) -> None:
        with inputs._compose_test_frozen_input_owner(()) as owner, inputs._compose_test_frozen_input_owner(()) as other:
            with owner.open_session() as session:
                plan = session._prepare_reference_publication((), object())
                session._prepare_publication()
                with other.open_session() as foreign:
                    foreign._prepare_publication()
                    with self.assertRaises((ValueError, RuntimeError)):
                        foreign._commit_publication(plan)
                plan.commit()
                with self.assertRaises(RuntimeError):
                    plan.commit()
            with self.assertRaises(RuntimeError):
                plan.commit()
            with owner.open_session() as next_session:
                aborted = next_session._prepare_reference_publication((), object())
                next_session._prepare_publication()
                next_session._abort()
                with self.assertRaises(RuntimeError):
                    aborted.commit()

    def test_reference_plan_rejects_callback_and_custom_descriptor(self) -> None:
        for value in (lambda: None, ((property(lambda _: None), object(), None, None),)):
            with inputs._compose_test_frozen_input_owner(()) as owner:
                with owner.open_session() as session:
                    with self.assertRaises(TypeError):
                        session._prepare_reference_publication(value, None)

    def test_source_window_requires_completion_and_rejects_late_owner_epoch_drift(self) -> None:
        with inputs._compose_source_input_owner(ROOT) as owner:
            with owner.open_session() as session:
                plan = session._prepare_reference_publication((), object())
                with self.assertRaises(RuntimeError):
                    plan.commit()
                session._prepare_publication()
                object.__setattr__(owner, "_GateInputOwner__epoch", object())
                with self.assertRaises(RuntimeError):
                    plan.commit()
                self.assertFalse(session._publication_completed())

    def test_shared_owner_accepts_fresh_window_after_completed_publication(self) -> None:
        with inputs._compose_source_input_owner(ROOT) as owner:
            with owner.open_session() as first:
                first.read_bytes("tm_gate_inputs.py")
                plan = first._prepare_reference_publication((), object())
                first._prepare_publication()
                plan.commit()
            with owner.open_session() as second:
                self.assertTrue(second.read_bytes("tm_gate_inputs.py"))
                self.assertIs(second._production_owner(), owner)

    def test_commit_wins_before_concurrent_revoke_and_outer_exit_stays_completed(self) -> None:
        publisher = retrieval.default_retrieval_capability_publisher(NOW)
        threads = []
        revoked = Event()
        with inputs._compose_test_frozen_input_owner(()) as owner:
            with owner.open_session() as session:
                install = retrieval._install_publication_reference

                def cancel():
                    owner._revoke_publication()
                    revoked.set()

                def commit_then_contend(assignment):
                    # Test-only scheduling at the actual builtin setter: the
                    # production path has no thread launch/wait/callback here.
                    thread = Thread(target=cancel)
                    threads.append(thread)
                    thread.start()
                    install(assignment)

                with mock.patch.object(retrieval, "_install_publication_reference", side_effect=commit_then_contend):
                    result = _transition(publisher, session, lambda candidate: candidate)
                for thread in threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                self.assertTrue(revoked.is_set())
                self.assertTrue(session._publication_completed())
                self.assertIs(result, publisher.snapshot())
            self.assertTrue(session._publication_completed())

    def test_scheduler_revokes_while_terminal_is_still_pending(self) -> None:
        ready, resume = Event(), Event()
        state = {}
        publisher = retrieval.default_retrieval_capability_publisher(NOW)
        prior = publisher.snapshot()

        @contextmanager
        def queued_terminal(adapter):
            yield
            ready.set()
            if not resume.wait(5):
                raise AssertionError("coordination deadline")

        def worker():
            try:
                with inputs._compose_test_frozen_input_owner(()) as owner:
                    state["owner"] = owner
                    with owner.open_session() as session:
                        _transition(publisher, session, lambda candidate: candidate)
            except BaseException as error:
                state["error"] = error

        with mock.patch.object(inputs._TestFrozenInputAdapter, "proof_window", queued_terminal):
            thread = Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(ready.wait(5))
                state["owner"]._revoke_publication()
                self.assertTrue(thread.is_alive())
            finally:
                resume.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(state.get("error"), RuntimeError)
        self.assertIs(publisher.snapshot(), prior)

    def test_retained_source_close_failure_is_before_snapshot_and_revokes_retry(self) -> None:
        publisher = retrieval.default_retrieval_capability_publisher(NOW)
        prior = publisher.snapshot()
        owner = inputs._retain_source_input_owner(ROOT)
        try:
            with owner.open_session() as session:
                session.read_bytes("tm_gate_inputs.py")
                plan = session._prepare_reference_publication((), object())
                close = inputs._SourceInputAdapter.close

                def failed_close(adapter):
                    close(adapter)
                    raise RuntimeError("native close failed")

                def prepare(candidate):
                    # A downstream prebuilt plan may already have finished its
                    # shared window. The public one-shot owner must still close.
                    session._prepare_publication()
                    session._prepare_publication(close_owner=True)
                    return candidate

                with mock.patch.object(inputs._SourceInputAdapter, "close", failed_close):
                    with self.assertRaisesRegex(RuntimeError, "native close"):
                        _transition(publisher, session, prepare)
                with self.assertRaises(RuntimeError):
                    session._prepare_publication()
                with self.assertRaises(RuntimeError):
                    plan.commit()
                self.assertIs(publisher.snapshot(), prior)
        finally:
            owner.close()

    def test_reference_cancel_after_final_identity_check_installs_nothing(self) -> None:
        references = _References()
        old = references.handoff
        with inputs._compose_test_frozen_input_owner(()) as owner:
            with owner.open_session() as session:
                # Matcher/Gate C consumers can preconstruct their complete
                # graph, then consume this same closed memory-only plan.
                plan = session._prepare_reference_publication((
                    (_References.handoff, references, old, object()),
                ), object())
                session._prepare_publication()
                owner._revoke_publication()
                with mock.patch.object(retrieval, "_install_publication_reference") as install:
                    with self.assertRaises(RuntimeError):
                        plan.commit()
                    install.assert_not_called()
                self.assertIs(references.handoff, old)

    def test_public_gate_d_closes_retained_source_before_publishing(self) -> None:
        # Existing synthetic measurement fixtures exercise the real source
        # publication entry; this does not qualify benchmark performance.
        from tests import test_tm_benchmark_gate as fixtures
        import tm_benchmark_gate as gate

        publisher = fixtures._capability_publisher()
        prior = publisher.snapshot()
        result = fixtures.GateDPublicationTests._run_result(fixtures._combined_bundle())
        original = inputs._SourceInputAdapter.close
        closed = []

        def fail_close(adapter):
            original(adapter)
            closed.append(adapter)
            raise RuntimeError("public retained owner close failed")

        with mock.patch.object(inputs._SourceInputAdapter, "close", fail_close):
            with self.assertRaises(gate.BenchmarkGateDError):
                gate.publish_retrieval_capability_gate_d(
                    fixtures._base_capability_manifest(), result, publisher,
                    generated_at_utc=fixtures._GENERATED_AT,
                    valid_until_utc=fixtures._VALID_UNTIL,
                    evaluated_at_utc=fixtures._EVALUATED_AT,
                )
        self.assertEqual(len(closed), 1)
        self.assertIs(publisher.snapshot(), prior)

    def test_observers_wait_until_all_slots_rollback_under_original_locks(self) -> None:
        references = _References()
        old = (references.handoff, references.status)
        publisher = retrieval.default_retrieval_capability_publisher(NOW)
        prior = publisher.snapshot()
        host_lock = RLock()
        observed, threads = [], []

        def host_observer():
            with host_lock:
                observed.append(("host", (references.handoff, references.status)))

        def query_observer():
            observed.append(("query", publisher.snapshot()))

        with inputs._compose_test_frozen_input_owner(()) as owner:
            with owner.open_session() as session:
                def prepare(candidate):
                    return session._prepare_reference_publication((
                        (_References.handoff, references, old[0], object()),
                        (_References.status, references, old[1], object()),
                    ), candidate)

                install = retrieval._install_publication_reference

                def fault(assignment):
                    install(assignment)
                    if assignment[1] is publisher:
                        for target in (host_observer, query_observer):
                            thread = Thread(target=target)
                            threads.append(thread)
                            thread.start()
                        raise RuntimeError("last setter failed after assignment")

                with host_lock, mock.patch.object(retrieval, "_install_publication_reference", side_effect=fault):
                    with self.assertRaisesRegex(RuntimeError, "last setter"):
                        _transition(publisher, session, prepare)
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertCountEqual(observed, [("host", old), ("query", prior)])


if __name__ == "__main__":
    unittest.main()
