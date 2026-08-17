"""Task 2.1 public contract for the first canonical activation."""

from __future__ import annotations

from dataclasses import fields
import inspect
from pathlib import Path
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_migration
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationOutcome,
    MigrationReport,
)
from tm_migration import MigrationPreflightError, TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteTMStore


SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"same","target":"winner"}\n'
    b'{"source":"other","target":"value"}\n'
)


class _StringSubclass(str):
    pass


class _PathSubclass(type(Path())):
    pass


def _identity(
    root: Path,
    *,
    resource_id: str = "tm.primary",
    basename: str = "primary.jsonl",
) -> CanonicalResourceIdentity:
    source = (root / basename).resolve()
    source.write_bytes(SOURCE_BYTES)
    return CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        source,
    )


def _coordinator(
    identity: CanonicalResourceIdentity,
    *,
    canonical_store_id: str = "store.primary",
) -> ResourceStoreCoordinator:
    return ResourceStoreCoordinator(
        canonical_store_id=canonical_store_id,
        resource_identity=identity,
    )


def _service(
    identity: CanonicalResourceIdentity,
    coordinator: ResourceStoreCoordinator | None,
    *,
    canonical_store_id: str = "store.primary",
) -> TMMigrationService:
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id=canonical_store_id,
        coordinator=coordinator,
    )


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    entries: list[tuple[str, str, bytes]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "symlink", str(path.readlink()).encode()))
        elif path.is_file():
            entries.append((relative, "file", path.read_bytes()))
        elif path.is_dir():
            entries.append((relative, "directory", b""))
        else:
            entries.append((relative, "other", b""))
    return tuple(entries)


