"""Provisional memory transactions: real Host, session and closed publisher.

These probes never issue production receipts or open a capability. Production
source and provisional admission remain covered by the existing owning suites.
"""
from __future__ import annotations

from contextlib import contextmanager
import inspect
import sys
from pathlib import Path
from threading import Condition, Event, RLock, Thread
from typing import Any, Iterator
import unittest
from unittest import mock

import capability_frozen_inputs as frozen
import capability_host as host
import tm_gate_inputs as inputs
import tm_retrieval_capability as core
from tests.test_capability_host_frozen_inputs import NOW

ROOT = Path(__file__).absolute().parents[1]


class PublicationRetryTests(unittest.TestCase):
    def source(self):
        source = frozen._test_frozen_capability_source((("probe", b"retained"),), ROOT)
        self.addCleanup(source.close)
        return source

    def test_close_revokes_the_current_real_core_owner(self):
        source = self.source()
        with self.assertRaises(RuntimeError), source.input_session() as session:
            self.assertEqual(session.read_bytes("probe"), b"retained")
            owner = getattr(session, "_GateInputSession__owner")
            session._prepare_publication()
            source.close()
            with self.assertRaises(RuntimeError):
                owner._require_live()
            with self.assertRaises(RuntimeError):
                session._commit_publication(session._publication_window().plan((), None))

    def fixture(self):
        subject = host.CapabilityHost(evaluated_at_utc=NOW)
        publisher = getattr(subject, "_CapabilityHost__retrieval_publisher")
        graph = host._GateDGraphSnapshot(
            publisher, getattr(subject, "_CapabilityHost__retrieval_service"),
            getattr(subject, "_CapabilityHost__retrieval_base_manifest"),
            subject.retrieval_snapshot(), publisher.snapshot(), object(),
        )
        owner = object.__new__(host._RetrievalGateDOwner)
        setattr(owner, "_RetrievalGateDOwner__condition", Condition(RLock()))
        setattr(owner, "_RetrievalGateDOwner__host", subject)
        setattr(owner, "_RetrievalGateDOwner__owner_identity", getattr(subject, "_CapabilityHost__retrieval_owner_identity"))
        setattr(owner, "_RetrievalGateDOwner__status", host.GateDRunStatus(1, host.GateDRunState.RUNNING, None))
        setattr(owner, "_RetrievalGateDOwner__programmer_error", None)
        receipt = object.__new__(host._CoreGateDPublication)
        return subject, graph, owner, receipt

    def state(self, subject, graph, owner):
        return (
            subject.retrieval_snapshot(),
            getattr(subject, "_CapabilityHost__retrieval_operation_display"),
            subject.status_snapshot(), subject.retrieval_generation_notifications().current(),
            owner.status(), getattr(owner, "_RetrievalGateDOwner__programmer_error"),
            graph.publisher.snapshot(),
        )

    @contextmanager
    def provisional_publication(self, source, *, after_prepare=None, after_terminal=None, after_commit=None):
        # Replace only the receipt/admission front door. Everything from Host
        # preparation through original Core descriptor installation is real.
        def publish(_receipt, **kwargs):
            with source.input_session() as session:
                session.read_bytes("probe")
                def prepare(candidate):
                    callback = kwargs["prepare_publication"]
                    extra = {"_input_session": session} if "_input_session" in inspect.signature(callback).parameters else {}
                    prepared = callback(candidate, **extra)
                    if after_prepare is not None:
                        after_prepare(session)
                    session._prepare_publication()
                    if after_terminal is not None:
                        after_terminal(session)
                    return prepared
                publisher = kwargs["publisher"]
                result = publisher._validated_transition(
                    None, evaluated_at_utc=NOW, expected_current=publisher.snapshot(),
                    prepare=prepare,
                    _snapshot_descriptor=core._RETRIEVAL_CAPABILITY_SNAPSHOT_DESCRIPTOR,
                    _publication_window=session._publication_window(),
                )
                if after_commit is not None:
                    after_commit(session)
                return result
        with mock.patch.object(host._CoreGateDPublication, "publish", publish):
            yield

    def publish(self, owner, graph, receipt):
        getattr(owner, "_RetrievalGateDOwner__publish_publication")(
            epoch=1, graph=graph, publication=receipt, evaluated_at_utc=NOW,
        )

    def test_real_terminal_failure_preserves_every_host_owner_and_core_reference(self):
        source = self.source()
        subject, graph, owner, receipt = self.fixture()
        prior = self.state(subject, graph, owner)
        original = inputs._GateInputSession.terminal_reproof
        def terminal(session):
            with mock.patch.object(inputs._TestFrozenInputAdapter, "reprove", side_effect=RuntimeError("terminal")):
                return original(session)
        with self.provisional_publication(source), mock.patch.object(inputs._GateInputSession, "terminal_reproof", terminal):
            with self.assertRaises(RuntimeError):
                self.publish(owner, graph, receipt)
        self.assertEqual(self.state(subject, graph, owner), prior)
        self.assertIs(owner.status().state, host.GateDRunState.RUNNING)

    def test_cancel_after_terminal_prevents_every_install(self):
        source = self.source()
        subject, graph, owner, receipt = self.fixture()
        prior = self.state(subject, graph, owner)
        with self.provisional_publication(source, after_terminal=lambda session: source.close()):
            with self.assertRaises(RuntimeError):
                self.publish(owner, graph, receipt)
        self.assertEqual(self.state(subject, graph, owner), prior)

    def test_commit_wins_then_close_does_not_reclassify_success(self):
        source = self.source()
        subject, graph, owner, receipt = self.fixture()
        prior = self.state(subject, graph, owner)
        with self.provisional_publication(source, after_commit=lambda session: source.close()):
            self.publish(owner, graph, receipt)
        current = self.state(subject, graph, owner)
        self.assertEqual(current[0].generation, prior[0].generation + 1)
        self.assertEqual(current[3], current[0].generation)
        self.assertIs(owner.status().state, host.GateDRunState.SUCCEEDED)
        self.assertIsNot(current[-1], prior[-1])
        self.assertFalse(current[-1].context.available)
        self.assertFalse(current[-1].fuzzy_core.available)

    def test_matcher_final_identity_then_cancel_leaves_prior_handoff(self):
        from datetime import timedelta
        from tests.test_tm_gate_input_composition import _frozen_entries
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT)
        self.addCleanup(source.close)
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        prior = composition.host.matcher_snapshot()
        install = host.CapabilityHost._install_core_matcher
        def close_before_install(subject, **kwargs):
            source.close()
            return install(subject, **kwargs)
        with mock.patch.object(host.CapabilityHost, "_install_core_matcher", close_before_install):
            result = composition.matcher_validation_owner.validate_text_v1(
                generated_at_utc=NOW, valid_until_utc=NOW + timedelta(hours=1), evaluated_at_utc=NOW,
            )
        self.assertIs(result, prior)
        self.assertEqual(composition.host.matcher_generation_notifications().current(), 0)

    def test_prepare_keeps_prior_references_until_real_core_commit(self):
        source = self.source()
        subject, graph, owner, receipt = self.fixture()
        prior = self.state(subject, graph, owner)
        def check_prior(session):
            actual = (subject.retrieval_snapshot(), getattr(subject, "_CapabilityHost__retrieval_operation_display"), subject.status_snapshot(), subject.retrieval_generation_notifications().current(), owner.status(), getattr(owner, "_RetrievalGateDOwner__programmer_error"))
            self.assertTrue(all(left is right for left, right in zip(actual, prior[:-1])))
            self.assertFalse(session._publication_completed())
        with self.provisional_publication(source, after_prepare=check_prior):
            self.publish(owner, graph, receipt)

    def test_real_proof_window_exit_failure_preserves_prior(self):
        source = self.source()
        subject, graph, owner, receipt = self.fixture()
        prior = self.state(subject, graph, owner)
        original = inputs._TestFrozenInputAdapter.proof_window
        @contextmanager
        def fail_exit(adapter):
            with original(adapter):
                yield
                raise OSError("proof window cleanup")
        with self.provisional_publication(source), mock.patch.object(inputs._TestFrozenInputAdapter, "proof_window", fail_exit):
            with self.assertRaises(RuntimeError):
                self.publish(owner, graph, receipt)
        self.assertTrue(all(left is right for left, right in zip(self.state(subject, graph, owner), prior)))

    def test_first_middle_last_real_assignment_failure_rolls_back_all_references(self):
        install = core._install_publication_reference
        for failure_index in (0, 3, 6):
            with self.subTest(failure_index=failure_index):
                source = self.source()
                subject, graph, owner, receipt = self.fixture()
                prior = self.state(subject, graph, owner)
                count = 0
                def fail(assignment):
                    nonlocal count
                    install(assignment)
                    current = count
                    count += 1
                    if current == failure_index:
                        raise RuntimeError("injected actual slot installation failure")
                with self.provisional_publication(source), mock.patch.object(core, "_install_publication_reference", fail):
                    with self.assertRaises(RuntimeError):
                        self.publish(owner, graph, receipt)
                self.assertEqual(count, failure_index + 1)
                self.assertTrue(all(left is right for left, right in zip(self.state(subject, graph, owner), prior)))

    def test_host_and_owner_notification_before_after_failures_keep_prior(self):
        for target in ("host", "owner"):
            for after in (False, True):
                with self.subTest(target=target, after=after):
                    source = self.source()
                    subject, graph, owner, receipt = self.fixture()
                    prior = self.state(subject, graph, owner)
                    condition = getattr(owner, "_RetrievalGateDOwner__condition")
                    holder = host._RetrievalGenerationNotifications if target == "host" else condition
                    name = "_publish_prevalidated_locked" if target == "host" else "notify_all"
                    original = getattr(holder, name)
                    def fail(*args):
                        if after:
                            original(*args)
                        raise RuntimeError("injected notification failure")
                    with self.provisional_publication(source), mock.patch.object(holder, name, fail):
                        with self.assertRaises(RuntimeError):
                            self.publish(owner, graph, receipt)
                    self.assertTrue(all(left is right for left, right in zip(self.state(subject, graph, owner), prior)))

    def test_independent_host_and_real_query_observers_wait_for_joint_commit(self):
        from tm_contracts import TMQuery
        source = self.source()
        subject, graph, owner, receipt = self.fixture()
        installing, release, worker_done = Event(), Event(), Event()
        host_started, host_done, query_started, query_done = Event(), Event(), Event(), Event()
        observations: dict[str, Any] = {}
        errors: list[BaseException] = []
        original = core._install_publication_reference
        def paused(assignment):
            original(assignment)
            if assignment[0].__name__ == "_CapabilityHost__status":
                installing.set()
                if not release.wait(3):
                    raise AssertionError("observer coordination timeout")
        def publish():
            try:
                self.publish(owner, graph, receipt)
            except BaseException as error:
                errors.append(error)
            finally:
                worker_done.set()
        def read_host():
            host_started.set()
            observations["host"] = subject.retrieval_snapshot()
            host_done.set()
        def read_query():
            def observe(frame, event, arg):
                if event == "return" and frame.f_code.co_name == "snapshot" and frame.f_globals.get("__name__") == "tm_retrieval_capability":
                    observations["query_snapshot"] = arg
            query_started.set()
            sys.setprofile(observe)
            try:
                observations["query_report"] = graph.handoff.query_port.query((), TMQuery("probe", None, None, None, 0.6, 10, ()))
            finally:
                sys.setprofile(None)
                query_done.set()
        with self.provisional_publication(source), mock.patch.object(core, "_install_publication_reference", paused):
            worker = Thread(target=publish, daemon=True)
            worker.start()
            self.assertTrue(installing.wait(3))
            readers = (Thread(target=read_host, daemon=True), Thread(target=read_query, daemon=True))
            for reader in readers:
                reader.start()
            try:
                self.assertTrue(host_started.wait(3) and query_started.wait(3))
                self.assertFalse(host_done.wait(0.05))
                self.assertFalse(query_done.wait(0.05))
            finally:
                release.set()
            self.assertTrue(worker_done.wait(3) and host_done.wait(3) and query_done.wait(3))
            worker.join(0)
            for reader in readers:
                reader.join(0)
        self.assertEqual(errors, [])
        self.assertIs(observations["host"], subject.retrieval_snapshot())
        self.assertIs(observations["query_snapshot"], graph.publisher.snapshot())
        self.assertIsNot(observations["query_snapshot"], graph.capability)

    def test_gate_c_source_candidate_cancel_after_last_identity_keeps_entire_graph(self):
        from tests.test_capability_host_gate_c import _composition, _GENERATED_AT, _VALID_UNTIL, _EVALUATED_AT
        composition = _composition()
        gate_c = composition.retrieval_gate_c_validation_owner
        execution = getattr(gate_c, "_RetrievalGateCValidationOwner__execution")
        binding, identity = execution._capture_binding()
        release = binding.recompute(repository_root=ROOT, generated_at_utc=_GENERATED_AT, valid_until_utc=_VALID_UNTIL)
        candidate = binding.compose_service(release, evaluated_at_utc=_EVALUATED_AT)
        self.assertIsNotNone(candidate)
        publisher, service, capability, manifest = candidate
        subject = composition.host
        names = ("retrieval_checkout_identity", "retrieval_validation_binding", "retrieval_publisher", "retrieval_service", "retrieval_base_manifest", "retrieval_handoff", "retrieval_operation_display", "status")
        before = tuple(getattr(subject, "_CapabilityHost__" + name) for name in names)
        reached = []
        with inputs._compose_source_input_owner(ROOT) as input_owner, input_owner.open_session() as session:
            def revoke_after_identity(frame, event, arg):
                if event == "call" and frame.f_code.co_name == "_prepare_reference_publication" and frame.f_locals.get("self") is session:
                    reached.append(True)
                    input_owner._revoke_publication()
            previous = sys.getprofile()
            sys.setprofile(revoke_after_identity)
            try:
                with self.assertRaises(RuntimeError):
                    subject._install_gate_c_service(
                        owner_identity=getattr(subject, "_CapabilityHost__retrieval_owner_identity"),
                        checkout_identity=identity, validation_binding=binding,
                        publisher=publisher, service=service, capability=capability,
                        base_manifest=manifest, _input_session=session,
                    )
            finally:
                sys.setprofile(previous)
                # A cancelled active window must exit before its owner closes.
                with self.assertRaises(RuntimeError):
                    session.terminal_reproof()
        self.assertEqual(reached, [True])
        self.assertTrue(all(getattr(subject, "_CapabilityHost__" + name) is value for name, value in zip(names, before)))
        self.assertEqual(subject.retrieval_generation_notifications().current(), 0)


if __name__ == "__main__":
    unittest.main()
