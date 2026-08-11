"""Task 5.R3 snapshot-artifact module boundary tests.

The suite proves the Task 5.R3 behavior-preserving extraction held its
dependency direction: ``tm_snapshot_artifacts.py`` never imports
``tm_migration``, ``tm_snapshot_recovery`` or ``tm_sqlite_store``
(statically or through literal dynamic imports), the owner authorities
(export/refresh orchestration, receipt reconciliation/terminal replay/
divergence decisions, ledger/binding/coordinator state) stay in their
modules, and the old private names stay behind as late-bound
compatibility wrappers so existing patch/fault-injection seams keep
observing the owner namespaces.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import py_compile
import sys
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_migration
import tm_snapshot_artifacts
import tm_snapshot_recovery
import tm_sqlite_store


PROJECT_ROOT = Path(__file__).resolve().parents[1]

_OWNER_MODULES = (
    "tm_migration",
    "tm_snapshot_recovery",
    "tm_sqlite_store",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _dynamic_imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "import_module"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            modules.add(first.value)
    return modules


# Every private/public name that moved to tm_snapshot_artifacts must
# remain in the migration owner as a late-bound compatibility wrapper or
# alias.
_MOVED_MIGRATION_NAMES = (
    "_EXPORT_MANIFEST_SUFFIX",
    "_EXPORT_JSONL_TEMP_SUFFIX",
    "_EXPORT_MANIFEST_TEMP_SUFFIX",
    "_EXPORT_JSONL_RECOVERY_SUFFIX",
    "_EXPORT_MANIFEST_RECOVERY_SUFFIX",
    "_CreatedFileIdentity",
    "_NoDestinationProof",
    "_NO_DESTINATION_PROOF",
    "ExportPreflightError",
    "_ExportArtifactPaths",
    "_ExportParentHandle",
    "_require_export_basename",
    "_export_artifact_paths",
    "_export_authority_paths",
    "_export_path_in_authority_family",
    "_artifact_parent_identity",
    "_open_export_parent_chain_no_follow",
    "_after_export_parent_chain_validated",
    "_after_replace_source_proved",
    "_prove_replace_source",
    "_prove_replace_destination",
    "_require_export_parent_safe",
    "_export_existing_digest",
    "_export_existing_state",
    "_validate_export_destination",
    "_refresh_destination_state",
    "_require_refresh_artifacts_absent",
    "_path_exists",
    "_fsync_file",
    "_fsync_directory",
    "_created_export_identity",
    "_replace_path",
    "_stream_export_jsonl_temp",
    "_verify_export_jsonl_temp",
    "_write_export_payload_temp",
    "_verify_export_payload_temp",
    "_remove_exported_if_owned",
    "_copy_export_prior_pair",
    "_copy_export_recovery_file",
    "_entry_is_owned",
    "_restore_export_pair",
    "_restore_export_from_recovery",
    "_dirfd_entry_state",
    "_strict_regular_file_state",
    "_published_file_identity",
    "_verify_export_pair",
    "_cleanup_export_artifacts",
    "_remove_failed_export_artifact",
    "_remove_created_file",
    "_try_file_digest",
)

_MOVED_RECOVERY_NAMES = (
    "_EXPORT_MANIFEST_SUFFIX",
    "_EXPORT_JSONL_TEMP_SUFFIX",
    "_EXPORT_MANIFEST_TEMP_SUFFIX",
    "_EXPORT_JSONL_RECOVERY_SUFFIX",
    "_EXPORT_MANIFEST_RECOVERY_SUFFIX",
    "RecoveryError",
    "_ArtifactHandoffFacts",
    "_RecoveryArtifactPaths",
    "_RecoveryParentHandle",
    "_RecoveryFileCapture",
    "_recovery_artifact_paths",
    "_parent_chain_safe",
    "_artifact_parent_proof",
    "_open_recovery_parent_chain_no_follow",
    "_require_recovery_basename",
    "_after_recovery_parent_bound",
    "_after_recovery_manifest_source_proved",
    "_prove_recovery_manifest_source",
    "_prove_recovery_manifest_destination",
    "_strict_file_state",
    "_manifest_for_receipt",
    "_manifest_bytes",
    "_manifest_digest_for_receipt",
    "_artifact_expected_identity",
    "_unproven_artifact_code",
    "_prior_handoff_digests",
    "_remove_owned_recovery_artifact",
    "_recovery_parent_capture",
    "_recovery_parent_open_exclusive",
    "_remove_content_proven_artifact",
    "_recovery_cleanup_parent_gate",
    "_fsync_artifact_parent",
)

_MOVED_STORE_NAMES = (
    "_ARTIFACT_HANDOFF_META_PREFIX",
    "_strict_pair_file_state",
    "_artifact_parent_dirfd",
    "_after_artifact_parent_dirfd_bound",
    "_artifact_handoff_dirfd_entry",
    "_artifact_handoff_dirfd_absent",
    "_artifact_handoff_dirfd_reprove",
    "_require_artifact_identity_pair",
    "_artifact_handoff_meta_key",
    "_artifact_handoff_prior_record",
    "_artifact_handoff_meta_value",
    "_artifact_handoff_from_meta",
)


class TMSnapshotArtifactsModuleBoundariesTest(unittest.TestCase):
    def test_snapshot_artifacts_module_never_imports_the_owners(self) -> None:
        path = PROJECT_ROOT / "tm_snapshot_artifacts.py"
        py_compile.compile(str(path), doraise=True)
        modules = _imported_modules(path)
        for owner in _OWNER_MODULES:
            self.assertNotIn(owner, modules)
            self.assertFalse(
                any(module.startswith(owner) for module in modules)
            )
        self.assertEqual(_dynamic_imported_modules(path), set())
        source = path.read_text(encoding="utf-8")
        for owner in _OWNER_MODULES:
            self.assertNotIn(f"import {owner}", source)
            self.assertNotIn(f"from {owner} import", source)
        self.assertNotIn("SQLiteTMStore", source)

    def test_snapshot_artifacts_module_imports_only_allowed_family(
        self,
    ) -> None:
        path = PROJECT_ROOT / "tm_snapshot_artifacts.py"
        modules = _imported_modules(path)
        self.assertIn("tm_contracts", modules)
        self.assertIn("tm_activation_journal", modules)
        for module in modules:
            if module in {"tm_contracts", "tm_activation_journal"}:
                continue
            if module.startswith("tm_contracts"):
                continue
            if module in {"collections.abc"} or module.startswith("collections"):
                continue
            self.assertIn(module, sys.stdlib_module_names, module)

    def test_migration_keeps_export_authority_entry_points(self) -> None:
        service = tm_migration.TMMigrationService
        for method in (
            "export_jsonl",
            "refresh_configured_snapshot",
            "recover_configured_refresh",
            "preflight",
        ):
            with self.subTest(method=method):
                self.assertTrue(hasattr(service, method), method)
        for method in (
            "_run_arbitrary_export",
            "_publish_export_snapshot",
            "_export_report",
            "_export_failure",
        ):
            with self.subTest(method=method):
                self.assertTrue(hasattr(service, method), method)
        for name in (
            "_export_error_code",
            "_export_retryable",
            "_recovery_error_code",
            "_recovery_retryable",
            "_export_jsonl_row",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(tm_migration, name), name)

    def test_recovery_keeps_receipt_authority_entry_points(self) -> None:
        for name in (
            "IssuedReceiptFacts",
            "_RefreshRecoveryFacts",
            "_RefreshDecision",
            "recover_snapshot_publication",
            "_classify_refresh_receipts",
            "_replay_terminal_handoffs",
            "_terminal_handoff_row_blocker",
            "_SnapshotRecoveryPort",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(tm_snapshot_recovery, name), name
                )

    def test_store_keeps_ledger_authority_entry_points(self) -> None:
        self.assertTrue(hasattr(tm_sqlite_store, "ResourceStoreCoordinator"))
        for name in (
            "_SnapshotRecoveryPort",
            "_SQLiteGenerationView",
            "_recover_generation_publication",
            "_validate_next_generation_stage",
            "_configured_pair_diagnostics",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(tm_sqlite_store, name), name)
        store = tm_sqlite_store.SQLiteTMStore
        for method in (
            "register_issued_export_receipt",
            "register_issued_refresh_receipt",
            "register_issued_snapshot_receipt",
            "complete_issued_export_receipt",
            "complete_issued_refresh_receipt",
            "cancel_issued_export_receipt",
            "cancel_issued_refresh_receipt",
            "clear_issued_receipt_handoff",
            "record_export_recovery_handoff",
            "probe_issued_receipt_completed",
        ):
            with self.subTest(method=method):
                self.assertTrue(hasattr(store, method), method)

    def test_moved_migration_names_stay_late_bound_wrappers(self) -> None:
        for name in _MOVED_MIGRATION_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(tm_migration, name), name)

    def test_moved_recovery_names_stay_late_bound_wrappers(self) -> None:
        for name in _MOVED_RECOVERY_NAMES:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(tm_snapshot_recovery, name), name
                )

    def test_moved_store_names_stay_late_bound_wrappers(self) -> None:
        for name in _MOVED_STORE_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(tm_sqlite_store, name), name)

    def test_migration_wrapper_is_late_bound_to_the_new_module(self) -> None:
        with patch(
            "tm_snapshot_artifacts._fsync_file",
        ) as fsynced:
            tm_migration._fsync_file(3)
        fsynced.assert_called_once_with(descriptor=3)

    def test_recovery_wrapper_is_late_bound_to_the_new_module(self) -> None:
        parent = cast(Any, object())
        with patch(
            "tm_snapshot_artifacts._recovery_parent_capture",
        ) as captured:
            result = tm_snapshot_recovery._recovery_parent_capture(
                parent,
                "candidate.jsonl",
                "RECOVERY_MANIFEST_TEMP",
            )
        captured.assert_called_once_with(
            parent=parent,
            name="candidate.jsonl",
            asset_kind="RECOVERY_MANIFEST_TEMP",
        )
        self.assertIs(result, captured.return_value)

    def test_store_wrapper_is_late_bound_to_the_new_module(self) -> None:
        with patch(
            "tm_snapshot_artifacts._artifact_parent_dirfd",
            return_value=9,
        ) as bound:
            result = tm_sqlite_store._artifact_parent_dirfd(
                Path("/tmp/example"),
                None,
            )
        self.assertEqual(result, 9)
        bound.assert_called_once()

    def test_store_handoff_codec_wrapper_is_late_bound(self) -> None:
        with patch(
            "tm_snapshot_artifacts._artifact_handoff_from_meta",
        ) as decoded:
            result = tm_sqlite_store._artifact_handoff_from_meta(
                "handoff",
                '{"jsonl_digest": "abc"}',
            )
        decoded.assert_called_once_with(
            key="handoff",
            value='{"jsonl_digest": "abc"}',
        )
        self.assertIs(result, decoded.return_value)

    def test_migration_stream_seams_stay_in_the_owner_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "candidate.jsonl.tmp"
            record = object()
            with patch(
                "tm_migration._export_jsonl_row",
                return_value={
                    "record_id": "r1",
                    "source": "s",
                    "target": "t",
                    "speaker": None,
                },
            ) as row_seam, patch(
                "tm_migration._fsync_file",
            ) as fsync_seam:
                digest, count, identity = (
                    tm_migration._stream_export_jsonl_temp(
                        candidate,
                        (record,),
                    )
                )
            row_seam.assert_called_once_with(record)
            fsync_seam.assert_called_once()
            self.assertEqual(count, 1)
            self.assertGreater(len(digest), 0)
            self.assertIsInstance(identity.device, int)
            self.assertIsInstance(identity.inode, int)

    def test_migration_replace_seam_is_injected_from_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.jsonl"
            destination = root / "destination.jsonl"
            source.write_bytes(b"published bytes")
            observed = os.stat(source)
            identity = (observed.st_dev, observed.st_ino)
            with patch(
                "tm_migration._after_replace_source_proved",
            ) as proved:
                tm_migration._replace_path(
                    source,
                    destination,
                    expected_source_identity=identity,
                    expected_destination_digest=None,
                    expected_destination_identity=None,
                )
            proved.assert_called_once()
            self.assertEqual(proved.call_args[0][2], identity)
            self.assertEqual(
                destination.read_bytes(),
                b"published bytes",
            )
            self.assertFalse(source.exists())

    def test_export_parent_bind_seam_is_injected_from_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "candidate.jsonl"
            with patch(
                "tm_migration._after_export_parent_chain_validated",
            ) as validated:
                handle = tm_migration._ExportParentHandle.bind(destination)
            validated.assert_called_once_with(destination)
            self.assertIsInstance(
                handle,
                tm_snapshot_artifacts._ExportParentHandle,
            )
            handle.close()

    def test_recovery_proof_seam_is_injected_from_owner(self) -> None:
        capture = type(
            "Capture",
            (),
            {
                "digest": "expected-digest",
                "identity": type(
                    "Identity", (), {"device": 7, "inode": 9}
                )(),
            },
        )()
        with patch(
            "tm_snapshot_recovery._recovery_parent_capture",
            return_value=capture,
        ) as captured:
            tm_snapshot_recovery._prove_recovery_manifest_source(
                Path("/tmp/candidate.jsonl"),
                "candidate.jsonl",
                expected_digest="expected-digest",
                expected_identity=(7, 9),
                parent=cast(Any, object()),
            )
        captured.assert_called_once()

    def test_store_dirfd_bound_seam_observes_owner_namespace(self) -> None:
        with patch(
            "tm_sqlite_store._after_artifact_parent_dirfd_bound",
        ) as bound:
            tm_sqlite_store._after_artifact_parent_dirfd_bound(
                Path("/tmp/candidate.jsonl"),
                (7, 9),
            )
        bound.assert_called_once_with(
            Path("/tmp/candidate.jsonl"),
            (7, 9),
        )

    def test_error_class_identity_across_wrappers(self) -> None:
        self.assertIs(
            tm_migration.ExportPreflightError,
            tm_snapshot_artifacts.ExportPreflightError,
        )
        self.assertIs(
            tm_snapshot_recovery.RecoveryError,
            tm_snapshot_artifacts.RecoveryError,
        )
        self.assertIs(
            tm_migration._NO_DESTINATION_PROOF,
            tm_snapshot_artifacts._NO_DESTINATION_PROOF,
        )
        self.assertTrue(
            issubclass(
                tm_migration._ExportParentHandle,
                tm_snapshot_artifacts._ExportParentHandle,
            )
        )

    def test_edited_modules_compile(self) -> None:
        for name in (
            "tm_migration.py",
            "tm_snapshot_recovery.py",
            "tm_sqlite_store.py",
            "tm_snapshot_artifacts.py",
        ):
            with self.subTest(name=name):
                py_compile.compile(
                    str(PROJECT_ROOT / name),
                    doraise=True,
                )


if __name__ == "__main__":
    unittest.main()
