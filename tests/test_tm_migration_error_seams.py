"""Core 错误接缝：纯控制流证据不替代 Windows 原生 authority 验收。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
from tm_activation_journal import ActivationPreparationError
from tm_contracts import CanonicalResourceIdentity
import tm_migration as migration
import tm_sqlite_store as store


def _platform_error() -> PlatformFileError:
    return PlatformFileError(
        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False,
    )


class _CloseProbe:
    def __init__(
        self, name: str, events: list[str], error: BaseException,
        *, close_first: bool = True,
    ) -> None:
        self.name = name
        self.events = events
        self.error = error
        self.close_first = close_first
        self.closed = False

    def close(self) -> None:
        self.events.append(self.name)
        if self.close_first:
            self.closed = True
        raise self.error


class MigrationErrorSeamTests(unittest.TestCase):
    # unittest calls setUp before each test; these fields belong to that lifecycle.
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    source: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    identity: CanonicalResourceIdentity  # pyright: ignore[reportUninitializedInstanceVariable]
    owner: store.ResourceStoreCoordinator  # pyright: ignore[reportUninitializedInstanceVariable]
    service: migration.TMMigrationService  # pyright: ignore[reportUninitializedInstanceVariable]

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "memory.jsonl"
        self.source.write_text('{"source":"hello","target":"你好"}\n', encoding="utf-8")
        self.identity = CanonicalResourceIdentity.from_configured_jsonl("tm.errors", self.source)
        self.owner = store.ResourceStoreCoordinator(
            resource_identity=self.identity, canonical_store_id="store.errors",
        )
        self.addCleanup(self.owner.close_family_session)
        self.service = migration.TMMigrationService(
            resource_identity=self.identity, canonical_store_id="store.errors",
            coordinator=self.owner,
        )

    def _attempt(self, database: _CloseProbe, manifest: _CloseProbe) -> Any:
        return SimpleNamespace(
            stage_retirement=database, manifest_retirement=manifest,
            stage_reservation=None, manifest_reservation=None,
        )

    def _assert_public_source_close_error(self, *, replacement: bool, ambient: bool) -> None:
        """Run real builder/seal/publication; inject only after real source close."""
        if replacement:
            first = self.service.activate_initial(self.source, self.identity.resource_id)
            self.assertIs(type(first), migration.MigrationReport)
        method = self.service.import_snapshot if replacement else self.service.activate_initial
        close_error = OSError("source close after actual publication")
        caller_error = LookupError("caller is already handling this unrelated error")
        readers: list[migration._PortableRootedRead] = []
        real_close = migration._PortableRootedRead.close

        def close(reader: migration._PortableRootedRead) -> None:
            readers.append(reader)
            real_close(reader)
            raise close_error

        caught: BaseException | None = None
        with patch.object(migration._PortableRootedRead, "close", close):
            if ambient:
                try:
                    raise caller_error
                except LookupError:
                    try:
                        method(self.source, self.identity.resource_id)
                    except BaseException as error:
                        caught = error
            else:
                try:
                    method(self.source, self.identity.resource_id)
                except BaseException as error:
                    caught = error
        self.assertIs(caught, close_error)
        self.assertFalse(hasattr(caller_error, "__notes__"))
        self.assertTrue(readers)
        self.assertTrue(all(reader.authority.closed for reader in readers))
        self.assertEqual(self.owner.current_generation, 1 if replacement else 0)

    @unittest.skipIf(sys.platform == "win32", "真实 POSIX source-close 对照")
    def test_initial_public_source_close_without_caller_exception(self) -> None:
        self._assert_public_source_close_error(replacement=False, ambient=False)

    @unittest.skipIf(sys.platform == "win32", "真实 POSIX source-close 对照")
    def test_initial_public_source_close_inside_caller_except(self) -> None:
        self._assert_public_source_close_error(replacement=False, ambient=True)

    @unittest.skipIf(sys.platform == "win32", "真实 POSIX source-close 对照")
    def test_replacement_public_source_close_without_caller_exception(self) -> None:
        self._assert_public_source_close_error(replacement=True, ambient=False)

    @unittest.skipIf(sys.platform == "win32", "真实 POSIX source-close 对照")
    def test_replacement_public_source_close_inside_caller_except(self) -> None:
        self._assert_public_source_close_error(replacement=True, ambient=True)

    def test_initial_reconciliation_error_owns_primary_inside_caller_except(self) -> None:
        for primary in (TypeError("reconciliation"), KeyboardInterrupt()):
            with self.subTest(primary=type(primary).__name__):
                body_error = ActivationPreparationError("ACTIVATION.GATE_B_DENIED", retryable=False)
                caller_error = LookupError("caller error")
                events: list[str] = []
                attempt = self._attempt(_CloseProbe("db", events, _platform_error()), _CloseProbe("manifest", events, _platform_error()))
                reservation = Mock(_family_borrow=None, _initial_attempt=attempt)
                try:
                    raise caller_error
                except LookupError:
                    with patch.object(self.service, "preflight", side_effect=body_error), patch.object(
                        self.service, "_reconcile_initial_activation_failure", side_effect=primary,
                    ), self.assertRaises(type(primary)) as caught:
                        self.service._activate_initial_authority_transaction(
                            coordinator=self.owner, source=self.source,
                            source_before="0" * 64, reservation=reservation,
                        )
                self.assertIs(caught.exception, primary)
                self.assertIs(primary.__context__, body_error)
                self.assertTrue(any("Additional stage close failed" in note for note in primary.__notes__))
                self.assertEqual(events, ["db", "manifest"])
                self.assertIsNone(reservation._initial_attempt)
                self.assertFalse(hasattr(caller_error, "__notes__"))

    def test_posix_reconciliation_error_owns_primary_inside_caller_except(self) -> None:
        for primary in (TypeError("reconciliation"), KeyboardInterrupt()):
            with self.subTest(primary=type(primary).__name__):
                body_error = migration.MigrationPreflightError("MIGRATION.SOURCE_CHANGED")
                caller_error = LookupError("caller error")
                events: list[str] = []
                source = cast(Any, _CloseProbe("source", events, OSError("source close")))
                source.authority = _CloseProbe("source-authority", events, _platform_error())
                coordinator = Mock(active_store_path=self.root / "unused.sqlite3", canonical_store_id="store.errors", current_generation=0)
                reservation = Mock(_family_borrow=None)
                reservation.open_portable_rooted_read.return_value = source
                try:
                    raise caller_error
                except LookupError:
                    with patch.object(self.service, "_coordinator", coordinator), patch.object(
                        migration, "_try_file_digest", return_value="0" * 64,
                    ), patch.object(migration, "_scan_portable_jsonl", side_effect=body_error), patch.object(
                        self.service, "_disambiguation_failure", side_effect=primary,
                    ), self.assertRaises(type(primary)) as caught:
                        self.service._posix_explicit_disambiguation_reserved(
                            self.source, self.identity.resource_id, reservation=reservation,
                        )
                self.assertIs(caught.exception, primary)
                self.assertIs(primary.__context__, body_error)
                self.assertTrue(any("Additional activation close failed" in note for note in primary.__notes__))
                self.assertEqual(events, ["source", "source-authority"])
                self.assertIsNone(reservation._initial_attempt)
                self.assertFalse(hasattr(caller_error, "__notes__"))

    def test_handled_primary_inside_caller_except_keeps_local_close_facts(self) -> None:
        caller_error = LookupError("caller error")
        try:
            raise caller_error
        except LookupError:
            self.test_initial_handled_primary_yields_recovery_carrier()
            self.test_posix_handled_primary_yields_original_source_close_error()
        self.assertFalse(hasattr(caller_error, "__notes__"))

    def test_stage_owner_adapter_maps_only_exact_reservation_error(self) -> None:
        class DerivedReservationError(migration._InitialActivationReservationError):
            pass

        errors = (
            migration._InitialActivationReservationError("MIGRATION.INITIAL_RESOURCE_LOCK_UNAVAILABLE"),
            DerivedReservationError("MIGRATION.INITIAL_RESOURCE_LOCK_UNAVAILABLE"),
            TypeError("programmer owner"), AssertionError("programmer owner"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                reservation = migration._InitialActivationResourceReservation(
                    identity=self.identity, parent=None, descriptor=-1,
                    lock_name="unused", fcntl_module=None,
                    backend=cast(Any, object()), root=cast(Any, object()),
                    lease=cast(Any, object()),
                )
                with patch.object(type(reservation), "reprove") as reprove, patch.object(
                    migration, "_create_caller_held_seal_borrow",
                ) as mint:
                    reservation.stage_seal_inputs()
                    callback = mint.call_args.kwargs["owner_reproof"]
                    reprove.side_effect = error
                    if type(error) is migration._InitialActivationReservationError:
                        with self.assertRaises(migration.StageSealError) as caught:
                            callback()
                        self.assertEqual(caught.exception.error_code, "SEALER.ATTESTATION_UNAVAILABLE")
                        self.assertIs(caught.exception.__cause__, error)
                    else:
                        with self.assertRaises(type(error)) as caught:
                            callback()
                        self.assertIs(caught.exception, error)

    def test_stage_close_wraps_body_free_first_and_records_second(self) -> None:
        events: list[str] = []
        first, second = _platform_error(), _platform_error()
        attempt = self._attempt(_CloseProbe("db", events, first), _CloseProbe("manifest", events, second))
        with self.assertRaises(migration.MigrationPreflightError) as caught:
            migration._close_initial_stage_attempt_authorities(attempt)
        self.assertEqual(caught.exception.error_code, "MIGRATION.INITIAL_RECOVERY_REQUIRED")
        self.assertIs(caught.exception.__cause__, first)
        self.assertTrue(any("Additional stage close failed" in note for note in caught.exception.__notes__))
        self.assertEqual(events, ["db", "manifest"])
        self.assertIsNone(attempt.stage_retirement)
        self.assertIsNone(attempt.manifest_retirement)
        self.assertFalse(hasattr(first, "__notes__"))
        self.assertFalse(hasattr(second, "__notes__"))

    def test_stage_close_retries_only_open_authority_and_keeps_all_facts(self) -> None:
        events: list[str] = []
        first = _platform_error()
        database = _CloseProbe("db", events, first, close_first=False)
        attempt = self._attempt(database, _CloseProbe("manifest", events, _platform_error()))
        with self.assertRaises(migration.MigrationPreflightError) as caught:
            migration._close_initial_stage_attempt_authorities(attempt)
        self.assertEqual(events, ["db", "db", "manifest"])
        self.assertIs(attempt.stage_retirement, database)
        self.assertIsNone(attempt.manifest_retirement)
        self.assertIs(caught.exception.__cause__, first)
        self.assertTrue(any("remained incomplete" in note for note in caught.exception.__notes__))
        self.assertTrue(any("Additional stage close failed" in note for note in caught.exception.__notes__))

    def test_outer_close_body_free_primary_and_close_do_not_mask_facts(self) -> None:
        events: list[str] = []
        primary, first, second = _platform_error(), _platform_error(), _platform_error()
        source = cast(Any, _CloseProbe("source", events, first))
        source.authority = _CloseProbe("source-authority", events, second)
        error = migration._close_initial_activation_authorities(source, None, primary=primary)
        self.assertIsInstance(error, migration.MigrationPreflightError)
        assert error is not None
        self.assertIs(error.__cause__, first)
        self.assertEqual(events, ["source", "source-authority"])
        self.assertTrue(any("Additional activation close failed" in note for note in error.__notes__))
        self.assertTrue(any("Activation operation failed" in note for note in error.__notes__))
        self.assertFalse(hasattr(primary, "__notes__"))

    def test_initial_handled_primary_yields_recovery_carrier(self) -> None:
        primary = ActivationPreparationError("ACTIVATION.GATE_B_DENIED", retryable=False)
        first, second = _platform_error(), _platform_error()
        events: list[str] = []
        attempt = self._attempt(_CloseProbe("db", events, first), _CloseProbe("manifest", events, second))
        reservation = Mock(_family_borrow=None, _initial_attempt=attempt)
        with patch.object(self.service, "preflight", side_effect=primary), patch.object(
            self.service, "_reconcile_initial_activation_failure", return_value=Mock(),
        ), self.assertRaises(migration.MigrationPreflightError) as caught:
            self.service._activate_initial_authority_transaction(
                coordinator=self.owner, source=self.source,
                source_before="0" * 64, reservation=reservation,
            )
        self.assertEqual(caught.exception.error_code, "MIGRATION.INITIAL_RECOVERY_REQUIRED")
        self.assertIs(caught.exception.__cause__, first)
        self.assertTrue(any("ACTIVATION.GATE_B_DENIED" in note for note in caught.exception.__notes__))
        self.assertEqual(events, ["db", "manifest"])
        self.assertIsNone(reservation._initial_attempt)

    def test_initial_programmer_primary_survives_all_close_failures(self) -> None:
        for primary in (TypeError("body"), AssertionError("body"), AttributeError("body"), KeyboardInterrupt()):
            with self.subTest(primary=type(primary).__name__):
                events: list[str] = []
                attempt = self._attempt(_CloseProbe("db", events, _platform_error()), _CloseProbe("manifest", events, _platform_error()))
                reservation = Mock(_family_borrow=None, _initial_attempt=attempt)
                with patch.object(self.service, "preflight", side_effect=primary), self.assertRaises(type(primary)) as caught:
                    self.service._activate_initial_authority_transaction(
                        coordinator=self.owner, source=self.source,
                        source_before="0" * 64, reservation=reservation,
                    )
                self.assertIs(caught.exception, primary)
                self.assertEqual(events, ["db", "manifest"])
                self.assertTrue(any("Additional stage close failed" in note for note in primary.__notes__))
                self.assertIsNone(reservation._initial_attempt)

    def test_posix_handled_primary_yields_original_source_close_error(self) -> None:
        primary = migration.MigrationPreflightError("MIGRATION.SOURCE_CHANGED")
        secondary = OSError("source close")
        events: list[str] = []
        source = cast(Any, _CloseProbe("source", events, secondary))
        source.authority = Mock()
        coordinator = Mock(
            active_store_path=self.root / "unused.sqlite3",
            canonical_store_id="store.errors", current_generation=0,
        )
        reservation = Mock(_family_borrow=None)
        reservation.open_portable_rooted_read.return_value = source
        with patch.object(self.service, "_coordinator", coordinator), patch.object(
            migration, "_try_file_digest", return_value="0" * 64,
        ), patch.object(migration, "_scan_portable_jsonl", side_effect=primary), patch.object(
            self.service, "_disambiguation_failure", return_value=Mock(),
        ), self.assertRaises(OSError) as caught:
            self.service._posix_explicit_disambiguation_reserved(
                self.source, self.identity.resource_id, reservation=reservation,
            )
        self.assertIs(caught.exception, secondary)
        self.assertTrue(any("MIGRATION.SOURCE_CHANGED" in note for note in secondary.__notes__))
        self.assertEqual(events, ["source"])
        source.authority.close.assert_called_once_with()

    def test_completed_apis_keep_narrowing_capability_failure_nonretryable(self) -> None:
        self.service._recovery_profile = migration._RecoveryProfile.WINDOWS_V3
        for method in (self.service.open_completed_portable_runtime, self.service.rehydrate_completed_portable_activation):
            for seam in ("narrow_windows_persistent_private_proof", "narrow_windows_locked_descendant_namespace_inspection"):
                with self.subTest(method=method.__name__, seam=seam):
                    error = _platform_error()
                    with patch.object(migration, "compose_platform_file_backend", return_value=Mock()), patch.object(
                        migration, "narrow_windows_persistent_private_proof",
                    ), patch.object(migration, "narrow_windows_locked_descendant_namespace_inspection"), patch.object(
                        migration, seam, side_effect=error,
                    ), patch.object(migration._InitialActivationResourceReservation, "acquire_with_backend") as acquire, self.assertRaises(ActivationPreparationError) as caught:
                        method()
                    self.assertEqual(caught.exception.code, "ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE")
                    self.assertFalse(caught.exception.retryable)
                    self.assertIs(caught.exception.__cause__, error)
                    acquire.assert_not_called()

    def test_completed_apis_do_not_reclassify_compose_or_w1_failure(self) -> None:
        self.service._recovery_profile = migration._RecoveryProfile.WINDOWS_V3
        for method in (self.service.open_completed_portable_runtime, self.service.rehydrate_completed_portable_activation):
            for seam in ("compose", "acquire"):
                with self.subTest(method=method.__name__, seam=seam):
                    platform_error = _platform_error()
                    reservation_error = migration._InitialActivationReservationError("MIGRATION.INITIAL_RESOURCE_LOCK_UNAVAILABLE")
                    reservation_error.__cause__ = platform_error
                    with patch.object(
                        migration, "compose_platform_file_backend", return_value=Mock(),
                        side_effect=platform_error if seam == "compose" else None,
                    ), patch.object(migration, "narrow_windows_persistent_private_proof"), patch.object(
                        migration, "narrow_windows_locked_descendant_namespace_inspection",
                    ), patch.object(
                        migration._InitialActivationResourceReservation, "acquire_with_backend",
                        side_effect=reservation_error,
                    ), self.assertRaises(ActivationPreparationError) as caught:
                        method()
                    self.assertEqual(caught.exception.code, "ACTIVATION.RECOVERY_REQUIRED")
                    self.assertTrue(caught.exception.retryable)
                    self.assertIsInstance(caught.exception.__cause__, migration._InitialActivationReservationError)
                    assert caught.exception.__cause__ is not None
                    self.assertIs(caught.exception.__cause__.__cause__, platform_error)

    def test_initial_narrowing_failure_still_precedes_builder(self) -> None:
        self.service._recovery_profile = migration._RecoveryProfile.WINDOWS_V3
        with patch.object(migration, "compose_platform_file_backend", return_value=Mock()), patch.object(
            migration, "narrow_windows_persistent_private_proof", side_effect=_platform_error(),
        ), patch.object(self.service, "_build_stage") as builder, self.assertRaises(migration._InitialActivationReservationError):
            with self.service._initial_stage_reservation():
                builder()
        builder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
