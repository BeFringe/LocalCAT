"""ADR-027 ResourcePackage owner-finalize and Windows lock retirement tests."""

from __future__ import annotations

import hashlib
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from editor_contracts import ResourceConfig, ResourceKind
from editor_controller import _initial_tm_activation_service
from platform_fs_contracts import OutputLockFinishResult
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
from resource_artifact_save import ResourceArtifactSaveService
from resource_package_contracts import (
    PortableResourceKind,
    PortableResourceSnapshot,
    ResourcePayloadProfile,
    ResourcePortabilityError,
)
from resource_portability import ResourcePortabilityService
from resource_repository import ResourceRepository
from tests.benchmark_worker_test_support import worker_temporary_directory


_TERMS = b"\xef\xbb\xbfsource,target\nlocalcat-term-v1,id-1,Case,Target,true,false\n"
_TMX = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<tmx version="1.4"><body><tu><tuv xml:lang="en"><seg>a</seg></tuv>'
    b'<tuv xml:lang="zh-CN"><seg>b</seg></tuv></tu></body></tmx>\n'
)


class _TmxPayloadHandler:
    profile = ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1

    @staticmethod
    def _snapshot(baseline: str) -> PortableResourceSnapshot:
        return PortableResourceSnapshot(
            kind=PortableResourceKind.TRANSLATION_MEMORY,
            profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
            payload_digest=hashlib.sha256(_TMX).hexdigest(),
            payload_byte_count=len(_TMX),
            record_count=1,
            legacy_record_count=0,
            v1_record_count=0,
            source_baseline_digest=baseline,
            owner_receipt_digest=None,
            owner_generation=1,
            owner_revision=1,
        )

    def prepare_export_payload(
        self,
        resource: ResourceConfig,
    ) -> tuple[PortableResourceSnapshot, bytes]:
        baseline = hashlib.sha256(resource.path.read_bytes()).hexdigest()
        return self._snapshot(baseline), _TMX

    def validate_snapshot(self, source: Path) -> PortableResourceSnapshot:
        if source.read_bytes() != _TMX:
            raise ResourcePortabilityError("TMX.PAYLOAD.INVALID")
        return self._snapshot(hashlib.sha256(b"cold-validation").hexdigest())

    @staticmethod
    def reprove_snapshot(
        resource: ResourceConfig,
        snapshot: PortableResourceSnapshot,
    ) -> None:
        if hashlib.sha256(resource.path.read_bytes()).hexdigest() != (
            snapshot.source_baseline_digest
        ):
            raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE")


def _lock_path(destination: Path) -> Path:
    digest = hashlib.sha256(destination.name.encode("utf-8", "strict")).hexdigest()
    return destination.with_name(f".resource-artifact-{digest[:32]}.lock")


def _remove_activation_quarantine(root: Path) -> None:
    quarantine = root / ".localcat-activation-quarantine-v1"
    if not quarantine.exists():
        return
    for attempt in quarantine.iterdir():
        for path in attempt.iterdir():
            os.unlink("\\\\?\\" + str(path))
        os.rmdir("\\\\?\\" + str(attempt))
    os.rmdir("\\\\?\\" + str(quarantine))


