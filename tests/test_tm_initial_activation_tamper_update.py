"""Task 2.5 first-activation tamper, concurrency, and update regressions.

These tests exercise only public migration/facade behavior.  They do not
construct sealed capabilities or repair durable activation facts from the
test side.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Any
import unittest
from unittest.mock import patch

import tm_migration
import tm_sqlite_store
from tm_contracts import MigrationFailure, MigrationReport, SourceBindingState
from tm_engine import SourceUnit, TMEngine
from tm_stage_sealer import StageSealError
from tests.test_tm_initial_activation_contract import (
    SOURCE_BYTES,
    _coordinator,
    _identity,
    _service,
)


def _assert_no_partial_initial_authority(
    testcase: unittest.TestCase,
    *,
    identity: Any,
    coordinator: Any,
) -> None:
    testcase.assertEqual(coordinator.state, "READY")
    testcase.assertIsNone(coordinator.current_generation)
    testcase.assertIsNone(coordinator.active_store_path)
    testcase.assertIsNone(coordinator.durable_activation_phase)
    testcase.assertFalse(identity.canonical_sidecar_path.exists())
    testcase.assertFalse(identity.snapshot_manifest_path.exists())


class InitialActivationTamperAndConcurrencyTests(unittest.TestCase):
    def test_completion_only_append_proof_never_opens_writable_or_repair_seam(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                published = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                engine = TMEngine(
                    str(identity.configured_jsonl_path),
                    update=True,
                )
            self.assertIs(type(published), MigrationReport)
            self.assertTrue(
                engine.save_record(
                    SourceUnit(id="append", text="canonical append"),
                    "canonical target",
                )
            )
            lock_path = identity.canonical_sidecar_path.with_name(
                f".{identity.canonical_sidecar_path.name}."
                "localcat-initial-activation.lock"
            )
            replacement = root / "foreign-completed-append.lock"
            replacement.write_bytes(b"foreign completed append lock")
            os.replace(replacement, lock_path)

            def database_family_snapshot() -> tuple[tuple[str, bytes], ...]:
                basename = identity.canonical_sidecar_path.name
                return tuple(
                    sorted(
                        (
                            path.name,
                            path.read_bytes(),
                        )
                        for path in root.iterdir()
                        if path.is_file()
                        and (
                            path.name == basename
                            or path.name.startswith(f"{basename}-")
                        )
                    )
                )

            before = database_family_snapshot()
            fresh = _coordinator(identity)
            real_open = tm_sqlite_store._open_configured_connection
            real_recover_indexes = (
                tm_sqlite_store._recover_activation_indexes
            )
            with (
                patch.object(
                    tm_sqlite_store,
                    "_open_configured_connection",
                    wraps=real_open,
                ) as writable_open,
                patch.object(
                    tm_sqlite_store,
                    "_recover_activation_indexes",
                    wraps=real_recover_indexes,
                ) as repair_index,
                patch("tm_sqlite_store._probe_fts5", return_value=False),
            ):
                recovered = fresh.rehydrate_completed_initial_authority()

            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.action, "COMPLETED")
            self.assertEqual(recovered.generation, 0)
            writable_open.assert_not_called()
            repair_index.assert_not_called()
            self.assertEqual(database_family_snapshot(), before)
            self.assertEqual(fresh.current_generation, 0)

    def test_completion_only_final_reproof_failure_publishes_no_partial_view(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                published = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(published), MigrationReport)
            marker_path = tm_migration._activation_lineage_marker_path(
                identity
            )
            original_marker = marker_path.read_bytes()
            fresh = _coordinator(identity)
            real_lstat_journal = (
                tm_sqlite_store._lstat_activation_journal_identity
            )
            lstat_calls = 0

            def swap_marker_before_final_journal_reproof(path: Path):
                nonlocal lstat_calls
                lstat_calls += 1
                if lstat_calls == 2:
                    replacement = root / "foreign-marker-same-bytes"
                    replacement.write_bytes(original_marker)
                    os.replace(replacement, marker_path)
                return real_lstat_journal(path)

            with (
                patch.object(
                    tm_sqlite_store,
                    "_lstat_activation_journal_identity",
                    side_effect=swap_marker_before_final_journal_reproof,
                ),
                patch("tm_sqlite_store._probe_fts5", return_value=False),
            ):
                recovered = fresh.rehydrate_completed_initial_authority()

            self.assertIsNone(recovered)
            self.assertEqual(fresh.state, "READY")
            self.assertIsNone(fresh.current_generation)
            self.assertIsNone(fresh.active_store_path)
            self.assertEqual(fresh.canonical_store_id, "store.primary")

    def test_foreign_lock_hot_journal_fails_without_sqlite_recovery_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                published = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(published), MigrationReport)
            child_script = "\n".join(
                (
                    "import os, sqlite3, sys",
                    "connection = sqlite3.connect(sys.argv[1])",
                    "connection.execute('PRAGMA journal_mode=DELETE')",
                    "connection.execute('PRAGMA cache_size=1')",
                    "connection.execute('PRAGMA cache_spill=ON')",
                    "connection.execute('BEGIN IMMEDIATE')",
                    "connection.execute('UPDATE tm_record SET target_raw = ? WHERE record_id = 1', ('x' * 5000000,))",
                    "os._exit(23)",
                )
            )
            crashed = subprocess.run(
                (
                    sys.executable,
                    "-c",
                    child_script,
                    str(identity.canonical_sidecar_path),
                ),
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(crashed.returncode, 23)
            self.assertEqual(crashed.stdout, "")
            self.assertEqual(crashed.stderr, "")
            hot_journal = Path(
                f"{identity.canonical_sidecar_path}-journal"
            )
            self.assertTrue(hot_journal.exists())
            lock_path = identity.canonical_sidecar_path.with_name(
                f".{identity.canonical_sidecar_path.name}."
                "localcat-initial-activation.lock"
            )
            replacement = root / "foreign-hot-journal.lock"
            replacement.write_bytes(b"foreign hot journal lock")
            os.replace(replacement, lock_path)

            def exact_tree_snapshot() -> tuple[
                tuple[str, int, int, int, int, bytes], ...
            ]:
                entries: list[tuple[str, int, int, int, int, bytes]] = []
                for path in sorted(root.iterdir()):
                    observed = os.lstat(path)
                    payload = path.read_bytes() if path.is_file() else b""
                    entries.append(
                        (
                            path.name,
                            observed.st_dev,
                            observed.st_ino,
                            observed.st_mtime_ns,
                            observed.st_size,
                            hashlib.sha256(payload).digest(),
                        )
                    )
                return tuple(entries)

            before = exact_tree_snapshot()
            fresh_coordinator = _coordinator(identity)
            fresh_service = _service(identity, fresh_coordinator)
            real_connect = tm_sqlite_store.sqlite3.connect
            with patch.object(
                tm_sqlite_store.sqlite3,
                "connect",
                wraps=real_connect,
            ) as sqlite_connect:
                outcome = fresh_service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertIs(type(outcome), MigrationFailure)
            assert isinstance(outcome, MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertTrue(outcome.canonical_authority_ambiguous)
            sqlite_connect.assert_not_called()
            self.assertEqual(exact_tree_snapshot(), before)
            self.assertIsNone(fresh_coordinator.current_generation)
            self.assertIsNone(fresh_coordinator.active_store_path)

    def test_hot_journal_after_precheck_uses_immutable_zero_mutation_open(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                published = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(published), MigrationReport)
            lock_path = identity.canonical_sidecar_path.with_name(
                f".{identity.canonical_sidecar_path.name}."
                "localcat-initial-activation.lock"
            )
            replacement = root / "foreign-toctou.lock"
            replacement.write_bytes(b"foreign TOCTOU lock")
            os.replace(replacement, lock_path)

            def exact_tree_snapshot() -> tuple[
                tuple[str, int, int, int, int, bytes], ...
            ]:
                entries: list[tuple[str, int, int, int, int, bytes]] = []
                for path in sorted(root.iterdir()):
                    observed = os.lstat(path)
                    payload = path.read_bytes() if path.is_file() else b""
                    entries.append(
                        (
                            path.name,
                            observed.st_dev,
                            observed.st_ino,
                            observed.st_mtime_ns,
                            observed.st_size,
                            hashlib.sha256(payload).digest(),
                        )
                    )
                return tuple(entries)

            crash_script = "\n".join(
                (
                    "import os, sqlite3, sys",
                    "connection = sqlite3.connect(sys.argv[1])",
                    "connection.execute('PRAGMA journal_mode=DELETE')",
                    "connection.execute('PRAGMA cache_size=1')",
                    "connection.execute('PRAGMA cache_spill=ON')",
                    "connection.execute('BEGIN IMMEDIATE')",
                    "connection.execute('UPDATE tm_record SET target_raw = ? WHERE record_id = 1', ('y' * 5000000,))",
                    "os._exit(29)",
                )
            )
            real_sidecar_probe = (
                tm_sqlite_store
                ._completed_authority_sqlite_sidecar_present
            )
            probe_calls = 0
            injected_snapshot: list[
                tuple[tuple[str, int, int, int, int, bytes], ...]
            ] = []

            def inject_after_clean_precheck(database_path: Path) -> bool:
                nonlocal probe_calls
                probe_calls += 1
                observed = real_sidecar_probe(database_path)
                if probe_calls == 2:
                    self.assertFalse(observed)
                    crashed = subprocess.run(
                        (
                            sys.executable,
                            "-c",
                            crash_script,
                            str(identity.canonical_sidecar_path),
                        ),
                        cwd=Path(__file__).resolve().parents[1],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(crashed.returncode, 29)
                    self.assertEqual(crashed.stdout, "")
                    self.assertEqual(crashed.stderr, "")
                    self.assertTrue(
                        Path(
                            f"{identity.canonical_sidecar_path}-journal"
                        ).exists()
                    )
                    injected_snapshot.append(exact_tree_snapshot())
                return observed

            fresh_coordinator = _coordinator(identity)
            fresh_service = _service(identity, fresh_coordinator)
            real_connect = tm_sqlite_store.sqlite3.connect
            with (
                patch.object(
                    tm_sqlite_store,
                    "_completed_authority_sqlite_sidecar_present",
                    side_effect=inject_after_clean_precheck,
                ),
                patch.object(
                    tm_sqlite_store.sqlite3,
                    "connect",
                    wraps=real_connect,
                ) as sqlite_connect,
            ):
                outcome = fresh_service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertIs(type(outcome), MigrationFailure)
            assert isinstance(outcome, MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertTrue(outcome.canonical_authority_ambiguous)
            self.assertEqual(len(injected_snapshot), 1)
            self.assertEqual(exact_tree_snapshot(), injected_snapshot[0])
            self.assertGreaterEqual(probe_calls, 2)
            self.assertGreaterEqual(sqlite_connect.call_count, 1)
            for connect_call in sqlite_connect.call_args_list:
                database = connect_call.args[0]
                if type(database) is str and "mode=ro" in database:
                    self.assertIn("immutable=1", database)
            self.assertIsNone(fresh_coordinator.current_generation)
            self.assertIsNone(fresh_coordinator.active_store_path)

    def test_hot_journal_after_completed_probe_never_enters_runtime_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                published = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(published), MigrationReport)
            lock_path = identity.canonical_sidecar_path.with_name(
                f".{identity.canonical_sidecar_path.name}."
                "localcat-initial-activation.lock"
            )
            replacement = root / "foreign-post-proof.lock"
            replacement.write_bytes(b"foreign post-proof lock")
            os.replace(replacement, lock_path)

            def exact_tree_snapshot() -> tuple[
                tuple[str, int, int, int, int, bytes], ...
            ]:
                entries: list[tuple[str, int, int, int, int, bytes]] = []
                for path in sorted(root.iterdir()):
                    observed = os.lstat(path)
                    payload = path.read_bytes() if path.is_file() else b""
                    entries.append(
                        (
                            path.name,
                            observed.st_dev,
                            observed.st_ino,
                            observed.st_mtime_ns,
                            observed.st_size,
                            hashlib.sha256(payload).digest(),
                        )
                    )
                return tuple(entries)

            crash_script = "\n".join(
                (
                    "import os, sqlite3, sys",
                    "connection = sqlite3.connect(sys.argv[1])",
                    "connection.execute('PRAGMA journal_mode=DELETE')",
                    "connection.execute('PRAGMA cache_size=1')",
                    "connection.execute('PRAGMA cache_spill=ON')",
                    "connection.execute('BEGIN IMMEDIATE')",
                    "connection.execute('UPDATE tm_record SET target_raw = ? WHERE record_id = 1', ('z' * 5000000,))",
                    "os._exit(31)",
                )
            )
            real_completion_probe = (
                tm_sqlite_store
                ._rehydrate_completed_initial_authority_only
            )
            probe_calls = 0
            snapshot_after_injection: list[
                tuple[tuple[str, int, int, int, int, bytes], ...]
            ] = []
            connect_count_after_injection: list[int] = []
            real_connect = tm_sqlite_store.sqlite3.connect

            def prove_then_create_hot_journal(port: Any):
                nonlocal probe_calls
                result = real_completion_probe(port)
                probe_calls += 1
                if probe_calls == 1:
                    crashed = subprocess.run(
                        (
                            sys.executable,
                            "-c",
                            crash_script,
                            str(identity.canonical_sidecar_path),
                        ),
                        cwd=Path(__file__).resolve().parents[1],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(crashed.returncode, 31)
                    self.assertEqual(crashed.stdout, "")
                    self.assertEqual(crashed.stderr, "")
                    self.assertTrue(
                        Path(
                            f"{identity.canonical_sidecar_path}-journal"
                        ).exists()
                    )
                    snapshot_after_injection.append(exact_tree_snapshot())
                    connect_count_after_injection.append(
                        sqlite_connect.call_count
                    )
                return result

            fresh_coordinator = _coordinator(identity)
            fresh_service = _service(identity, fresh_coordinator)
            with (
                patch.object(
                    tm_sqlite_store.sqlite3,
                    "connect",
                    wraps=real_connect,
                ) as sqlite_connect,
                patch.object(
                    tm_sqlite_store,
                    "_rehydrate_completed_initial_authority_only",
                    side_effect=prove_then_create_hot_journal,
                ),
                patch("tm_sqlite_store._probe_fts5", return_value=False),
            ):
                outcome = fresh_service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertIs(type(outcome), MigrationFailure)
            assert isinstance(outcome, MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertTrue(outcome.canonical_authority_published)
            self.assertFalse(outcome.canonical_authority_ambiguous)
            self.assertEqual(outcome.active_generation, 0)
            self.assertEqual(len(snapshot_after_injection), 1)
            self.assertEqual(
                exact_tree_snapshot(),
                snapshot_after_injection[0],
            )
            self.assertEqual(
                sqlite_connect.call_count,
                connect_count_after_injection[0],
            )
            self.assertIsNone(fresh_coordinator.current_generation)
            self.assertIsNone(fresh_coordinator.active_store_path)

    def test_lock_initialization_is_atomic_before_resource_ownership(
        self,
    ) -> None:
        """A peer never observes the creator's uninitialized lock inode."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinators = (_coordinator(identity), _coordinator(identity))
            services = tuple(
                _service(identity, coordinator)
                for coordinator in coordinators
            )
            real_flock_module = __import__("fcntl")
            real_flock = real_flock_module.flock
            first_regular_waiting = threading.Event()
            second_regular_locked = threading.Event()
            regular_lock_guard = threading.Lock()
            regular_lock_calls = 0
            build_guard = threading.Lock()
            build_calls = 0
            real_builds = tuple(
                service._build_stage for service in services
            )
            outcomes: list[object | None] = [None, None]

            def schedule_regular_resource_lock(
                descriptor: int,
                operation: int,
            ) -> None:
                nonlocal regular_lock_calls
                observed = os.fstat(descriptor)
                if (
                    operation == real_flock_module.LOCK_EX
                    and stat.S_ISREG(observed.st_mode)
                ):
                    with regular_lock_guard:
                        regular_lock_calls += 1
                        call_number = regular_lock_calls
                    if call_number == 1:
                        first_regular_waiting.set()
                        if not second_regular_locked.wait(timeout=10.0):
                            raise TimeoutError(
                                "peer did not reach the resource lock"
                            )
                        real_flock(descriptor, operation)
                        return
                    if call_number == 2:
                        real_flock(descriptor, operation)
                        second_regular_locked.set()
                        return
                real_flock(descriptor, operation)

            def count_build(
                service_index: int,
                *args: Any,
                **kwargs: Any,
            ):
                nonlocal build_calls
                with build_guard:
                    build_calls += 1
                return real_builds[service_index](*args, **kwargs)

            def activate(service_index: int) -> None:
                try:
                    outcomes[service_index] = services[
                        service_index
                    ].activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                except BaseException as error:  # asserted below
                    outcomes[service_index] = error

            with (
                patch.object(
                    real_flock_module,
                    "flock",
                    side_effect=schedule_regular_resource_lock,
                ),
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    services[0],
                    "_build_stage",
                    side_effect=lambda *args, **kwargs: count_build(
                        0,
                        *args,
                        **kwargs,
                    ),
                ),
                patch.object(
                    services[1],
                    "_build_stage",
                    side_effect=lambda *args, **kwargs: count_build(
                        1,
                        *args,
                        **kwargs,
                    ),
                ),
            ):
                creator = threading.Thread(
                    target=activate,
                    args=(0,),
                    daemon=True,
                )
                peer = threading.Thread(
                    target=activate,
                    args=(1,),
                    daemon=True,
                )
                creator.start()
                self.assertTrue(first_regular_waiting.wait(timeout=10.0))
                peer.start()
                creator.join(timeout=20.0)
                peer.join(timeout=20.0)

            self.assertFalse(creator.is_alive())
            self.assertFalse(peer.is_alive())
            reports = [
                outcome
                for outcome in outcomes
                if type(outcome) is MigrationReport
            ]
            self.assertEqual(len(reports), 2, outcomes)
            self.assertEqual(reports[0], reports[1])
            self.assertEqual(build_calls, 1)
            self.assertEqual(
                tuple(item.current_generation for item in coordinators),
                (0, 0),
            )

    def test_prepared_owner_cannot_be_cancelled_before_peer_reservation(
        self,
    ) -> None:
        """Every durable recovery waits behind the physical transaction."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinators = (_coordinator(identity), _coordinator(identity))
            services = tuple(
                _service(identity, coordinator)
                for coordinator in coordinators
            )
            real_publish_prepared = (
                coordinators[0].publish_prepared_activation
            )
            real_builds = tuple(
                service._build_stage for service in services
            )
            prepared_durable = threading.Event()
            resume_owner = threading.Event()
            peer_finished = threading.Event()
            build_guard = threading.Lock()
            build_calls = 0
            outcomes: list[object | None] = [None, None]

            def publish_prepared_then_pause(preparation: Any):
                handle = real_publish_prepared(preparation)
                prepared_durable.set()
                if not resume_owner.wait(timeout=15.0):
                    raise TimeoutError("owner was not resumed")
                return handle

            def count_build(
                service_index: int,
                *args: Any,
                **kwargs: Any,
            ):
                nonlocal build_calls
                with build_guard:
                    build_calls += 1
                return real_builds[service_index](*args, **kwargs)

            def activate(service_index: int) -> None:
                try:
                    outcomes[service_index] = services[
                        service_index
                    ].activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                except BaseException as error:  # asserted below
                    outcomes[service_index] = error
                finally:
                    if service_index == 1:
                        peer_finished.set()

            owner = threading.Thread(target=activate, args=(0,), daemon=True)
            peer = threading.Thread(target=activate, args=(1,), daemon=True)
            peer_returned_before_owner = False
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    coordinators[0],
                    "publish_prepared_activation",
                    side_effect=publish_prepared_then_pause,
                ),
                patch.object(
                    services[0],
                    "_build_stage",
                    side_effect=lambda *args, **kwargs: count_build(
                        0,
                        *args,
                        **kwargs,
                    ),
                ),
                patch.object(
                    services[1],
                    "_build_stage",
                    side_effect=lambda *args, **kwargs: count_build(
                        1,
                        *args,
                        **kwargs,
                    ),
                ),
            ):
                owner.start()
                self.assertTrue(prepared_durable.wait(timeout=15.0))
                peer.start()
                peer_returned_before_owner = peer_finished.wait(timeout=1.0)
                resume_owner.set()
                owner.join(timeout=20.0)
                peer.join(timeout=20.0)

            self.assertFalse(peer_returned_before_owner, outcomes)
            self.assertFalse(owner.is_alive())
            self.assertFalse(peer.is_alive())
            reports = [
                outcome
                for outcome in outcomes
                if type(outcome) is MigrationReport
            ]
            self.assertEqual(len(reports), 2, outcomes)
            self.assertEqual(reports[0], reports[1])
            self.assertEqual(build_calls, 1)
            self.assertEqual(
                tuple(item.current_generation for item in coordinators),
                (0, 0),
            )

    def test_foreign_lock_never_recovers_or_cancels_prepared_owner(
        self,
    ) -> None:
        """Lock failure may inspect completed authority, never pending facts."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            owner_coordinator = _coordinator(identity)
            peer_coordinator = _coordinator(identity)
            owner_service = _service(identity, owner_coordinator)
            peer_service = _service(identity, peer_coordinator)
            real_publish_prepared = (
                owner_coordinator.publish_prepared_activation
            )
            prepared_durable = threading.Event()
            resume_owner = threading.Event()
            owner_outcome: list[object | None] = [None]
            lock_path = identity.canonical_sidecar_path.with_name(
                f".{identity.canonical_sidecar_path.name}."
                "localcat-initial-activation.lock"
            )
            journal_path = tm_migration._activation_journal_path(identity)
            foreign_bytes = b"foreign lock while owner is PREPARED"

            def publish_prepared_then_pause(preparation: Any):
                handle = real_publish_prepared(preparation)
                prepared_durable.set()
                if not resume_owner.wait(timeout=15.0):
                    raise TimeoutError("owner was not resumed")
                return handle

            def activate_owner() -> None:
                try:
                    owner_outcome[0] = owner_service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                except BaseException as error:  # asserted below
                    owner_outcome[0] = error

            owner = threading.Thread(target=activate_owner, daemon=True)
            peer_outcome: object | None = None
            journal_unchanged = False
            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    owner_coordinator,
                    "publish_prepared_activation",
                    side_effect=publish_prepared_then_pause,
                ),
            ):
                owner.start()
                self.assertTrue(prepared_durable.wait(timeout=15.0))
                journal_before = journal_path.read_bytes()
                journal_identity_before = os.lstat(journal_path)
                replacement = root / "foreign-pending.lock"
                replacement.write_bytes(foreign_bytes)
                os.replace(replacement, lock_path)
                try:
                    peer_outcome = peer_service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                    journal_identity_after = os.lstat(journal_path)
                    journal_unchanged = (
                        journal_path.read_bytes() == journal_before
                        and (
                            journal_identity_after.st_dev,
                            journal_identity_after.st_ino,
                        )
                        == (
                            journal_identity_before.st_dev,
                            journal_identity_before.st_ino,
                        )
                    )
                finally:
                    resume_owner.set()
                    owner.join(timeout=20.0)

            self.assertFalse(owner.is_alive())
            self.assertIs(type(peer_outcome), MigrationFailure)
            assert isinstance(peer_outcome, MigrationFailure)
            self.assertEqual(
                peer_outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertTrue(peer_outcome.canonical_authority_ambiguous)
            self.assertTrue(journal_unchanged)
            self.assertEqual(lock_path.read_bytes(), foreign_bytes)
            self.assertIs(type(owner_outcome[0]), MigrationFailure)

    def test_empty_crash_lock_is_durable_fail_stop_not_bootstrapped(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            lock_path = identity.canonical_sidecar_path.with_name(
                f".{identity.canonical_sidecar_path.name}."
                "localcat-initial-activation.lock"
            )
            child_script = "\n".join(
                (
                    "from pathlib import Path",
                    "import os, sys",
                    "from tm_contracts import CanonicalResourceIdentity",
                    "import tm_migration",
                    "source = Path(sys.argv[1]).resolve()",
                    "identity = CanonicalResourceIdentity.from_configured_jsonl(sys.argv[2], source)",
                    "def crash_before_payload(_descriptor, _payload):",
                    "    print('EMPTY_LOCK_VISIBLE', flush=True)",
                    "    os._exit(17)",
                    "tm_migration._InitialActivationResourceReservation._write_new_payload = staticmethod(crash_before_payload)",
                    "tm_migration._InitialActivationResourceReservation.acquire(identity)",
                )
            )
            crashed = subprocess.run(
                (
                    sys.executable,
                    "-c",
                    child_script,
                    str(identity.configured_jsonl_path),
                    identity.resource_id,
                ),
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(crashed.returncode, 17)
            self.assertEqual(crashed.stdout.strip(), "EMPTY_LOCK_VISIBLE")
            self.assertEqual(crashed.stderr, "")
            self.assertTrue(lock_path.exists())
            self.assertEqual(lock_path.read_bytes(), b"")

            outcome = service.activate_initial(
                identity.configured_jsonl_path,
                identity.resource_id,
            )

            self.assertIs(type(outcome), MigrationFailure)
            assert isinstance(outcome, MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertTrue(outcome.canonical_authority_ambiguous)
            self.assertEqual(lock_path.read_bytes(), b"")
            _assert_no_partial_initial_authority(
                self,
                identity=identity,
                coordinator=coordinator,
            )

    def test_parent_bootstrap_unlock_programmer_error_closes_all_handles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            real_flock_module = __import__("fcntl")
            real_flock = real_flock_module.flock
            real_bind = tm_migration._ExportParentHandle.bind
            real_write = (
                tm_migration._InitialActivationResourceReservation
                ._write_new_payload
            )
            parent_descriptors: list[int] = []
            lock_descriptors: list[int] = []

            def bind_and_capture(destination: Path):
                parent = real_bind(destination)
                parent_descriptors.append(parent.descriptor)
                return parent

            def write_and_capture(descriptor: int, payload: bytes) -> None:
                lock_descriptors.append(descriptor)
                real_write(descriptor, payload)

            def fail_parent_unlock(
                descriptor: int,
                operation: int,
            ) -> None:
                if (
                    operation == real_flock_module.LOCK_UN
                    and stat.S_ISDIR(os.fstat(descriptor).st_mode)
                ):
                    raise TypeError("forced parent unlock programmer error")
                real_flock(descriptor, operation)

            reservation = None
            try:
                with (
                    patch.object(
                        tm_migration._ExportParentHandle,
                        "bind",
                        side_effect=bind_and_capture,
                    ),
                    patch.object(
                        tm_migration._InitialActivationResourceReservation,
                        "_write_new_payload",
                        side_effect=write_and_capture,
                    ),
                    patch.object(
                        real_flock_module,
                        "flock",
                        side_effect=fail_parent_unlock,
                    ),
                ):
                    with self.assertRaisesRegex(
                        TypeError,
                        "forced parent unlock programmer error",
                    ):
                        reservation = (
                            tm_migration
                            ._InitialActivationResourceReservation.acquire(
                                identity
                            )
                        )
            finally:
                if reservation is not None:
                    reservation.release()

            self.assertEqual(len(parent_descriptors), 1)
            self.assertEqual(len(lock_descriptors), 1)
            for descriptor in (
                parent_descriptors[0],
                lock_descriptors[0],
            ):
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_blocked_same_resource_peer_does_not_hold_parent_bootstrap(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_a = _identity(
                root,
                resource_id="tm.a",
                basename="a.jsonl",
            )
            identity_b = _identity(
                root,
                resource_id="tm.b",
                basename="b.jsonl",
            )
            holder = (
                tm_migration._InitialActivationResourceReservation.acquire(
                    identity_a
                )
            )
            real_flock_module = __import__("fcntl")
            real_flock = real_flock_module.flock
            expected_a_payload = (
                tm_migration._InitialActivationResourceReservation._payload(
                    identity_a
                )
            )
            waiter_reached_resource_lock = threading.Event()
            waiter_done = threading.Event()
            unrelated_done = threading.Event()
            waiter_outcome: list[object | None] = [None]
            unrelated_outcome: list[object | None] = [None]
            coordinator_b = _coordinator(identity_b)
            service_b = _service(identity_b, coordinator_b)

            def observe_resource_wait(
                descriptor: int,
                operation: int,
            ) -> None:
                observed = os.fstat(descriptor)
                if (
                    operation == real_flock_module.LOCK_EX
                    and stat.S_ISREG(observed.st_mode)
                    and os.pread(
                        descriptor,
                        len(expected_a_payload) + 1,
                        0,
                    )
                    == expected_a_payload
                ):
                    waiter_reached_resource_lock.set()
                real_flock(descriptor, operation)

            def wait_for_a() -> None:
                try:
                    reservation = (
                        tm_migration._InitialActivationResourceReservation
                        .acquire(identity_a)
                    )
                    waiter_outcome[0] = reservation
                    reservation.release()
                except BaseException as error:  # asserted below
                    waiter_outcome[0] = error
                finally:
                    waiter_done.set()

            def activate_b() -> None:
                try:
                    unrelated_outcome[0] = service_b.activate_initial(
                        identity_b.configured_jsonl_path,
                        identity_b.resource_id,
                    )
                except BaseException as error:  # asserted below
                    unrelated_outcome[0] = error
                finally:
                    unrelated_done.set()

            waiter = threading.Thread(target=wait_for_a, daemon=True)
            unrelated = threading.Thread(target=activate_b, daemon=True)
            completed_before_release = False
            try:
                with (
                    patch.object(
                        real_flock_module,
                        "flock",
                        side_effect=observe_resource_wait,
                    ),
                    patch("tm_sqlite_store._probe_fts5", return_value=False),
                ):
                    waiter.start()
                    self.assertTrue(
                        waiter_reached_resource_lock.wait(timeout=10.0)
                    )
                    unrelated.start()
                    completed_before_release = unrelated_done.wait(
                        timeout=5.0
                    )
            finally:
                holder.release()
                waiter.join(timeout=10.0)
                if unrelated.ident is not None:
                    unrelated.join(timeout=10.0)

            self.assertTrue(completed_before_release)
            self.assertFalse(waiter.is_alive())
            self.assertFalse(unrelated.is_alive())
            self.assertTrue(waiter_done.is_set())
            self.assertIs(
                type(waiter_outcome[0]),
                tm_migration._InitialActivationResourceReservation,
            )
            self.assertIs(type(unrelated_outcome[0]), MigrationReport)
            self.assertEqual(coordinator_b.current_generation, 0)

    def test_foreign_resource_lock_artifacts_fail_closed_without_mutation(
        self,
    ) -> None:
        for artifact_kind in ("regular", "hardlink", "symlink"):
            with self.subTest(artifact_kind=artifact_kind):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity = _identity(root)
                    coordinator = _coordinator(identity)
                    service = _service(identity, coordinator)
                    lock_path = identity.canonical_sidecar_path.with_name(
                        f".{identity.canonical_sidecar_path.name}."
                        "localcat-initial-activation.lock"
                    )
                    foreign = root / f"foreign-{artifact_kind}.lock"
                    foreign_bytes = b"foreign resource lock bytes"
                    if artifact_kind == "regular":
                        lock_path.write_bytes(foreign_bytes)
                    elif artifact_kind == "hardlink":
                        foreign.write_bytes(foreign_bytes)
                        os.link(foreign, lock_path)
                    else:
                        foreign.write_bytes(foreign_bytes)
                        lock_path.symlink_to(foreign.name)

                    outcome = service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )

                    self.assertIs(type(outcome), MigrationFailure)
                    assert isinstance(outcome, MigrationFailure)
                    self.assertEqual(
                        outcome.error_code,
                        "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
                    )
                    self.assertTrue(outcome.canonical_authority_ambiguous)
                    if artifact_kind == "symlink":
                        self.assertTrue(lock_path.is_symlink())
                        self.assertEqual(lock_path.readlink(), Path(foreign.name))
                    else:
                        self.assertEqual(lock_path.read_bytes(), foreign_bytes)
                    if artifact_kind != "regular":
                        self.assertEqual(foreign.read_bytes(), foreign_bytes)
                    _assert_no_partial_initial_authority(
                        self,
                        identity=identity,
                        coordinator=coordinator,
                    )

    def test_resource_lock_programmer_error_crosses_public_seam(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            with patch.object(
                tm_migration._InitialActivationResourceReservation,
                "acquire",
                side_effect=TypeError("programmer defect"),
            ):
                with self.assertRaisesRegex(TypeError, "programmer defect"):
                    service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )

    def test_real_resource_reservation_release_closes_every_descriptor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            reservation = (
                tm_migration._InitialActivationResourceReservation.acquire(
                    identity
                )
            )
            lock_descriptor = reservation._descriptor
            parent_descriptor = reservation._parent.descriptor

            reservation.release()
            reservation.release()

            for descriptor in (lock_descriptor, parent_descriptor):
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_unlock_programmer_error_propagates_after_forced_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            reservation = (
                tm_migration._InitialActivationResourceReservation.acquire(
                    identity
                )
            )
            lock_descriptor = reservation._descriptor
            parent_descriptor = reservation._parent.descriptor

            def close_if_open(descriptor: int) -> None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

            self.addCleanup(close_if_open, lock_descriptor)
            self.addCleanup(close_if_open, parent_descriptor)
            with patch.object(
                reservation._fcntl,
                "flock",
                side_effect=TypeError("forced unlock programmer error"),
            ):
                with self.assertRaisesRegex(
                    TypeError,
                    "forced unlock programmer error",
                ):
                    reservation.release()

            for descriptor in (lock_descriptor, parent_descriptor):
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_acquire_programmer_error_closes_lock_and_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            captured_lock: list[int] = []
            captured_parent: list[int] = []
            real_bind = tm_migration._ExportParentHandle.bind

            def bind_and_capture(destination: Path):
                handle = real_bind(destination)
                captured_parent.append(handle.descriptor)
                return handle

            def fail_after_lock(descriptor: int, _payload: bytes) -> None:
                captured_lock.append(descriptor)
                raise TypeError("forced acquire programmer error")

            with (
                patch.object(
                    tm_migration._ExportParentHandle,
                    "bind",
                    side_effect=bind_and_capture,
                ),
                patch.object(
                    tm_migration._InitialActivationResourceReservation,
                    "_write_new_payload",
                    side_effect=fail_after_lock,
                ),
            ):
                with self.assertRaisesRegex(
                    TypeError,
                    "forced acquire programmer error",
                ):
                    tm_migration._InitialActivationResourceReservation.acquire(
                        identity
                    )

            self.assertEqual(len(captured_lock), 1)
            self.assertEqual(len(captured_parent), 1)
            for descriptor in (captured_lock[0], captured_parent[0]):
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_unsupported_lock_platform_fails_closed_before_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity = _identity(Path(temporary))
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            with patch.object(tm_migration.sys, "platform", "unsupported"):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(outcome), MigrationFailure)
            assert isinstance(outcome, MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertTrue(outcome.canonical_authority_ambiguous)
            _assert_no_partial_initial_authority(
                self,
                identity=identity,
                coordinator=coordinator,
            )

    def test_published_probe_precedes_foreign_lock_without_runtime_handoff(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                published = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(published), MigrationReport)
            lock_path = identity.canonical_sidecar_path.with_name(
                f".{identity.canonical_sidecar_path.name}."
                "localcat-initial-activation.lock"
            )
            foreign = root / "foreign-published.lock"
            foreign_bytes = b"foreign after durable generation zero"
            foreign.write_bytes(foreign_bytes)
            os.replace(foreign, lock_path)

            restarted_coordinator = _coordinator(identity)
            restarted = _service(identity, restarted_coordinator)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                recovered = restarted.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertIs(type(recovered), MigrationFailure)
            assert isinstance(recovered, MigrationFailure)
            self.assertEqual(
                recovered.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertTrue(recovered.canonical_authority_published)
            self.assertFalse(recovered.canonical_authority_ambiguous)
            self.assertEqual(recovered.active_generation, 0)
            self.assertIsNone(restarted_coordinator.current_generation)
            self.assertIsNone(restarted_coordinator.active_store_path)
            self.assertEqual(lock_path.read_bytes(), foreign_bytes)

    def test_lock_swap_after_publication_reports_unavailable_published_gen0(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            real_publish = coordinator.publish_activation
            lock_path = identity.canonical_sidecar_path.with_name(
                f".{identity.canonical_sidecar_path.name}."
                "localcat-initial-activation.lock"
            )
            foreign_bytes = b"foreign lock after publication"

            def publish_then_replace_lock(*args: Any, **kwargs: Any) -> int:
                generation = real_publish(*args, **kwargs)
                replacement = root / "foreign-post-publish.lock"
                replacement.write_bytes(foreign_bytes)
                os.replace(replacement, lock_path)
                return generation

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    coordinator,
                    "publish_activation",
                    side_effect=publish_then_replace_lock,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertIs(type(outcome), MigrationFailure)
            assert isinstance(outcome, MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertTrue(outcome.canonical_authority_published)
            self.assertFalse(outcome.canonical_authority_ambiguous)
            self.assertEqual(outcome.active_generation, 0)
            self.assertEqual(coordinator.current_generation, 0)
            self.assertEqual(lock_path.read_bytes(), foreign_bytes)

    def test_different_resources_do_not_share_physical_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identities = (
                _identity(root, resource_id="tm.a", basename="a.jsonl"),
                _identity(root, resource_id="tm.b", basename="b.jsonl"),
            )
            coordinators = tuple(_coordinator(item) for item in identities)
            services = tuple(
                _service(identity, coordinator)
                for identity, coordinator in zip(
                    identities,
                    coordinators,
                    strict=True,
                )
            )
            real_builds = tuple(service._build_stage for service in services)
            build_barrier = threading.Barrier(2)
            outcomes: list[object | None] = [None, None]

            def concurrent_build(
                service_index: int,
                *args: Any,
                **kwargs: Any,
            ):
                build_barrier.wait(timeout=10.0)
                return real_builds[service_index](*args, **kwargs)

            def activate(service_index: int) -> None:
                identity = identities[service_index]
                try:
                    outcomes[service_index] = services[
                        service_index
                    ].activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                except BaseException as error:  # asserted below
                    outcomes[service_index] = error

            with (
                patch("tm_sqlite_store._probe_fts5", return_value=False),
                patch.object(
                    services[0],
                    "_build_stage",
                    side_effect=lambda *args, **kwargs: concurrent_build(
                        0,
                        *args,
                        **kwargs,
                    ),
                ),
                patch.object(
                    services[1],
                    "_build_stage",
                    side_effect=lambda *args, **kwargs: concurrent_build(
                        1,
                        *args,
                        **kwargs,
                    ),
                ),
            ):
                workers = tuple(
                    threading.Thread(
                        target=activate,
                        args=(service_index,),
                        daemon=True,
                    )
                    for service_index in range(2)
                )
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=20.0)

            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertTrue(
                all(type(outcome) is MigrationReport for outcome in outcomes),
                outcomes,
            )
            self.assertEqual(
                tuple(item.current_generation for item in coordinators),
                (0, 0),
            )

    def test_process_death_orphan_stage_is_preserved_and_fresh_retry_succeeds(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            source_digest = hashlib.sha256(SOURCE_BYTES).hexdigest()
            residue = tm_migration._deterministic_stage_ref(
                identity,
                source_digest=source_digest,
                stage_prefix="migration",
                path_salt=f"initial-{'a' * 32}",
            ).staged_db_path
            child_script = "\n".join(
                (
                    "from pathlib import Path",
                    "import hashlib, os, sqlite3, sys",
                    "from tm_contracts import CanonicalResourceIdentity",
                    "import tm_migration",
                    "source = Path(sys.argv[1]).resolve()",
                    "identity = CanonicalResourceIdentity.from_configured_jsonl(sys.argv[2], source)",
                    "reservation = tm_migration._InitialActivationResourceReservation.acquire(identity)",
                    "digest = hashlib.sha256(source.read_bytes()).hexdigest()",
                    "stage = tm_migration._deterministic_stage_ref(identity, source_digest=digest, stage_prefix='migration', path_salt='initial-' + ('a' * 32))",
                    "connection = sqlite3.connect(stage.staged_db_path)",
                    "connection.execute('PRAGMA journal_mode=DELETE')",
                    "connection.execute('CREATE TABLE residue (value TEXT NOT NULL)')",
                    "connection.execute('INSERT INTO residue(value) VALUES (?)', ('before-crash',))",
                    "connection.commit()",
                    "connection.execute('BEGIN IMMEDIATE')",
                    "connection.execute('UPDATE residue SET value = ?', ('crash-owned-residue-' + ('x' * 1000000),))",
                    "assert Path(str(stage.staged_db_path) + '-journal').exists()",
                    "print('READY', flush=True)",
                    "sys.stdin.buffer.read(1)",
                    "os._exit(0)",
                )
            )
            process = subprocess.Popen(
                (
                    sys.executable,
                    "-c",
                    child_script,
                    str(identity.configured_jsonl_path),
                    identity.resource_id,
                ),
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "READY")
            residue_journal = Path(f"{residue}-journal")
            self.assertTrue(residue.exists())
            self.assertTrue(residue_journal.exists())
            residue_identity = os.lstat(residue)
            journal_identity = os.lstat(residue_journal)
            residue_bytes = residue.read_bytes()
            journal_bytes = residue_journal.read_bytes()
            outcome: list[object | None] = [None]
            finished = threading.Event()

            def activate_after_child_crash() -> None:
                try:
                    outcome[0] = service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                except BaseException as error:  # asserted below
                    outcome[0] = error
                finally:
                    finished.set()

            worker = threading.Thread(
                target=activate_after_child_crash,
                daemon=True,
            )
            worker.start()
            self.assertFalse(finished.wait(timeout=0.25))
            assert process.stdin is not None
            process.stdin.close()
            self.assertEqual(process.wait(timeout=10.0), 0)
            process.stdout.close()
            assert process.stderr is not None
            child_stderr = process.stderr.read()
            process.stderr.close()
            self.assertEqual(child_stderr, "")
            worker.join(timeout=10.0)
            self.assertFalse(worker.is_alive())
            self.assertIs(type(outcome[0]), MigrationReport)
            self.assertEqual(coordinator.current_generation, 0)
            self.assertEqual(residue.read_bytes(), residue_bytes)
            self.assertEqual(residue_journal.read_bytes(), journal_bytes)
            self.assertEqual(os.lstat(residue).st_ino, residue_identity.st_ino)
            self.assertEqual(
                os.lstat(residue_journal).st_ino,
                journal_identity.st_ino,
            )

            reopened = TMEngine(str(identity.configured_jsonl_path))
            self.assertIsNotNone(reopened.canonical_store)
            match = reopened.query_exact("same")
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.target, "winner")

    def test_concurrent_public_calls_linearize_to_one_generation(self) -> None:
        """One build wins; the peer recovers the same generation."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            services = (
                _service(identity, coordinator),
                _service(identity, coordinator),
            )
            real_build = tuple(service._build_stage for service in services)
            build_guard = threading.Lock()
            second_build_entered = threading.Event()
            build_calls = 0

            def align_overlapping_builds(
                service_index: int,
                *args: Any,
                **kwargs: Any,
            ):
                nonlocal build_calls
                with build_guard:
                    build_calls += 1
                    call_number = build_calls
                if call_number == 1:
                    # An implementation that owns the whole transaction with
                    # one coordinator lock will time out here and proceed;
                    # the peer cannot enter the builder until the winner is
                    # fully published.  The old split transaction lets both
                    # builders enter immediately and deterministically
                    # reproduces the stale/cancellation race.
                    second_build_entered.wait(timeout=1.0)
                else:
                    second_build_entered.set()
                return real_build[service_index](*args, **kwargs)

            outcomes: list[object] = []
            outcome_lock = threading.Lock()

            def activate(service_index: int) -> None:
                try:
                    result: object = services[service_index].activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                except BaseException as error:  # asserted below
                    result = error
                with outcome_lock:
                    outcomes.append(result)

            with (
                patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ),
                patch.object(
                    services[0],
                    "_build_stage",
                    side_effect=lambda *args, **kwargs: align_overlapping_builds(
                        0,
                        *args,
                        **kwargs,
                    ),
                ),
                patch.object(
                    services[1],
                    "_build_stage",
                    side_effect=lambda *args, **kwargs: align_overlapping_builds(
                        1,
                        *args,
                        **kwargs,
                    ),
                ),
            ):
                workers = tuple(
                    threading.Thread(
                        target=activate,
                        args=(service_index,),
                        daemon=True,
                    )
                    for service_index in range(2)
                )
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=20.0)

            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(len(outcomes), 2)
            reports = [item for item in outcomes if type(item) is MigrationReport]
            self.assertEqual(len(reports), 2, outcomes)
            self.assertEqual(reports[0], reports[1])
            self.assertTrue(
                all(report.activated_generation == 0 for report in reports)
            )
            self.assertEqual(build_calls, 1)
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(coordinator.current_generation, 0)
            self.assertEqual(
                coordinator.durable_activation_phase,
                "GENERATION_PUBLISHED",
            )
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=False,
            ):
                match = TMEngine(
                    str(identity.configured_jsonl_path),
                    update=False,
                ).query_exact("same")
            assert match is not None
            self.assertEqual(match.target, "winner")

    def test_stage_database_and_manifest_content_changes_fail_before_authority(
        self,
    ) -> None:
        for changed_asset, expected_code in (
            ("database", "SEALER.RECORD_MISMATCH"),
            ("manifest", "SEALER.MANIFEST_INVALID"),
        ):
            with self.subTest(changed_asset=changed_asset):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity = _identity(root)
                    coordinator = _coordinator(identity)
                    service = _service(identity, coordinator)
                    real_build = service._build_stage

                    def build_then_change(*args: Any, **kwargs: Any):
                        stage, database_identity, manifest_identity = real_build(
                            *args,
                            **kwargs,
                        )
                        if changed_asset == "database":
                            connection = sqlite3.connect(stage.staged_db_path)
                            try:
                                with connection:
                                    connection.execute(
                                        "UPDATE tm_record SET target_raw = ? "
                                        "WHERE record_id = 1",
                                        ("tampered",),
                                    )
                            finally:
                                connection.close()
                        else:
                            stage.manifest_temp_path.write_bytes(b"{}")
                        return stage, database_identity, manifest_identity

                    with patch.object(
                        service,
                        "_build_stage",
                        side_effect=build_then_change,
                    ):
                        outcome = service.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )

                    self.assertIs(type(outcome), MigrationFailure)
                    assert isinstance(outcome, MigrationFailure)
                    self.assertEqual(outcome.stage, "SEAL")
                    self.assertEqual(outcome.error_code, expected_code)
                    self.assertFalse(outcome.canonical_authority_published)
                    self.assertFalse(outcome.canonical_authority_ambiguous)
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        SOURCE_BYTES,
                    )
                    _assert_no_partial_initial_authority(
                        self,
                        identity=identity,
                        coordinator=coordinator,
                    )

    def test_peer_cannot_bypass_ambiguous_first_attempt(
        self,
    ) -> None:
        """A peer cannot publish after another attempt leaves ambiguity."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            services = (
                _service(identity, coordinator),
                _service(identity, coordinator),
            )
            real_build = services[0]._build_stage
            ambiguous_build_entered = threading.Event()
            foreign_path: Path | None = None
            foreign_bytes: bytes | None = None

            def build_then_replace(*args: Any, **kwargs: Any):
                nonlocal foreign_path, foreign_bytes
                stage, database_identity, manifest_identity = real_build(
                    *args,
                    **kwargs,
                )
                victim = stage.staged_db_path
                foreign_bytes = victim.read_bytes()
                replacement = victim.with_name(f"{victim.name}.foreign")
                replacement.write_bytes(foreign_bytes)
                os.replace(replacement, victim)
                foreign_path = victim
                ambiguous_build_entered.set()
                return stage, database_identity, manifest_identity

            outcomes: list[object | None] = [None, None]

            def activate(service_index: int) -> None:
                try:
                    outcomes[service_index] = services[
                        service_index
                    ].activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                except BaseException as error:  # asserted below
                    outcomes[service_index] = error

            with (
                patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ),
                patch.object(
                    services[0],
                    "_build_stage",
                    side_effect=build_then_replace,
                ),
            ):
                workers = (
                    threading.Thread(
                        target=activate,
                        args=(0,),
                        daemon=True,
                    ),
                    threading.Thread(
                        target=activate,
                        args=(1,),
                        daemon=True,
                    ),
                )
                workers[0].start()
                self.assertTrue(
                    ambiguous_build_entered.wait(timeout=10.0)
                )
                workers[1].start()
                for worker in workers:
                    worker.join(timeout=20.0)

            self.assertFalse(any(worker.is_alive() for worker in workers))
            for outcome in outcomes:
                self.assertIs(type(outcome), MigrationFailure, outcomes)
                assert isinstance(outcome, MigrationFailure)
                self.assertEqual(
                    outcome.error_code,
                    "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
                )
                self.assertFalse(outcome.canonical_authority_published)
                self.assertTrue(outcome.canonical_authority_ambiguous)
            assert foreign_path is not None
            assert foreign_bytes is not None
            self.assertEqual(foreign_path.read_bytes(), foreign_bytes)
            _assert_no_partial_initial_authority(
                self,
                identity=identity,
                coordinator=coordinator,
            )

    def test_second_coordinator_fresh_retries_after_unpublished_tamper(
        self,
    ) -> None:
        """The failed owner stays closed; a later owner uses a fresh nonce."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinators = (_coordinator(identity), _coordinator(identity))
            services = tuple(
                _service(identity, coordinator)
                for coordinator in coordinators
            )
            real_build = tuple(service._build_stage for service in services)
            build_guard = threading.Lock()
            build_calls = 0
            foreign_path: Path | None = None
            foreign_bytes: bytes | None = None

            def first_build_becomes_foreign(
                service_index: int,
                *args: Any,
                **kwargs: Any,
            ):
                nonlocal build_calls, foreign_path, foreign_bytes
                stage, database_identity, manifest_identity = real_build[
                    service_index
                ](*args, **kwargs)
                with build_guard:
                    build_calls += 1
                    call_number = build_calls
                if call_number == 1:
                    victim = stage.staged_db_path
                    foreign_bytes = victim.read_bytes()
                    replacement = victim.with_name(
                        f"{victim.name}.foreign"
                    )
                    replacement.write_bytes(foreign_bytes)
                    os.replace(replacement, victim)
                    foreign_path = victim
                return stage, database_identity, manifest_identity

            outcomes: list[object | None] = [None, None]

            def activate(service_index: int) -> None:
                try:
                    outcomes[service_index] = services[
                        service_index
                    ].activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                except BaseException as error:  # asserted below
                    outcomes[service_index] = error

            with (
                patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ),
                patch.object(
                    services[0],
                    "_build_stage",
                    side_effect=lambda *args, **kwargs: (
                        first_build_becomes_foreign(0, *args, **kwargs)
                    ),
                ),
                patch.object(
                    services[1],
                    "_build_stage",
                    side_effect=lambda *args, **kwargs: (
                        first_build_becomes_foreign(1, *args, **kwargs)
                    ),
                ),
            ):
                workers = tuple(
                    threading.Thread(
                        target=activate,
                        args=(service_index,),
                        daemon=True,
                    )
                    for service_index in range(2)
                )
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=20.0)

            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(build_calls, 2)
            self.assertEqual(
                sum(type(outcome) is MigrationFailure for outcome in outcomes),
                1,
            )
            self.assertEqual(
                sum(type(outcome) is MigrationReport for outcome in outcomes),
                1,
            )
            for outcome in outcomes:
                if type(outcome) is MigrationFailure:
                    assert isinstance(outcome, MigrationFailure)
                    self.assertEqual(
                        outcome.error_code,
                        "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
                    )
                    self.assertTrue(outcome.canonical_authority_ambiguous)
                else:
                    self.assertIs(type(outcome), MigrationReport, outcomes)
            assert foreign_path is not None
            assert foreign_bytes is not None
            self.assertEqual(foreign_path.read_bytes(), foreign_bytes)
            self.assertEqual(
                sum(
                    coordinator.current_generation == 0
                    for coordinator in coordinators
                ),
                1,
            )

    def test_same_byte_foreign_stage_inode_fails_current_attempt_then_retries(
        self,
    ) -> None:
        for changed_asset in ("database", "manifest"):
            with self.subTest(changed_asset=changed_asset):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity = _identity(root)
                    coordinator = _coordinator(identity)
                    service = _service(identity, coordinator)
                    real_build = service._build_stage
                    foreign_path: Path | None = None
                    foreign_bytes: bytes | None = None

                    def build_then_replace(*args: Any, **kwargs: Any):
                        nonlocal foreign_path, foreign_bytes
                        stage, database_identity, manifest_identity = real_build(
                            *args,
                            **kwargs,
                        )
                        victim = (
                            stage.staged_db_path
                            if changed_asset == "database"
                            else stage.manifest_temp_path
                        )
                        foreign_bytes = victim.read_bytes()
                        replacement = victim.with_name(f"{victim.name}.foreign")
                        replacement.write_bytes(foreign_bytes)
                        os.replace(replacement, victim)
                        foreign_path = victim
                        return stage, database_identity, manifest_identity

                    with patch.object(
                        service,
                        "_build_stage",
                        side_effect=build_then_replace,
                    ):
                        outcome = service.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )

                    self.assertIs(type(outcome), MigrationFailure)
                    assert isinstance(outcome, MigrationFailure)
                    self.assertEqual(
                        outcome.error_code,
                        "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
                    )
                    self.assertFalse(outcome.canonical_authority_published)
                    self.assertTrue(outcome.canonical_authority_ambiguous)
                    assert foreign_path is not None
                    assert foreign_bytes is not None
                    self.assertEqual(foreign_path.read_bytes(), foreign_bytes)
                    _assert_no_partial_initial_authority(
                        self,
                        identity=identity,
                        coordinator=coordinator,
                    )

                    restarted_coordinator = _coordinator(identity)
                    restarted = _service(identity, restarted_coordinator)
                    restarted_retry = restarted.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                    self.assertIs(type(restarted_retry), MigrationReport)
                    self.assertEqual(restarted_coordinator.current_generation, 0)
                    self.assertEqual(foreign_path.read_bytes(), foreign_bytes)

                    same_process_retry = service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                    self.assertIs(type(same_process_retry), MigrationReport)

                    # The residue family is bound to identity A.  An exact
                    # first activation for identity B in the same directory
                    # must remain independent and must not touch A's bytes.
                    peer_identity = _identity(
                        root,
                        resource_id="tm.peer",
                        basename="peer.jsonl",
                    )
                    peer_coordinator = _coordinator(peer_identity)
                    peer = _service(peer_identity, peer_coordinator)
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=False,
                    ):
                        peer_outcome = peer.activate_initial(
                            peer_identity.configured_jsonl_path,
                            peer_identity.resource_id,
                        )
                    self.assertIs(type(peer_outcome), MigrationReport)
                    self.assertEqual(peer_coordinator.current_generation, 0)
                    self.assertEqual(foreign_path.read_bytes(), foreign_bytes)

    def test_restart_preserves_alias_residue_and_uses_fresh_nonce(
        self,
    ) -> None:
        for residue_kind in ("symlink", "hardlink"):
            with self.subTest(residue_kind=residue_kind):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity = _identity(root)
                    coordinator = _coordinator(identity)
                    service = _service(identity, coordinator)
                    real_build = service._build_stage
                    residue_path: Path | None = None
                    foreign_target: Path | None = None
                    foreign_bytes: bytes | None = None

                    def build_then_alias(*args: Any, **kwargs: Any):
                        nonlocal residue_path, foreign_target, foreign_bytes
                        stage, database_identity, manifest_identity = real_build(
                            *args,
                            **kwargs,
                        )
                        victim = stage.staged_db_path
                        foreign_bytes = victim.read_bytes()
                        foreign_target = victim.with_name(
                            f"{victim.name}.{residue_kind}-foreign"
                        )
                        if residue_kind == "symlink":
                            victim.rename(foreign_target)
                            victim.symlink_to(foreign_target.name)
                        else:
                            os.link(victim, foreign_target)
                        residue_path = victim
                        return stage, database_identity, manifest_identity

                    with patch.object(
                        service,
                        "_build_stage",
                        side_effect=build_then_alias,
                    ):
                        first = service.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )

                    self.assertIs(type(first), MigrationFailure)
                    assert isinstance(first, MigrationFailure)
                    self.assertTrue(first.canonical_authority_ambiguous)
                    assert residue_path is not None
                    assert foreign_target is not None
                    assert foreign_bytes is not None
                    if residue_kind == "symlink":
                        self.assertTrue(residue_path.is_symlink())
                        self.assertEqual(
                            residue_path.readlink(),
                            Path(foreign_target.name),
                        )
                    else:
                        self.assertFalse(residue_path.is_symlink())
                        self.assertEqual(os.lstat(residue_path).st_nlink, 2)
                    self.assertEqual(foreign_target.read_bytes(), foreign_bytes)

                    restarted_coordinator = _coordinator(identity)
                    restarted = _service(identity, restarted_coordinator)
                    retry = restarted.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                    self.assertIs(type(retry), MigrationReport)
                    self.assertEqual(restarted_coordinator.current_generation, 0)
                    self.assertEqual(foreign_target.read_bytes(), foreign_bytes)
                    if residue_kind == "symlink":
                        self.assertTrue(residue_path.is_symlink())
                    else:
                        self.assertEqual(os.lstat(residue_path).st_nlink, 2)
                    reopened = TMEngine(str(identity.configured_jsonl_path))
                    self.assertIsNotNone(reopened.canonical_store)


class CanonicalUpdatePreservationTests(unittest.TestCase):
    def test_failed_import_and_rebuild_keep_reopenable_lkg_without_jsonl_fallback(
        self,
    ) -> None:
        for operation_name in ("import_snapshot", "rebuild_from_snapshot"):
            with self.subTest(operation=operation_name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity = _identity(root)
                    coordinator = _coordinator(identity)
                    service = _service(identity, coordinator)
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=False,
                    ):
                        initial = service.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )
                    self.assertIs(type(initial), MigrationReport)
                    self.assertEqual(coordinator.current_generation, 0)

                    divergent_bytes = (
                        b'{"source":"same","target":"jsonl replacement"}\n'
                        b'{"source":"legacy-only","target":"must not query"}\n'
                    )
                    identity.configured_jsonl_path.write_bytes(divergent_bytes)
                    operation = getattr(service, operation_name)
                    with patch.object(
                        coordinator,
                        "_seal_stage",
                        side_effect=StageSealError("SEALER.FORCED_TAMPER"),
                    ):
                        failed = operation(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )

                    self.assertIs(type(failed), MigrationFailure)
                    assert isinstance(failed, MigrationFailure)
                    self.assertEqual(failed.error_code, "SEALER.FORCED_TAMPER")
                    self.assertEqual(failed.active_generation, 0)
                    self.assertEqual(coordinator.current_generation, 0)
                    self.assertEqual(coordinator.state, "READY")
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        divergent_bytes,
                    )

                    # This is the target business API for the LKG claim: a
                    # fresh facade must reopen the canonical lineage, report
                    # divergence, and never answer from the changed JSONL.
                    # Keep the same frozen physical index profile used by
                    # the activation fixture; this test is about LKG
                    # authority, not a runtime capability-profile change.
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=False,
                    ):
                        reopened = TMEngine(
                            str(identity.configured_jsonl_path),
                            update=False,
                        )
                    self.assertTrue(reopened.canonical_active)
                    match = reopened.query_exact("same")
                    assert match is not None
                    self.assertEqual(match.target, "winner")
                    self.assertIsNone(reopened.query_exact("legacy-only"))
                    reopened_store = reopened._store
                    assert reopened_store is not None
                    self.assertIs(
                        reopened_store.source_binding_monitor.observe().state,
                        SourceBindingState.SOURCE_DIVERGED,
                    )
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        divergent_bytes,
                    )


if __name__ == "__main__":
    unittest.main()