class InitialActivationPublicContractTests(unittest.TestCase):
    def test_signature_and_export_surface_are_frozen(self) -> None:
        method = TMMigrationService.activate_initial
        signature = inspect.signature(method)
        self.assertEqual(
            tuple(signature.parameters),
            ("self", "source", "resource_id"),
        )
        self.assertEqual(
            signature.parameters["source"].annotation,
            "Path",
        )
        self.assertEqual(
            signature.parameters["resource_id"].annotation,
            "str",
        )
        self.assertEqual(signature.return_annotation, "MigrationOutcome")
        self.assertIn("TMMigrationService", tm_migration.__all__)
        self.assertNotIn("StageSealer", tm_migration.__all__)
        self.assertNotIn("MutableStageRef", tm_migration.__all__)
        self.assertNotIn("SealedStage", tm_migration.__all__)
        self.assertNotIn("_ActivationPreparation", tm_migration.__all__)
        self.assertEqual(
            tuple(field.name for field in fields(MigrationReport)),
            (
                "resource_id",
                "canonical_store_id",
                "source_digest",
                "snapshot_receipt",
                "migrated_count",
                "variant_count",
                "skipped_count",
                "diagnostics",
                "activated_generation",
                "canonical_exact_available",
                "context_available",
                "fuzzy_available",
            ),
        )
        self.assertNotIn("registry", signature.parameters)
        self.assertNotIn("stage", signature.parameters)
        self.assertNotIn("token", signature.parameters)
        self.assertNotIn("preparation", signature.parameters)

    def test_happy_path_returns_public_outcome_and_opens_exact_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)

            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                outcome: MigrationOutcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertIs(type(outcome), MigrationReport)
            report = outcome
            assert isinstance(report, MigrationReport)
            self.assertEqual(report.resource_id, identity.resource_id)
            self.assertEqual(report.canonical_store_id, "store.primary")
            self.assertEqual(report.activated_generation, 0)
            self.assertEqual(report.migrated_count, 3)
            self.assertEqual(report.variant_count, 1)
            self.assertEqual(report.skipped_count, 0)
            self.assertTrue(report.canonical_exact_available)
            self.assertFalse(report.context_available)
            self.assertFalse(report.fuzzy_available)
            self.assertEqual(identity.configured_jsonl_path.read_bytes(), SOURCE_BYTES)
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(coordinator.current_generation, 0)
            store = SQLiteTMStore.from_coordinator(coordinator)
            self.assertEqual(
                tuple(record.target_raw for record in store.exact_records("same")),
                ("winner", "first"),
            )

    def test_invalid_builtin_types_fail_before_mutation(self) -> None:
        invalid_cases: tuple[tuple[Any, Any, type[BaseException], str | None], ...] = (
            (
                "not-a-path",
                "tm.primary",
                TypeError,
                None,
            ),
            (
                _PathSubclass("/definitely/not/the/configured/source"),
                "tm.primary",
                TypeError,
                None,
            ),
            (
                None,
                "tm.primary",
                TypeError,
                None,
            ),
            (
                None,
                "",
                MigrationPreflightError,
                "MIGRATION.RESOURCE_ID_INVALID",
            ),
            (
                None,
                _StringSubclass("tm.primary"),
                MigrationPreflightError,
                "MIGRATION.RESOURCE_ID_INVALID",
            ),
        )
        for source_input, resource_input, error_type, error_code in invalid_cases:
            with self.subTest(source=source_input, resource=resource_input):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity = _identity(root)
                    coordinator = _coordinator(identity)
                    service = _service(identity, coordinator)
                    actual_source = (
                        identity.configured_jsonl_path
                        if source_input is None and resource_input != "tm.primary"
                        else source_input
                    )
                    before = _tree_snapshot(root)
                    with self.assertRaises(error_type) as raised:
                        service.activate_initial(
                            cast(Any, actual_source),
                            cast(Any, resource_input),
                        )
                    if error_code is not None:
                        self.assertEqual(
                            getattr(raised.exception, "error_code", None),
                            error_code,
                        )
                    self.assertEqual(_tree_snapshot(root), before)
                    self.assertEqual(coordinator.state, "READY")
                    self.assertIsNone(coordinator.current_generation)

    def test_wrong_source_resource_and_missing_or_foreign_coordinator_are_zero_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            wrong_source = (root / "wrong.jsonl").resolve()
            wrong_source.write_bytes(SOURCE_BYTES)
            foreign_identity = _identity(
                root,
                resource_id="tm.foreign",
                basename="foreign.jsonl",
            )
            cases = (
                (
                    _service(identity, _coordinator(identity)),
                    wrong_source,
                    identity.resource_id,
                    "MIGRATION.RESOURCE_IDENTITY_MISMATCH",
                ),
                (
                    _service(identity, _coordinator(identity)),
                    identity.configured_jsonl_path,
                    "tm.foreign",
                    "MIGRATION.RESOURCE_IDENTITY_MISMATCH",
                ),
                (
                    _service(identity, None),
                    identity.configured_jsonl_path,
                    identity.resource_id,
                    "MIGRATION.COORDINATOR_UNAVAILABLE",
                ),
                (
                    _service(identity, _coordinator(foreign_identity)),
                    identity.configured_jsonl_path,
                    identity.resource_id,
                    "MIGRATION.COORDINATOR_IDENTITY_MISMATCH",
                ),
                (
                    _service(
                        identity,
                        _coordinator(identity, canonical_store_id="store.foreign"),
                    ),
                    identity.configured_jsonl_path,
                    identity.resource_id,
                    "MIGRATION.COORDINATOR_IDENTITY_MISMATCH",
                ),
            )
            for service, source, resource_id, code in cases:
                with self.subTest(code=code):
                    before = _tree_snapshot(root)
                    coordinator = service._coordinator
                    coordinator_state = None if coordinator is None else coordinator.state
                    coordinator_generation = (
                        None if coordinator is None else coordinator.current_generation
                    )
                    with self.assertRaises(MigrationPreflightError) as raised:
                        service.activate_initial(source, resource_id)
                    self.assertEqual(raised.exception.error_code, code)
                    self.assertEqual(_tree_snapshot(root), before)
                    if coordinator is not None:
                        self.assertEqual(coordinator.state, coordinator_state)
                        self.assertEqual(
                            coordinator.current_generation,
                            coordinator_generation,
                        )

    def test_already_active_fails_with_stable_code_and_zero_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            coordinator = _coordinator(identity)
            service = _service(identity, coordinator)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                first = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(first), MigrationReport)
            before = _tree_snapshot(root)
            generation_before = coordinator.current_generation

            with self.assertRaises(MigrationPreflightError) as raised:
                service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertEqual(
                raised.exception.error_code,
                "MIGRATION.ALREADY_ACTIVE",
            )
            self.assertEqual(_tree_snapshot(root), before)
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(coordinator.current_generation, generation_before)


if __name__ == "__main__":
    unittest.main()
