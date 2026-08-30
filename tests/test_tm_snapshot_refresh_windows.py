"""Windows bound configured-snapshot refresh acceptance for WA-06 5.13a."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from platform_fs_contracts import (
    PendingPublication,
    PlatformFileError,
    PlatformFileErrorCode,
)
from platform_fs_windows import WindowsPlatformAdapter
from tm_contracts import (
    CanonicalResourceIdentity,
    ExportFailure,
    ExportReport,
    MigrationReport,
    SnapshotManifest,
    SourceBindingState,
    contract_from_json,
    snapshot_receipt_digest,
)
from tm_migration import (
    TMMigrationService,
    _export_artifact_paths,
    _InitialActivationResourceReservation,
)
from tm_sqlite_store import (
    ActivationPreparationError,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
)
from tests.test_tm_initial_activation_windows import (
    _identity as _activation_identity,
    _remove_long_quarantine,
    _service as _activation_service,
)
from tests.test_tm_snapshot_refresh import (
    _PRIOR_JSONL,
    _bind_current_snapshot,
    _binding_row,
    _draft,
    _ledger_rows,
    _pair,
    _prepared_store,
    _refresh_rows,
    _status_for,
)
from tests.test_tm_portable_replacement_owner_windows import (
    _fixture as _replacement_fixture,
    _remove_long_private,
)


@unittest.skipUnless(os.name == "nt", "Windows bound snapshot refresh")
class WindowsTMSnapshotRefreshTests(unittest.TestCase):
    @contextmanager
    def _prepared_refresh(
        self,
    ) -> Iterator[
        tuple[
            CanonicalResourceIdentity,
            Path,
            SQLiteTMStore,
            TMMigrationService,
        ]
    ]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            identity = _activation_identity(root)
            service = _activation_service(identity)
            activation = service.activate_initial(
                identity.configured_jsonl_path,
                identity.resource_id,
            )
            self.assertIs(type(activation), MigrationReport, repr(activation))
            coordinator = service._coordinator
            self.assertIsNotNone(coordinator)
            assert coordinator is not None
            store = SQLiteTMStore.from_coordinator(coordinator)
            yield identity, identity.canonical_sidecar_path, store, service
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()

    def _assert_refresh_residue_absent(
        self,
        identity: CanonicalResourceIdentity,
    ) -> None:
        paths = _export_artifact_paths(identity.configured_jsonl_path)
        for artifact in (
            paths.jsonl_temp,
            paths.manifest_temp,
            paths.jsonl_recovery,
            paths.manifest_recovery,
        ):
            self.assertFalse(artifact.exists(), str(artifact))

    def test_windows_profile_executes_without_a_skip_fallback(self) -> None:
        self.assertEqual(os.name, "nt")
        self.assertIs(type(WindowsPlatformAdapter()), WindowsPlatformAdapter)

    def test_unactivated_store_without_private_namespace_fails_pre_arm(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            service = TMMigrationService(
                resource_identity=identity,
                canonical_store_id="store.primary",
                coordinator=store.coordinator,
                platform_backend=WindowsPlatformAdapter(),
            )
            before_pair = _pair(identity)
            before_binding = _binding_row(stage.staged_db_path)
            before_ledger = _ledger_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            before_canonical = store.capture_export_snapshot()

            result = service.refresh_configured_snapshot(store)

            self.assertIs(type(result), ExportFailure, repr(result))
            assert isinstance(result, ExportFailure)
            self.assertEqual(result.stage, "REFRESH.PREFLIGHT")
            self.assertFalse(result.publication_committed)
            self.assertEqual(_pair(identity), before_pair)
            self.assertEqual(_binding_row(stage.staged_db_path), before_binding)
            self.assertEqual(
                _ledger_rows(
                    stage.staged_db_path,
                    identity.configured_jsonl_path,
                ),
                before_ledger,
            )
            self.assertEqual(store.capture_export_snapshot(), before_canonical)
            self.assertEqual(
                _refresh_rows(
                    stage.staged_db_path,
                    identity.configured_jsonl_path,
                ),
                (),
            )
            self._assert_refresh_residue_absent(identity)

    def test_sidecar_guard_failure_is_pre_arm_and_mutation_free(self) -> None:
        with self._prepared_refresh() as (
            identity,
            database_path,
            store,
            service,
        ):
            before_pair = _pair(identity)
            before_binding = _binding_row(database_path)
            before_ledger = _ledger_rows(
                database_path,
                identity.configured_jsonl_path,
            )
            before_canonical = store.capture_export_snapshot()
            with patch.object(
                WindowsPlatformAdapter,
                "guard_existing_for_mutation",
                side_effect=PlatformFileError(
                    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                    retryable=False,
                ),
            ):
                result = service.refresh_configured_snapshot(store)

            self.assertIs(type(result), ExportFailure, repr(result))
            assert isinstance(result, ExportFailure)
            self.assertEqual(result.stage, "REFRESH.PREFLIGHT")
            self.assertFalse(result.publication_committed)
            self.assertEqual(_pair(identity), before_pair)
            self.assertEqual(_binding_row(database_path), before_binding)
            self.assertEqual(
                _ledger_rows(
                    database_path,
                    identity.configured_jsonl_path,
                ),
                before_ledger,
            )
            self.assertEqual(store.capture_export_snapshot(), before_canonical)
            self.assertEqual(
                _refresh_rows(
                    database_path,
                    identity.configured_jsonl_path,
                ),
                (),
            )
            self._assert_refresh_residue_absent(identity)

    def test_private_owner_drift_before_register_cleans_candidates(self) -> None:
        with self._prepared_refresh() as (
            identity,
            database_path,
            store,
            service,
        ):
            before_pair = _pair(identity)
            before_binding = _binding_row(database_path)
            before_ledger = _ledger_rows(
                database_path,
                identity.configured_jsonl_path,
            )
            original = (
                _InitialActivationResourceReservation
                .reprove_bound_refresh_owner
            )
            calls = 0

            def drift(
                reservation: _InitialActivationResourceReservation,
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                original(reservation, *args, **kwargs)

            with patch.object(
                _InitialActivationResourceReservation,
                "reprove_bound_refresh_owner",
                new=drift,
            ):
                result = service.refresh_configured_snapshot(store)

            self.assertGreaterEqual(calls, 3)
            self.assertIs(type(result), ExportFailure, repr(result))
            assert isinstance(result, ExportFailure)
            self.assertFalse(result.publication_committed)
            self.assertEqual(_pair(identity), before_pair)
            self.assertEqual(_binding_row(database_path), before_binding)
            self.assertEqual(
                _ledger_rows(
                    database_path,
                    identity.configured_jsonl_path,
                ),
                before_ledger,
            )
            self.assertEqual(
                _refresh_rows(
                    database_path,
                    identity.configured_jsonl_path,
                ),
                (),
            )
            self._assert_refresh_residue_absent(identity)

    def test_final_owner_reproof_failure_upgrades_rollback_result(self) -> None:
        with self._prepared_refresh() as (
            identity,
            database_path,
            store,
            service,
        ):
            before_pair = _pair(identity)
            original_reprove = (
                _InitialActivationResourceReservation
                .reprove_bound_refresh_owner
            )
            original_cancel = SQLiteTMStore.cancel_issued_refresh_receipt
            cancelled = False

            def cancel(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal cancelled
                original_cancel(authority, *args, **kwargs)
                cancelled = True

            def final_drift(
                reservation: _InitialActivationResourceReservation,
                *args: object,
                **kwargs: object,
            ) -> None:
                if cancelled:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                original_reprove(reservation, *args, **kwargs)

            with (
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_refresh_receipt",
                    side_effect=SQLiteStoreSchemaError("STORE.INJECTED"),
                ),
                patch.object(
                    SQLiteTMStore,
                    "cancel_issued_refresh_receipt",
                    new=cancel,
                ),
                patch.object(
                    _InitialActivationResourceReservation,
                    "reprove_bound_refresh_owner",
                    new=final_drift,
                ),
            ):
                result = service.refresh_configured_snapshot(store)

            self.assertTrue(cancelled)
            self.assertIs(type(result), ExportFailure, repr(result))
            assert isinstance(result, ExportFailure)
            self.assertEqual(result.stage, "REFRESH.RECOVERY")
            self.assertTrue(result.publication_commit_ambiguous)
            self.assertEqual(_pair(identity), before_pair)
            rows = _refresh_rows(
                database_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][9]), "cancelled")
            self._assert_refresh_residue_absent(identity)

    def test_proven_unsafe_configured_family_latches_divergence(self) -> None:
        with self._prepared_refresh() as (
            identity,
            database_path,
            store,
            service,
        ):
            before_ledger = _ledger_rows(
                database_path,
                identity.configured_jsonl_path,
            )
            foreign = identity.configured_jsonl_path.with_name(
                "foreign-hardlink.jsonl"
            )
            foreign.write_bytes(identity.configured_jsonl_path.read_bytes())
            identity.configured_jsonl_path.unlink()
            os.link(foreign, identity.configured_jsonl_path)

            result = service.refresh_configured_snapshot(store)

            self.assertIs(type(result), ExportFailure, repr(result))
            assert isinstance(result, ExportFailure)
            self.assertEqual(result.error_code, "REFRESH.SOURCE_DIVERGED")
            self.assertFalse(result.publication_committed)
            self.assertEqual(
                _ledger_rows(
                    database_path,
                    identity.configured_jsonl_path,
                ),
                before_ledger,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            self._assert_refresh_residue_absent(identity)

    def test_generation_one_replacement_owner_refreshes_successfully(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = _replacement_fixture(root)
                replacement = service._explicit_disambiguation(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(replacement), MigrationReport, repr(replacement))
                self.assertEqual(replacement.activated_generation, 1)
                self.assertEqual(coordinator.current_generation, 1)
                store = SQLiteTMStore.from_coordinator(coordinator)
                store.append(_draft("replacement-history", "generation one"))
                self.assertEqual(
                    store.source_binding_monitor.observe().state,
                    SourceBindingState.VERIFIED_HISTORY,
                )
                before = store.capture_export_snapshot()

                result = service.refresh_configured_snapshot(store)

                self.assertIs(type(result), ExportReport, repr(result))
                assert isinstance(result, ExportReport)
                self.assertEqual(result.canonical_generation, 1)
                self.assertEqual(result.exported_revision, before.revision.head_revision)
                self.assertEqual(store.capture_export_snapshot(), before)
                self.assertEqual(
                    store.source_binding_monitor.observe().state,
                    SourceBindingState.VERIFIED_CURRENT,
                )
                self.assertEqual(_binding_row(identity.canonical_sidecar_path)[4], result.snapshot_id)
                self._assert_refresh_residue_absent(identity)
            finally:
                _remove_long_quarantine(root)
                _remove_long_private(root)

    def test_public_refresh_closes_pair_receipt_binding_and_keeps_canonical(
        self,
    ) -> None:
        with self._prepared_refresh() as (
            identity,
            database_path,
            store,
            service,
        ):
            before = store.capture_export_snapshot()

            result = service.refresh_configured_snapshot(store)

            self.assertIs(type(result), ExportReport, repr(result))
            assert isinstance(result, ExportReport)
            self.assertEqual(store.capture_export_snapshot(), before)
            jsonl_bytes = identity.configured_jsonl_path.read_bytes()
            self.assertEqual(
                hashlib.sha256(jsonl_bytes).hexdigest(),
                result.snapshot_receipt.jsonl_digest,
            )
            self.assertEqual(
                result.destination_digest,
                result.snapshot_receipt.jsonl_digest,
            )
            self.assertEqual(
                result.exported_revision,
                before.revision.head_revision,
            )
            self.assertEqual(
                result.canonical_generation,
                before.revision.generation,
            )

            manifest = contract_from_json(
                identity.snapshot_manifest_path.read_text(encoding="utf-8")
            )
            self.assertIs(type(manifest), SnapshotManifest)
            assert isinstance(manifest, SnapshotManifest)
            self.assertEqual(manifest.receipt, result.snapshot_receipt)
            self.assertEqual(
                manifest.receipt_digest,
                snapshot_receipt_digest(result.snapshot_receipt),
            )
            self.assertEqual(
                result.snapshot_receipt_digest,
                manifest.receipt_digest,
            )

            binding = _binding_row(database_path)
            self.assertEqual(
                str(binding[1]),
                str(identity.configured_jsonl_path),
            )
            self.assertEqual(
                str(binding[2]),
                str(identity.snapshot_manifest_path),
            )
            self.assertEqual(str(binding[4]), result.snapshot_id)
            self.assertEqual(
                _status_for(database_path, result.snapshot_id),
                "completed",
            )
            rows = _refresh_rows(
                database_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][0]), result.snapshot_id)
            self.assertEqual(
                str(rows[0][3]),
                str(before.revision.head_revision),
            )
            self.assertEqual(
                str(rows[0][4]),
                result.snapshot_receipt.jsonl_digest,
            )
            self.assertEqual(str(rows[0][9]), "completed")
            self._assert_refresh_residue_absent(identity)

    def test_verified_history_refreshes_to_current_without_canonical_change(
        self,
    ) -> None:
        with self._prepared_refresh() as (
            identity,
            database_path,
            store,
            service,
        ):
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            store.append(_draft("history", "appended before refresh"))
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_HISTORY,
            )
            before = store.capture_export_snapshot()
            generation = store.coordinator.current_generation

            result = service.refresh_configured_snapshot(store)

            self.assertIs(type(result), ExportReport, repr(result))
            assert isinstance(result, ExportReport)
            self.assertEqual(store.capture_export_snapshot(), before)
            self.assertEqual(store.coordinator.current_generation, generation)
            self.assertEqual(result.exported_revision, before.revision.head_revision)
            self.assertEqual(result.exported_count, before.revision.record_count)
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _binding_row(database_path)[4],
                result.snapshot_id,
            )
            self.assertEqual(
                _status_for(database_path, result.snapshot_id),
                "completed",
            )
            self._assert_refresh_residue_absent(identity)

    def test_existing_pair_keeps_four_pending_live_until_owner_completion(
        self,
    ) -> None:
        with self._prepared_refresh() as (
            _identity,
            _database_path,
            store,
            service,
        ):
            events: list[str] = []
            original_reprove = (
                _InitialActivationResourceReservation
                .reprove_bound_refresh_owner
            )
            original_register = SQLiteTMStore.register_issued_refresh_receipt
            original_complete = SQLiteTMStore.complete_bound_issued_refresh_receipt
            original_terminal = PendingPublication.terminal_reproof
            original_close = PendingPublication.close

            def reprove(
                reservation: _InitialActivationResourceReservation,
                *args: object,
                **kwargs: object,
            ) -> None:
                events.append("w2")
                original_reprove(reservation, *args, **kwargs)

            def register(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> None:
                events.append("register")
                original_register(authority, *args, **kwargs)

            def complete(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> None:
                events.append("complete-enter")
                original_complete(authority, *args, **kwargs)
                events.append("complete-return")

            def terminal(authority: PendingPublication) -> object:
                events.append("terminal")
                return original_terminal(authority)

            def close(authority: PendingPublication) -> None:
                events.append("close")
                original_close(authority)

            with (
                patch.object(
                    _InitialActivationResourceReservation,
                    "reprove_bound_refresh_owner",
                    new=reprove,
                ),
                patch.object(
                    SQLiteTMStore,
                    "register_issued_refresh_receipt",
                    new=register,
                ),
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_refresh_receipt",
                    new=complete,
                ),
                patch.object(
                    PendingPublication,
                    "terminal_reproof",
                    new=terminal,
                ),
                patch.object(PendingPublication, "close", new=close),
            ):
                result = service.refresh_configured_snapshot(store)

            self.assertIs(type(result), ExportReport, repr(result))
            register_index = events.index("register")
            complete_enter = events.index("complete-enter")
            complete_return = events.index("complete-return")
            first_terminal = events.index("terminal")
            self.assertIn("w2", events[:register_index])
            self.assertIn("w2", events[register_index + 1 : complete_enter])
            self.assertIn("w2", events[complete_enter + 1 : complete_return])
            self.assertIn("w2", events[complete_return + 1 : first_terminal])
            self.assertEqual(events.count("terminal"), 4)
            self.assertEqual(events.count("close"), 4)
            for index, event in enumerate(events):
                if event == "terminal":
                    self.assertEqual(events[index - 1], "w2")
            self.assertLess(
                max(
                    index
                    for index, event in enumerate(events)
                    if event == "terminal"
                ),
                min(index for index, event in enumerate(events) if event == "close"),
            )

    def test_w1_precedes_refresh_gate_and_publisher_reuses_same_lock(
        self,
    ) -> None:
        with self._prepared_refresh() as (
            _identity,
            _database_path,
            store,
            service,
        ):
            events: list[str] = []
            captured: dict[str, object] = {}
            original_inputs = (
                _InitialActivationResourceReservation.bound_family_inputs
            )
            original_gate = SQLiteTMStore.configured_refresh_reservation
            original_publish = TMMigrationService._publish_bound_export_snapshot

            def inputs(
                reservation: _InitialActivationResourceReservation,
            ) -> tuple[object, object, object, str, bytes]:
                result = original_inputs(reservation)
                events.append("w1")
                captured.update(
                    lease=result[2],
                    lock_name=result[3],
                    lock_payload=result[4],
                )
                return result

            @contextmanager
            def gate(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> Iterator[None]:
                events.append("gate")
                with original_gate(authority, *args, **kwargs):
                    yield

            def publish(
                migration: TMMigrationService,
                *args: object,
                **kwargs: object,
            ) -> object:
                events.append("publish")
                self.assertIs(kwargs["lease"], captured["lease"])
                self.assertEqual(kwargs["lock_name"], captured["lock_name"])
                self.assertEqual(
                    kwargs["lock_payload"],
                    captured["lock_payload"],
                )
                return original_publish(migration, *args, **kwargs)

            with (
                patch.object(
                    _InitialActivationResourceReservation,
                    "bound_family_inputs",
                    new=inputs,
                ),
                patch.object(
                    SQLiteTMStore,
                    "configured_refresh_reservation",
                    new=gate,
                ),
                patch.object(
                    TMMigrationService,
                    "_publish_bound_export_snapshot",
                    new=publish,
                ),
            ):
                result = service.refresh_configured_snapshot(store)

            self.assertIs(type(result), ExportReport, repr(result))
            self.assertLess(events.index("w1"), events.index("gate"))
            self.assertLess(events.index("gate"), events.index("publish"))

    def test_committed_failure_survives_final_owner_reproof_failure(self) -> None:
        with self._prepared_refresh() as (
            _identity,
            _database_path,
            store,
            service,
        ):
            original_complete = SQLiteTMStore.complete_bound_issued_refresh_receipt
            original_reprove = (
                _InitialActivationResourceReservation
                .reprove_bound_refresh_owner
            )
            completed = False

            def complete(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal completed
                original_complete(authority, *args, **kwargs)
                completed = True

            def fail_after_complete(
                reservation: _InitialActivationResourceReservation,
                *args: object,
                **kwargs: object,
            ) -> None:
                if completed:
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )
                original_reprove(reservation, *args, **kwargs)

            with (
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_refresh_receipt",
                    new=complete,
                ),
                patch.object(
                    _InitialActivationResourceReservation,
                    "reprove_bound_refresh_owner",
                    new=fail_after_complete,
                ),
            ):
                result = service.refresh_configured_snapshot(store)

            self.assertIs(type(result), ExportFailure, repr(result))
            assert isinstance(result, ExportFailure)
            self.assertTrue(result.publication_committed)
            self.assertEqual(result.stage, "REFRESH.LEDGER")
            self.assertFalse(result.publication_commit_ambiguous)

    def test_owner_precommit_failure_restores_prior_pair_and_cancels(self) -> None:
        with self._prepared_refresh() as (
            identity,
            database_path,
            store,
            service,
        ):
            before_pair = _pair(identity)
            before_binding = _binding_row(database_path)
            before_canonical = store.capture_export_snapshot()

            with patch.object(
                SQLiteTMStore,
                "complete_bound_issued_refresh_receipt",
                side_effect=SQLiteStoreSchemaError("STORE.INJECTED"),
            ):
                result = service.refresh_configured_snapshot(store)

            self.assertIs(type(result), ExportFailure, repr(result))
            assert isinstance(result, ExportFailure)
            self.assertFalse(result.publication_committed)
            self.assertEqual(_pair(identity), before_pair)
            self.assertEqual(_binding_row(database_path), before_binding)
            self.assertEqual(store.capture_export_snapshot(), before_canonical)
            rows = _refresh_rows(
                database_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][9]), "cancelled")
            self._assert_refresh_residue_absent(identity)

    def test_commit_then_raise_uses_bound_probe_and_finishes_four_pending(
        self,
    ) -> None:
        with self._prepared_refresh() as (
            identity,
            database_path,
            store,
            service,
        ):
            events: list[str] = []
            probe_kwargs: list[dict[str, object]] = []
            original_complete = SQLiteTMStore.complete_bound_issued_refresh_receipt
            original_probe = (
                SQLiteTMStore.probe_bound_issued_refresh_receipt_completed
            )
            original_terminal = PendingPublication.terminal_reproof
            original_close = PendingPublication.close

            def complete_then_raise(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> None:
                original_complete(authority, *args, **kwargs)
                events.append("owner-raised")
                raise sqlite3.DatabaseError("after refresh commit")

            def probe(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> object:
                events.append("bound-probe")
                probe_kwargs.append(dict(kwargs))
                return original_probe(authority, *args, **kwargs)

            def terminal(authority: PendingPublication) -> object:
                events.append("terminal")
                return original_terminal(authority)

            def close(authority: PendingPublication) -> None:
                events.append("close")
                original_close(authority)

            with (
                patch.object(
                    SQLiteTMStore,
                    "complete_bound_issued_refresh_receipt",
                    new=complete_then_raise,
                ),
                patch.object(
                    SQLiteTMStore,
                    "probe_bound_issued_refresh_receipt_completed",
                    new=probe,
                ),
                patch.object(
                    SQLiteTMStore,
                    "probe_issued_receipt_completed",
                    side_effect=AssertionError("pathname completion probe used"),
                ),
                patch.object(
                    PendingPublication,
                    "terminal_reproof",
                    new=terminal,
                ),
                patch.object(PendingPublication, "close", new=close),
            ):
                result = service.refresh_configured_snapshot(store)

            self.assertIs(type(result), ExportReport, repr(result))
            assert isinstance(result, ExportReport)
            self.assertEqual(events[:2], ["owner-raised", "bound-probe"])
            self.assertEqual(len(probe_kwargs), 1)
            self.assertTrue(callable(probe_kwargs[0].get("family_reprove")))
            self.assertEqual(events.count("terminal"), 4)
            self.assertEqual(events.count("close"), 4)
            self.assertEqual(
                _binding_row(database_path)[4],
                result.snapshot_id,
            )
            self.assertEqual(
                _status_for(database_path, result.snapshot_id),
                "completed",
            )
            self._assert_refresh_residue_absent(identity)

    def test_revision_change_before_owner_commit_fails_closed(self) -> None:
        with self._prepared_refresh() as (
            identity,
            database_path,
            store,
            service,
        ):
            before_pair = _pair(identity)
            before_binding = _binding_row(database_path)
            before_revision = store.canonical_revision()
            original_complete = SQLiteTMStore.complete_bound_issued_refresh_receipt
            advanced = False

            def advance_then_complete(
                authority: SQLiteTMStore,
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal advanced
                self.assertFalse(advanced)
                advanced = True
                store.append(_draft("stale", "advanced before owner commit"))
                original_complete(authority, *args, **kwargs)

            with patch.object(
                SQLiteTMStore,
                "complete_bound_issued_refresh_receipt",
                new=advance_then_complete,
            ):
                result = service.refresh_configured_snapshot(store)

            self.assertTrue(advanced)
            self.assertIs(type(result), ExportFailure, repr(result))
            assert isinstance(result, ExportFailure)
            self.assertFalse(result.publication_committed)
            self.assertEqual(_pair(identity), before_pair)
            self.assertEqual(_binding_row(database_path), before_binding)
            self.assertEqual(
                store.canonical_revision().head_revision,
                before_revision.head_revision + 1,
            )
            rows = _refresh_rows(
                database_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(str(rows[0][9]), "cancelled")
            self._assert_refresh_residue_absent(identity)

    def test_programmer_errors_from_owner_completion_pass_through(self) -> None:
        for error_type in (TypeError, AssertionError, AttributeError):
            with self.subTest(error_type=error_type.__name__):
                with self._prepared_refresh() as (
                    _identity,
                    _database_path,
                    store,
                    service,
                ):
                    with patch.object(
                        SQLiteTMStore,
                        "complete_bound_issued_refresh_receipt",
                        side_effect=error_type("programmer refresh fault"),
                    ):
                        with self.assertRaisesRegex(
                            error_type,
                            "^programmer refresh fault$",
                        ):
                            service.refresh_configured_snapshot(store)
