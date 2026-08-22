"""Cluster 2C ProjectPackage logical, carrier, and transaction acceptance."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
from types import ModuleType
from unittest import mock
import unittest
import zipfile

from project_save import ProjectSaveService, SaveJournalState
from project_workspace import ProjectWorkspaceService, workspace_content_digest_v1
from project_workspace_contracts import CodecPrivateMemberRef
from project_workspace_identity import ProjectWorkspaceError
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    stage_selected_project_documents,
)


def _cluster2c() -> ModuleType:
    try:
        module = importlib.import_module("project_package")
    except ModuleNotFoundError:
        raise AssertionError(
            "Cluster 2C RED: public module project_package is missing"
        ) from None
    required = (
        "PROJECT_PACKAGE_CARRIER_PROFILE",
        "PROJECT_PACKAGE_MANIFEST_SCHEMA",
        "ProjectPackageBlobSource",
        "ProjectPackageExportResult",
        "ProjectPackageImportMode",
        "ProjectPackageImportPreview",
        "ProjectPackageImportReceipt",
        "ProjectPackageService",
        "ProjectPackageValidationReport",
    )
    missing = tuple(name for name in required if not hasattr(module, name))
    if missing:
        raise AssertionError(
            f"Cluster 2C RED: package public contract is missing {missing!r}"
        )
    return module


def _write_project(
    root: Path,
    relative: str,
    *,
    source: str,
    target: str,
) -> Path:
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
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _fixture(root: Path):
    first = _write_project(
        root,
        "chapters/a.json",
        source="Source A",
        target="Target A",
    )
    second = _write_project(
        root,
        "chapters/b.json",
        source="Source B",
        target="Target B",
    )
    staged = stage_selected_project_documents(
        root,
        (first, second),
        SelectedProjectDocumentsRequest(
            name="Portable project",
            source_locale="en",
            target_locale="zh-CN",
        ),
    )
    workspace_service = ProjectWorkspaceService(
        staged.workspace,
        staged.origin_binding,
        session_id="session-c2c",
        revision=7,
    )
    save_service = ProjectSaveService(workspace_service, baseline=None)
    return staged, workspace_service, save_service


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _stream_bytes(stream: object) -> bytes:
    chunks: list[bytes] = []
    while block := stream.read(17):  # type: ignore[attr-defined]
        chunks.append(block)
    return b"".join(chunks)


class Cluster2CProjectPackageRoundTripTests(unittest.TestCase):
    def test_real_two_document_export_is_deterministic_and_cold_reopens(self) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            staged, workspace_service, save_service = _fixture(root)
            first = root / "first.localcat-project"
            second = root / "second.localcat-project"
            package = module.ProjectPackageService()

            exported = package.export_workspace(save_service, first)
            # Determinism compares the same immutable workspace facts. A new
            # intake intentionally issues a new project identity.
            save_service2 = ProjectSaveService(workspace_service, baseline=None)
            exported2 = module.ProjectPackageService().export_workspace(
                save_service2,
                second,
            )

            self.assertIs(type(exported), module.ProjectPackageExportResult)
            self.assertEqual(exported.save_report.journal_state, SaveJournalState.COMMITTED)
            self.assertTrue(exported.receipt.durable)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(exported.receipt.artifact_digest, _sha(first))
            self.assertEqual(exported2.receipt.artifact_digest, _sha(second))
            self.assertEqual(
                exported.receipt.workspace_content_digest,
                workspace_content_digest_v1(save_service.saved_workspace_snapshot),
            )
            with self.assertRaises(ProjectWorkspaceError):
                replace(
                    exported.receipt,
                    document_results=tuple(
                        module.ProjectPackageDocumentResult(
                            item.document_id,
                            "installed",
                        )
                        for item in exported.receipt.document_results
                    ),
                )
            with self.assertRaises(ProjectWorkspaceError):
                replace(
                    exported.receipt,
                    operation_id="package-import-" + "0" * 64,
                )
            with self.assertRaises(ProjectWorkspaceError):
                replace(
                    exported,
                    receipt=replace(
                        exported.receipt,
                        operation_id="save-" + "0" * 64,
                    ),
                )
            with self.assertRaises(ProjectWorkspaceError):
                replace(
                    exported,
                    receipt=replace(
                        exported.receipt,
                        document_results=tuple(
                            module.ProjectPackageDocumentResult(
                                item.document_id,
                                "unchanged",
                            )
                            for item in exported.receipt.document_results
                        ),
                    ),
                )

            validation = package.validate(first)
            opened = package.open(first)
            self.assertIs(type(validation), module.ProjectPackageValidationReport)
            self.assertEqual(validation.document_count, 2)
            self.assertEqual(validation.segment_count, 2)
            self.assertEqual(
                tuple(
                    segment.identity.local_segment_id
                    for document in opened.workspace.documents
                    for segment in document.segments
                ),
                ("shared", "shared"),
            )
            self.assertEqual(opened.workspace, save_service.saved_workspace_snapshot)
            source_payloads: list[bytes] = []
            for entry in opened.manifest.documents:
                with opened.open_member(entry.source_member.path) as stream:
                    source_payloads.append(_stream_bytes(stream))
            self.assertEqual(
                tuple(source_payloads),
                tuple(
                    path.read_bytes()
                    for path in (root / "chapters/a.json", root / "chapters/b.json")
                ),
            )
            self.assertEqual(staged.workspace.project_id, opened.workspace.project_id)

    def test_fresh_preview_is_read_only_single_use_and_apply_installs_exact_bytes(self) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            _staged, _workspace_service, save_service = _fixture(root)
            source = root / "source.localcat-project"
            destination = root / "installed.localcat-project"
            module.ProjectPackageService().export_workspace(save_service, source)
            source_sha = _sha(source)
            service = module.ProjectPackageService()

            preview = service.preview_import(source, destination)
            self.assertIs(type(preview), module.ProjectPackageImportPreview)
            self.assertEqual(preview.mode, module.ProjectPackageImportMode.NEW)
            self.assertFalse(destination.exists())
            receipt = service.apply_import(preview.operation_id)

            self.assertIs(type(receipt), module.ProjectPackageImportReceipt)
            self.assertTrue(receipt.durable)
            self.assertEqual(receipt.destination_after_digest, source_sha)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(service.open(destination).workspace.project_id, preview.project_id)
            with self.assertRaises(ProjectWorkspaceError) as stale:
                service.apply_import(preview.operation_id)
            self.assertEqual(stale.exception.code, "PROJECT.PACKAGE.PREVIEW_STALE")

    def test_preview_binds_source_and_destination_without_partial_mutation(self) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            _staged, _workspace_service, save_service = _fixture(root)
            source = root / "source.localcat-project"
            destination = root / "destination.localcat-project"
            module.ProjectPackageService().export_workspace(save_service, source)
            service = module.ProjectPackageService()

            preview = service.preview_import(source, destination)
            source.write_bytes(source.read_bytes() + b"x")
            with self.assertRaises(ProjectWorkspaceError) as source_stale:
                service.apply_import(preview.operation_id)
            self.assertEqual(source_stale.exception.code, "PROJECT.PACKAGE.SOURCE_STALE")
            self.assertFalse(destination.exists())

            # A first save has no LKG and must not overwrite the now-corrupt
            # package merely because a caller reuses its path.
            source.unlink()
            _staged, _workspace_service, save_service = _fixture(root)
            module.ProjectPackageService().export_workspace(save_service, source)
            preview = service.preview_import(source, destination)
            destination.write_bytes(b"unrelated-existing-target")
            before = destination.read_bytes()
            with self.assertRaises(ProjectWorkspaceError) as destination_stale:
                service.apply_import(preview.operation_id)
            self.assertEqual(
                destination_stale.exception.code,
                "PROJECT.PACKAGE.DESTINATION_STALE",
            )
            self.assertEqual(destination.read_bytes(), before)


class Cluster2CStrictZipEnvelopeTests(unittest.TestCase):
    def _package(self, root: Path):
        module = _cluster2c()
        _staged, _workspace_service, save_service = _fixture(root)
        package = root / "valid.localcat-project"
        module.ProjectPackageService().export_workspace(save_service, package)
        return module, package

    def test_prefix_suffix_and_duplicate_members_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            module, package = self._package(root)
            original = package.read_bytes()
            bad_values = {
                "prefix": b"stub" + original,
                "suffix": original + b"x",
            }
            duplicate = root / "duplicate.zip"
            with zipfile.ZipFile(duplicate, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("manifest.json", b"{}")
                archive.writestr("manifest.json", b"{}")
            bad_values["duplicate"] = duplicate.read_bytes()
            for label, payload in bad_values.items():
                with self.subTest(label=label):
                    candidate = root / f"{label}.zip"
                    candidate.write_bytes(payload)
                    with self.assertRaises(ProjectWorkspaceError) as failure:
                        module.ProjectPackageService().validate(candidate)
                    self.assertIn(
                        failure.exception.code,
                        {
                            "PROJECT.PACKAGE.FORMAT_UNSUPPORTED",
                            "PROJECT.PACKAGE.MEMBER_INVALID",
                        },
                    )

    def test_compressed_extra_zip64_and_crc_corruption_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            module, package = self._package(root)
            compressed = root / "compressed.zip"
            with zipfile.ZipFile(compressed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", b"{}")
            extra = root / "extra.zip"
            info = zipfile.ZipInfo("manifest.json")
            info.extra = b"\x01\x00\x00\x00"
            with zipfile.ZipFile(extra, "w", allowZip64=False) as archive:
                archive.writestr(info, b"{}")
            zip64 = root / "zip64.zip"
            with zipfile.ZipFile(zip64, "w", allowZip64=True) as archive:
                with archive.open("manifest.json", "w", force_zip64=True) as member:
                    member.write(b"{}")
            corrupt = root / "crc.zip"
            payload = bytearray(package.read_bytes())
            marker = b'"schema":"localcat-project-package-manifest-v1"'
            offset = payload.find(marker)
            self.assertGreaterEqual(offset, 0)
            payload[offset] ^= 1
            corrupt.write_bytes(payload)

            for candidate in (compressed, extra, zip64, corrupt):
                with self.subTest(candidate=candidate.name):
                    with self.assertRaises(ProjectWorkspaceError):
                        module.ProjectPackageService().validate(candidate)

    def test_physical_names_are_exact_ascii_closed_and_not_extracted(self) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            for name in ("../escape", "/absolute", "documents\\bad.json"):
                candidate = root / (hashlib.sha256(name.encode()).hexdigest() + ".zip")
                with zipfile.ZipFile(candidate, "w") as archive:
                    archive.writestr("manifest.json", b"{}")
                    archive.writestr(name, b"payload")
                with self.assertRaises(ProjectWorkspaceError):
                    module.ProjectPackageService().validate(candidate)
            self.assertEqual(tuple(root.rglob("escape")), ())


class Cluster2COpaqueMemberAndRecoveryTests(unittest.TestCase):
    def test_codec_private_member_round_trips_bit_exact_via_bounded_blob_source(self) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            staged, workspace_service, _save_service = _fixture(root)
            blob = root / "opaque.bin"
            block = bytes(range(256)) * 4096
            with blob.open("wb") as stream:
                for _ in range(16):
                    stream.write(block)
            blob_digest = _sha(blob)
            first = staged.workspace.documents[0]
            member_path = (
                f"codec-private/{first.document_id}/{blob_digest}.bin"
            )
            private_ref = CodecPrivateMemberRef(
                member_path=member_path,
                sha256=blob_digest,
                byte_count=blob.stat().st_size,
                codec_identity=first.codec_identity,
                profile_version="test-opaque-v1",
            )
            workspace = replace(
                staged.workspace,
                documents=(
                    replace(first, codec_private_member=private_ref),
                    staged.workspace.documents[1],
                ),
            )
            workspace_service = ProjectWorkspaceService(
                workspace,
                staged.origin_binding,
                session_id="session-private",
                revision=3,
            )
            save_service = ProjectSaveService(workspace_service, baseline=None)
            source = module.ProjectPackageBlobSource.from_path(
                document_id=first.document_id,
                member_path=member_path,
                path=blob,
                expected_sha256=blob_digest,
                expected_byte_count=blob.stat().st_size,
            )
            target = root / "private.localcat-project"

            module.ProjectPackageService().export_workspace(
                save_service,
                target,
                codec_private_sources=(source,),
            )
            opened = module.ProjectPackageService().open(target)

            self.assertEqual(opened.workspace.documents[0].codec_private_member, private_ref)
            with opened.open_member(
                member_path,
                codec_identity=first.codec_identity,
            ) as stream:
                self.assertEqual(_stream_bytes(stream), blob.read_bytes())
            with self.assertRaises(ProjectWorkspaceError) as unavailable:
                with opened.open_member(
                    member_path,
                    codec_identity=replace(
                        first.codec_identity,
                        codec_version="foreign-version",
                    ),
                ):
                    pass
            self.assertEqual(
                unavailable.exception.code,
                "PROJECT.PACKAGE.CODEC_UNAVAILABLE",
            )
            with self.assertRaises(ProjectWorkspaceError) as ungated:
                with opened.open_member(member_path):
                    pass
            self.assertEqual(
                ungated.exception.code,
                "PROJECT.PACKAGE.CODEC_UNAVAILABLE",
            )

            missing = opened.codec_availability(())
            self.assertEqual(
                tuple(item.available for item in missing),
                (False,) * len(opened.workspace.documents),
            )
            self.assertEqual(
                tuple(item.safe_warnings for item in missing),
                (("PROJECT.PACKAGE.CODEC_UNAVAILABLE",),)
                * len(opened.workspace.documents),
            )
            available = opened.codec_availability(
                tuple(
                    dict.fromkeys(
                        document.codec_identity
                        for document in opened.workspace.documents
                    )
                )
            )
            self.assertTrue(all(item.available for item in available))
            self.assertTrue(all(item.safe_warnings == () for item in available))

    def test_public_dtos_and_errors_are_body_safe(self) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            bad = root / "secret-source-body.zip"
            bad.write_bytes(b"not a package\nsource text secret")
            with self.assertRaises(ProjectWorkspaceError) as failure:
                module.ProjectPackageService().validate(bad)
            self.assertEqual(str(failure.exception), failure.exception.code)
            self.assertNotIn(str(root), str(failure.exception))
            self.assertNotIn("secret", str(failure.exception))

    def test_replace_failure_preserves_last_known_good_package(self) -> None:
        module = _cluster2c()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "root"
            root.mkdir()
            _staged, workspace_service, save_service = _fixture(root)
            target = root / "project.localcat-project"
            package = module.ProjectPackageService()
            package.export_workspace(save_service, target)
            old = target.read_bytes()
            edited = replace(
                workspace_service.workspace,
                name="Edited project",
            )
            workspace_service._workspace = edited

            with mock.patch("project_package.os.replace", side_effect=OSError("secret")):
                result = package.export_workspace(save_service, target)

            self.assertNotEqual(result.save_report.journal_state, SaveJournalState.COMMITTED)
            self.assertEqual(target.read_bytes(), old)
            self.assertEqual(result.save_report.journal_state, SaveJournalState.ROLLED_BACK)
            self.assertFalse(result.save_report.recovery_required)


if __name__ == "__main__":
    unittest.main()