@unittest.skipUnless(os.name == "nt", "ADR-027 output retirement is Windows-only")
class ResourceOutputLockRetirementWindowsTests(unittest.TestCase):
    def test_package_commits_the_ready_receipt_once_and_leaves_no_coordination_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            resource = repository.create_resource("Terms", ResourceKind.TERMBASE)
            resource.path.write_bytes(_TERMS)
            service = ResourcePortabilityService(repository)
            destination = root / "terms.localcat-resource"

            with mock.patch.object(
                service,
                "_commit_ready_receipt",
                wraps=service._commit_ready_receipt,
            ) as commit:
                outcome = service.export_package(resource.id, destination)

            self.assertEqual(commit.call_count, 1)
            self.assertEqual(service._ledger.list_receipts(), (outcome.receipt,))
            self.assertEqual(service._ledger.list_pending(), ())
            self.assertTrue(destination.is_file())
            self.assertFalse(_lock_path(destination).exists())
            self.assertEqual(
                service.validate_resource_package(destination).artifact_digest,
                outcome.receipt.package_artifact_digest,
            )

    def test_tm_jsonl_and_tmx_profiles_also_leave_no_coordination_lock(self) -> None:
        with ExitStack() as stack:
            raw = stack.enter_context(worker_temporary_directory())
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "jsonl-app")
            stack.callback(_remove_activation_quarantine, repository.managed_dir)
            resource = repository.create_resource("TM", ResourceKind.TRANSLATION_MEMORY)
            resource.path.write_text(
                '{"source":"Hello","target":"你好"}\n',
                encoding="utf-8",
            )
            _initial_tm_activation_service(resource).activate_initial(
                resource.path,
                resource.id,
            )
            jsonl_destination = root / "tm-jsonl.localcat-resource"
            jsonl_outcome = ResourcePortabilityService(repository).export_package(
                resource.id,
                jsonl_destination,
            )
            self.assertIs(
                jsonl_outcome.receipt.payload_profile,
                ResourcePayloadProfile.TM_JSONL_V1,
            )
            self.assertFalse(_lock_path(jsonl_destination).exists())

            tmx_repository = ResourceRepository(root / "tmx-app")
            tmx_resource = tmx_repository.create_resource(
                "TMX",
                ResourceKind.TRANSLATION_MEMORY,
            )
            tmx_resource.path.write_bytes(b"canonical-owner-bytes\n")
            tmx_destination = root / "tm-tmx.localcat-resource"
            tmx_outcome = ResourcePortabilityService(
                tmx_repository,
                tmx_payload_handler=_TmxPayloadHandler(),
            ).export_package(
                tmx_resource.id,
                tmx_destination,
                payload_profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
            )
            self.assertIs(
                tmx_outcome.receipt.payload_profile,
                ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
            )
            self.assertFalse(_lock_path(tmx_destination).exists())

    def test_owner_finalize_runs_once_while_destination_is_retained_then_retires_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            candidate = root / ".candidate"
            destination = root / "resource.localcat-resource"
            replacement = root / "replacement.localcat-resource"
            candidate.write_bytes(b"new")
            replacement.write_bytes(b"replacement")
            calls: list[str] = []

            def finalize() -> None:
                calls.append("finalize")
                with self.assertRaises(PermissionError):
                    replacement.replace(destination)

            publication, validation = ResourceArtifactSaveService().publish(
                candidate,
                destination,
                lambda path: path.read_bytes(),
                owner_commit=lambda _publication, _validation: calls.append("ready"),
                owner_finalize=finalize,
            )

            self.assertEqual(calls, ["ready", "finalize"])
            self.assertEqual(validation, b"new")
            self.assertEqual(publication.destination_after_digest, hashlib.sha256(b"new").hexdigest())
            self.assertFalse(_lock_path(destination).exists())
            replacement.replace(destination)

    def test_finalize_or_platform_retirement_fault_preserves_the_existing_outcome_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            candidate = root / ".candidate"
            destination = root / "fault.localcat-resource"
            candidate.write_bytes(b"new")
            calls = 0

            def fail_finalize() -> None:
                nonlocal calls
                calls += 1
                raise RuntimeError("owner finalize fault")

            with self.assertRaises(ResourcePortabilityError):
                ResourceArtifactSaveService().publish(
                    candidate,
                    destination,
                    lambda path: path.read_bytes(),
                    owner_commit=lambda _publication, _validation: None,
                    owner_finalize=fail_finalize,
                )
            self.assertEqual(calls, 1)
            self.assertTrue(_lock_path(destination).exists())

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            resource = repository.create_resource("Terms", ResourceKind.TERMBASE)
            resource.path.write_bytes(_TERMS)
            service = ResourcePortabilityService(repository)
            destination = root / "not-proven.localcat-resource"
            backend_type = type(repository.platform_backend)

            with mock.patch.object(
                backend_type,
                "finish_output_lock",
                return_value=OutputLockFinishResult.NOT_PROVEN,
            ) as finish:
                outcome = service.export_package(resource.id, destination)

            self.assertEqual(finish.call_count, 1)
            self.assertEqual(service._ledger.get(outcome.receipt.operation_id), outcome.receipt)
            self.assertTrue(destination.is_file())
            self.assertTrue(_lock_path(destination).exists())

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            resource = repository.create_resource("Terms", ResourceKind.TERMBASE)
            resource.path.write_bytes(_TERMS)
            service = ResourcePortabilityService(repository)
            destination = root / "retirement-exception.localcat-resource"

            with mock.patch.object(
                type(repository.platform_backend),
                "finish_output_lock",
                side_effect=RuntimeError("injected platform finish failure"),
            ) as finish:
                outcome = service.export_package(resource.id, destination)

            self.assertEqual(finish.call_count, 1)
            self.assertEqual(service._ledger.get(outcome.receipt.operation_id), outcome.receipt)
            self.assertTrue(destination.is_file())
            self.assertTrue(_lock_path(destination).exists())

    def test_retained_close_uncertainty_skips_retirement_without_changing_success(
        self,
    ) -> None:
        from platform_fs_windows import _WindowsBoundRegularFile

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            candidate = root / ".candidate"
            destination = root / "close-uncertain.localcat-resource"
            candidate.write_bytes(b"new")
            finalized = False
            real_close = _WindowsBoundRegularFile._close_authority

            def finalize() -> None:
                nonlocal finalized
                finalized = True

            def uncertain_close(bound) -> None:
                real_close(bound)
                if finalized and bound._entry_path.endswith(destination.name):
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )

            with mock.patch.object(
                _WindowsBoundRegularFile,
                "_close_authority",
                new=uncertain_close,
            ):
                publication, validation = ResourceArtifactSaveService().publish(
                    candidate,
                    destination,
                    lambda path: path.read_bytes(),
                    owner_commit=lambda _publication, _validation: None,
                    owner_finalize=finalize,
                )

            self.assertEqual(validation, b"new")
            self.assertEqual(
                publication.destination_after_digest,
                hashlib.sha256(b"new").hexdigest(),
            )
            self.assertTrue(_lock_path(destination).exists())

    def test_direct_csv_keeps_ordinary_persistent_lock_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            resource = repository.create_resource("Terms", ResourceKind.TERMBASE)
            resource.path.write_bytes(_TERMS)
            service = ResourcePortabilityService(repository)
            destination = root / "terms.csv"

            outcome = service.export_direct(resource.id, destination)

            self.assertEqual(outcome.receipt.durable_state.value, "committed")
            self.assertEqual(destination.read_bytes(), _TERMS)
            self.assertTrue(_lock_path(destination).exists())

    def test_direct_jsonl_does_not_request_output_lock_retirement(self) -> None:
        with ExitStack() as stack:
            raw = stack.enter_context(worker_temporary_directory())
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            stack.callback(_remove_activation_quarantine, repository.managed_dir)
            resource = repository.create_resource("TM", ResourceKind.TRANSLATION_MEMORY)
            resource.path.write_text(
                '{"source":"Hello","target":"你好"}\n',
                encoding="utf-8",
            )
            _initial_tm_activation_service(resource).activate_initial(
                resource.path,
                resource.id,
            )
            service = ResourcePortabilityService(repository)
            destination = root / "tm.jsonl"

            with mock.patch.object(
                type(repository.platform_backend),
                "finish_output_lock",
            ) as finish:
                outcome = service.export_direct(resource.id, destination)

            finish.assert_not_called()
            self.assertIs(
                outcome.receipt.payload_profile,
                ResourcePayloadProfile.TM_JSONL_V1,
            )
            self.assertTrue(destination.is_file())


if __name__ == "__main__":
    unittest.main()
