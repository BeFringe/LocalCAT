"""Hostile carrier tests for Cluster 2C's public ProjectPackage boundary.

The mutations stay at the ZIP byte boundary: no candidate, journal, or import
plan is manufactured by a test.  A package must therefore be rejected before
it can become a semantic workspace or an installed artifact.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
from types import ModuleType
from unittest import mock
import unittest
import zlib

from project_save import ProjectSaveService, SaveJournalState, WorkspaceSaveBaseline
from project_workspace import ProjectWorkspaceService
from project_workspace_identity import ProjectWorkspaceError
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    stage_selected_project_documents,
)


_LOCAL = struct.Struct("<4s5H3L2H")
_CENTRAL = struct.Struct("<4s6H3L5H2L")
_EOCD = struct.Struct("<4s4H2LH")


def _module() -> ModuleType:
    import importlib

    return importlib.import_module("project_package")


def _project(root: Path, relative: str, source: str, target: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": relative,
                "source_locale": "en",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": "shared",
                        "source": source,
                        "target": target,
                        "speaker": "",
                        "confirmed": bool(target),
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _package(root: Path, relative: str = "valid.localcat-project") -> Path:
    first = _project(root, "chapters/a.json", "Source A", "Target A")
    second = _project(root, "chapters/b.json", "Source B", "Target B")
    staged = stage_selected_project_documents(
        root,
        (first, second),
        SelectedProjectDocumentsRequest(
            name="Carrier security",
            source_locale="en",
            target_locale="zh-CN",
        ),
    )
    workspace = ProjectWorkspaceService(
        staged.workspace,
        staged.origin_binding,
        session_id="zip-security",
        revision=3,
    )
    target = root / relative
    _module().ProjectPackageService().export_workspace(
        ProjectSaveService(workspace, baseline=None), target
    )
    return target


def _layout(payload: bytes) -> tuple[list[dict[str, int]], int]:
    """Return local/CD positions for a strict stored package without private APIs."""

    eocd_at = len(payload) - _EOCD.size
    _signature, _disk, _central_disk, _on_disk, count, cd_size, cd_at, comment = (
        _EOCD.unpack_from(payload, eocd_at)
    )
    if comment or cd_at + cd_size != eocd_at:
        raise AssertionError("fixture is not a strict C2C package")
    entries: list[dict[str, int]] = []
    local_at = 0
    for _ in range(count):
        (
            _signature,
            _needed,
            _flags,
            _method,
            _time,
            _date,
            _crc,
            _compressed,
            size,
            name_count,
            extra_count,
        ) = _LOCAL.unpack_from(payload, local_at)
        entries.append(
            {
                "local": local_at,
                "local_name": local_at + _LOCAL.size,
                "data": local_at + _LOCAL.size + name_count + extra_count,
                "size": size,
            }
        )
        local_at += _LOCAL.size + name_count + extra_count + size
    central_at = cd_at
    for entry in entries:
        values = _CENTRAL.unpack_from(payload, central_at)
        name_count, extra_count, comment_count = values[10:13]
        entry["central"] = central_at
        entry["central_name"] = central_at + _CENTRAL.size
        central_at += _CENTRAL.size + name_count + extra_count + comment_count
    return entries, eocd_at


def _u16(payload: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<H", payload, offset, value)


def _u32(payload: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<L", payload, offset, value)


def _copy_package(root: Path, name: str, payload: bytes) -> Path:
    target = root / name
    target.write_bytes(payload)
    return target


def _rewrite_member_and_crc(
    payload: bytearray,
    entry: dict[str, int],
    old: bytes,
    new: bytes,
) -> None:
    """Make a semantic mutation while retaining a valid raw ZIP CRC pair."""

    if len(old) != len(new):
        raise AssertionError("same-length semantic mutation required")
    start = entry["data"]
    end = start + entry["size"]
    member = bytes(payload[start:end])
    position = member.find(old)
    if position < 0:
        raise AssertionError(f"fixture member lacks {old!r}")
    payload[start + position : start + position + len(old)] = new
    crc = zlib.crc32(payload[start:end]) & 0xFFFFFFFF
    _u32(payload, entry["local"] + 14, crc)
    _u32(payload, entry["central"] + 16, crc)


class Cluster2CSealedArtifactTests(unittest.TestCase):
    def test_same_inode_same_length_rewrite_during_validate_never_returns_report(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            package = _package(root)
            original = package.read_bytes()
            before = package.stat()
            read_entry = module._read_entry
            calls = 0

            def rewrite_after_last_member(*args, **kwargs):
                nonlocal calls
                result = read_entry(*args, **kwargs)
                calls += 1
                # Fixture is manifest + two document members + two sources.
                if calls == 5:
                    changed = bytearray(original)
                    changed[1] ^= 1
                    with package.open("r+b", buffering=0) as stream:
                        stream.seek(0)
                        stream.write(changed)
                        stream.flush()
                        os.fsync(stream.fileno())
                return result

            with mock.patch("project_package._read_entry", side_effect=rewrite_after_last_member):
                with self.assertRaises(ProjectWorkspaceError) as rejected:
                    module.ProjectPackageService().validate(package)
            after = package.stat()
            self.assertGreaterEqual(calls, 5)
            self.assertEqual((before.st_dev, before.st_ino, before.st_size), (after.st_dev, after.st_ino, after.st_size))
            self.assertIn(
                rejected.exception.code,
                {"PROJECT.PACKAGE.SOURCE_STALE", "PROJECT.PACKAGE.DIGEST_MISMATCH"},
            )


class Cluster2CParentBindingTests(unittest.TestCase):
    def test_overwrite_rejects_same_bytes_on_a_new_destination_inode(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            target = _package(root, "bound.localcat-project")
            opened = module.ProjectPackageService().open(target)
            document = opened.workspace.documents[0]
            overlay = document.editing_overlay[0]
            edited = replace(
                opened.workspace,
                documents=(
                    replace(
                        document,
                        editing_overlay=(
                            replace(overlay, target="changed after open", confirmed=False),
                            *document.editing_overlay[1:],
                        ),
                    ),
                    *opened.workspace.documents[1:],
                ),
            )
            workspace = ProjectWorkspaceService(
                edited,
                None,
                session_id="inode-save",
                revision=4,
            )
            save = ProjectSaveService(
                workspace,
                baseline=WorkspaceSaveBaseline.from_workspace(
                    opened.workspace,
                    workspace_revision=4,
                    saved_package_digest=opened.validation.workspace_content_digest,
                ),
            )
            real_copy = module._port_copy_regular
            replaced = False

            def replace_after_lkg_copy(source, destination, **kwargs):
                nonlocal replaced
                copied = real_copy(source, destination, **kwargs)
                if not replaced:
                    payload = target.read_bytes()
                    replacement = target.with_name("same-bytes-new-inode.tmp")
                    replacement.write_bytes(payload)
                    os.replace(replacement, target)
                    replaced = True
                return copied

            with mock.patch(
                "project_package._port_copy_regular",
                side_effect=replace_after_lkg_copy,
            ):
                result = module.ProjectPackageService().save_workspace(
                    save,
                    target,
                    persistence_binding=opened.persistence_binding,
                )
            self.assertTrue(replaced)
            self.assertIsNot(result.save_report.journal_state, SaveJournalState.COMMITTED)
            self.assertIsNone(result.receipt)

    def test_first_save_unknown_postpublication_target_requires_recovery(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            first = _project(root, "chapters/a.json", "A", "TA")
            second = _project(root, "chapters/b.json", "B", "TB")
            staged = stage_selected_project_documents(
                root,
                (first, second),
                SelectedProjectDocumentsRequest(
                    name="Unknown publication",
                    source_locale="en",
                    target_locale="zh-CN",
                ),
            )
            workspace = ProjectWorkspaceService(
                staged.workspace,
                staged.origin_binding,
                session_id="unknown-publication",
                revision=1,
            )
            save = ProjectSaveService(workspace, baseline=None)
            target = root / "first.localcat-project"
            real_validate = module._port_validate_artifact
            replaced = False

            def replace_before_first_target_readback(path):
                nonlocal replaced
                if path == target and not replaced:
                    replacement = target.with_name("unknown-concurrent.tmp")
                    replacement.write_bytes(b"unknown concurrent target")
                    os.replace(replacement, target)
                    replaced = True
                return real_validate(path)

            with mock.patch(
                "project_package._port_validate_artifact",
                side_effect=replace_before_first_target_readback,
            ):
                result = module.ProjectPackageService().save_workspace(save, target)
            self.assertTrue(replaced)
            self.assertIs(
                result.save_report.journal_state,
                SaveJournalState.RECOVERY_REQUIRED,
            )
            self.assertTrue(result.save_report.recovery_required)
            self.assertEqual(target.read_bytes(), b"unknown concurrent target")
            self.assertIsNone(result.receipt)

    def test_terminal_cleanup_failure_is_cold_recoverable_after_lkg_removal(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            target = _package(root, "cleanup.localcat-project")
            opened = module.ProjectPackageService().open(target)
            document = opened.workspace.documents[0]
            overlay = document.editing_overlay[0]
            edited = replace(
                opened.workspace,
                documents=(
                    replace(
                        document,
                        editing_overlay=(
                            replace(overlay, target="cleanup edit", confirmed=False),
                            *document.editing_overlay[1:],
                        ),
                    ),
                    *opened.workspace.documents[1:],
                ),
            )
            workspace = ProjectWorkspaceService(
                edited,
                None,
                session_id="cleanup-session",
                revision=5,
            )
            save = ProjectSaveService(
                workspace,
                baseline=WorkspaceSaveBaseline.from_workspace(
                    opened.workspace,
                    workspace_revision=5,
                    saved_package_digest=opened.validation.workspace_content_digest,
                ),
            )
            real_unlink = module._unlink_in_bound_parent
            failed = False

            def fail_first_journal_cleanup(path, expected, **kwargs):
                nonlocal failed
                if "localcat-save-journal-v1" in path.name and not failed:
                    failed = True
                    raise OSError("journal cleanup interrupted")
                return real_unlink(path, expected, **kwargs)

            with mock.patch(
                "project_package._unlink_in_bound_parent",
                side_effect=fail_first_journal_cleanup,
            ):
                result = module.ProjectPackageService().save_workspace(
                    save,
                    target,
                    persistence_binding=opened.persistence_binding,
                )
            self.assertTrue(failed)
            self.assertIs(
                result.save_report.journal_state,
                SaveJournalState.RECOVERY_REQUIRED,
            )

            fresh = module.ProjectPackageService()
            preview = fresh.inspect_recovery(target)
            self.assertIsNotNone(preview)
            self.assertEqual(
                preview.available_actions,
                (module.RecoveryAction.COMPLETE_COMMIT,),
            )
            recovered = fresh.recover(
                target,
                preview.operation_id,
                module.RecoveryAction.COMPLETE_COMMIT,
            )
            self.assertFalse(recovered.recovery_required)
            self.assertIsNone(module.ProjectPackageService().inspect_recovery(target))

    def test_preview_binds_real_parents_and_rename_replacement_is_zero_mutation(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            source_parent = root / "source-parent"
            destination_parent = root / "destination-parent"
            source_parent.mkdir(parents=True)
            destination_parent.mkdir()
            source = _package(source_parent, "source.localcat-project")
            destination = destination_parent / "destination.localcat-project"
            service = module.ProjectPackageService()

            source_preview = service.preview_import(source, destination)
            moved_source_parent = root / "source-parent-before-preview"
            os.replace(source_parent, moved_source_parent)
            source_parent.mkdir()
            with self.assertRaises(ProjectWorkspaceError) as source_stale:
                service.apply_import(source_preview.operation_id)
            self.assertEqual(source_stale.exception.code, "PROJECT.PACKAGE.SOURCE_STALE")
            self.assertFalse(destination.exists())
            self.assertFalse((moved_source_parent / destination.name).exists())

            source = moved_source_parent / source.name
            destination_preview = service.preview_import(source, destination)
            moved_destination_parent = root / "destination-parent-before-preview"
            os.replace(destination_parent, moved_destination_parent)
            destination_parent.mkdir()
            with self.assertRaises(ProjectWorkspaceError) as destination_stale:
                service.apply_import(destination_preview.operation_id)
            self.assertEqual(destination_stale.exception.code, "PROJECT.PACKAGE.DESTINATION_STALE")
            self.assertFalse(destination.exists())
            self.assertFalse((moved_destination_parent / destination.name).exists())

    def test_selected_ancestor_symlink_is_canonicalized_then_real_parent_is_bound(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            actual = root / "actual"
            actual.mkdir(parents=True)
            selected = root / "selected"
            try:
                selected.symlink_to(actual, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink unavailable: {error}")
            source = _package(actual, "source.localcat-project")
            selected_source = selected / source.name
            selected_destination = selected / "installed.localcat-project"
            service = module.ProjectPackageService()
            preview = service.preview_import(selected_source, selected_destination)
            receipt = service.apply_import(preview.operation_id)
            self.assertTrue(receipt.durable)
            self.assertTrue((actual / "installed.localcat-project").is_file())


class Cluster2CRawZipEnvelopeTests(unittest.TestCase):
    def test_raw_envelope_matrix_is_rejected_before_workspace_publication(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            original = _package(root).read_bytes()
            entries, eocd_at = _layout(original)
            first = entries[0]
            second = entries[1]
            third = entries[2]

            def changed(mutator) -> bytes:
                payload = bytearray(original)
                mutator(payload)
                return bytes(payload)

            second_local_name = original[
                second["local_name"] : second["data"]
            ]

            def rename_second_everywhere(payload: bytearray, first_byte: int) -> None:
                payload[second["local_name"]] = first_byte
                payload[second["central_name"]] = first_byte

            def duplicate_second_everywhere(payload: bytearray) -> None:
                payload[
                    third["local_name"] : third["local_name"] + len(second_local_name)
                ] = second_local_name
                payload[
                    third["central_name"] : third["central_name"] + len(second_local_name)
                ] = second_local_name

            def manifest_schema(payload: bytearray) -> None:
                _rewrite_member_and_crc(
                    payload,
                    first,
                    b"localcat-project-package-manifest-v1",
                    b"xocalcat-project-package-manifest-v1",
                )

            def manifest_profile(payload: bytearray) -> None:
                _rewrite_member_and_crc(
                    payload,
                    first,
                    b"localcat-project-package-zip-v1",
                    b"xocalcat-project-package-zip-v1",
                )

            def document_codec(payload: bytearray) -> None:
                _rewrite_member_and_crc(
                    payload,
                    second,
                    b"localcat-json",
                    b"xocalcat-json",
                )

            def declared_digest(payload: bytearray) -> None:
                start = first["data"]
                end = start + first["size"]
                member = bytes(payload[start:end])
                marker = b'"sha256":"'
                position = member.find(marker)
                if position < 0:
                    raise AssertionError("fixture manifest lacks a member digest")
                at = start + position + len(marker)
                payload[at] = ord("f") if payload[at] != ord("f") else ord("e")
                crc = zlib.crc32(payload[start:end]) & 0xFFFFFFFF
                _u32(payload, first["local"] + 14, crc)
                _u32(payload, first["central"] + 16, crc)

            # Offsets are deliberately exact so every case isolates one raw
            # envelope promise rather than relying on zipfile's permissiveness.
            cases = {
                "local-name": changed(lambda p: p.__setitem__(first["local_name"], ord("x"))),
                "central-name": changed(lambda p: p.__setitem__(first["central_name"], ord("x"))),
                "local-crc": changed(lambda p: _u32(p, first["local"] + 14, 1)),
                "central-crc": changed(lambda p: _u32(p, first["central"] + 16, 1)),
                "local-size": changed(lambda p: _u32(p, first["local"] + 22, 1)),
                "central-size": changed(lambda p: _u32(p, first["central"] + 24, 1)),
                "central-offset": changed(lambda p: _u32(p, second["central"] + 42, 0)),
                "gap": changed(lambda p: _u32(p, first["local"] + 18, first["size"] + 1)),
                "overlap": changed(lambda p: _u32(p, first["local"] + 18, max(0, first["size"] - 1))),
                "local-flags": changed(lambda p: _u16(p, first["local"] + 6, 8)),
                "central-data-descriptor": changed(lambda p: _u16(p, first["central"] + 8, 8)),
                "central-encrypted": changed(lambda p: _u16(p, first["central"] + 8, 1)),
                "local-extra": changed(lambda p: _u16(p, first["local"] + 28, 1)),
                "central-extra": changed(lambda p: _u16(p, first["central"] + 30, 1)),
                "central-comment": changed(lambda p: _u16(p, first["central"] + 32, 1)),
                "external-symlink": changed(lambda p: _u32(p, first["central"] + 38, (0o120777) << 16)),
                "external-executable": changed(lambda p: _u32(p, first["central"] + 38, (0o100755) << 16)),
                "prefix": b"prefix" + original,
                "suffix": original + b"suffix",
                "undeclared-and-missing-member": changed(
                    lambda p: rename_second_everywhere(p, ord("e"))
                ),
                "duplicate-member": changed(duplicate_second_everywhere),
                "manifest-schema": changed(manifest_schema),
                "carrier-profile": changed(manifest_profile),
                "document-codec": changed(document_codec),
                "declared-digest": changed(declared_digest),
                "eocd-comment": changed(lambda p: _u16(p, eocd_at + 20, 1)),
            }
            for label, payload in cases.items():
                with self.subTest(label=label):
                    candidate = _copy_package(root, f"{label}.localcat-project", payload)
                    with self.assertRaises(ProjectWorkspaceError) as rejected:
                        module.ProjectPackageService().validate(candidate)
                    self.assertIn(
                        rejected.exception.code,
                        {
                            "PROJECT.PACKAGE.FORMAT_UNSUPPORTED",
                            "PROJECT.PACKAGE.MEMBER_INVALID",
                            "PROJECT.PACKAGE.MANIFEST_INVALID",
                            "PROJECT.PACKAGE.DIGEST_MISMATCH",
                            "PROJECT.PACKAGE.SOURCE_STALE",
                        },
                    )


class Cluster2CReceiptAndPreviewClosureTests(unittest.TestCase):
    def test_result_surface_returns_cold_opened_install_bound_to_receipt(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            source = _package(root, "source.localcat-project")
            destination = root / "destination.localcat-project"
            service = module.ProjectPackageService()
            preview = service.preview_import(source, destination)
            result = service.apply_import_result(preview.operation_id)

            self.assertIs(type(result), module.ProjectPackageImportResult)
            self.assertIs(type(result.installed), module.OpenedProjectPackage)
            self.assertIs(type(result.receipt), module.ProjectPackageImportReceipt)
            self.assertEqual(
                result.installed.validation.artifact_digest,
                result.receipt.destination_after_digest,
            )
            self.assertEqual(result.installed.path, destination)
            forged_receipt = replace(
                result.receipt,
                document_count=1,
                document_results=(result.receipt.document_results[0],),
            )
            with self.assertRaises(ProjectWorkspaceError):
                module.ProjectPackageImportResult(
                    result.installed,
                    forged_receipt,
                )

    def test_preview_and_receipt_reject_wrong_closed_contract_shapes(self) -> None:
        module = _module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            source = _package(root, "source.localcat-project")
            destination = root / "destination.localcat-project"
            service = module.ProjectPackageService()
            preview = service.preview_import(source, destination)
            receipt = service.apply_import(preview.operation_id)

            invalid = (
                lambda: replace(preview, operation_id="package-import-prefix-only"),
                lambda: replace(preview, destination_exists=1),
                lambda: replace(preview, required_decision_identities=(object(),)),
                lambda: replace(receipt, durable=True, recovery_required=True),
                lambda: replace(receipt, operation_id="package-import-prefix-only"),
                lambda: replace(
                    receipt,
                    operation_kind=module.ProjectPackageOperationKind.EXPORT_COPY,
                ),
                lambda: replace(receipt, member_digests=(object(),)),
                lambda: replace(receipt, document_results=(object(),)),
                lambda: replace(receipt, reconciliation=object()),
            )
            for constructor in invalid:
                with self.subTest(constructor=constructor):
                    with self.assertRaises(ProjectWorkspaceError) as rejected:
                        constructor()
                    self.assertEqual(rejected.exception.code, "PROJECT.PACKAGE.MANIFEST_INVALID")
