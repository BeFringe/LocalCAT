"""Cross-restart canonical identity re-attestation remediation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from capability_host import CapabilityHost
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    TMResourceDisplayMode,
)
from editor_controller import EditorController
from editor_tm_adapter import EditorTMAdapter
from resource_repository import ResourceRepository
from tm_activation_journal import ActivationPreparationError
import tm_contracts as contract_module
from tm_application_composition import TMResourceResolver, TMRuntimeHost
from tm_contracts import CanonicalResourceIdentity, MigrationReport
from tm_engine import TMEngine
from tm_migration import TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator, _activation_journal_path


SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"same","target":"winner"}\n'
    b'{"source":"other","target":"value"}\n'
)


def _activate_source(
    source: Path,
    *,
    resource_id: str,
) -> tuple[CanonicalResourceIdentity, str]:
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        source,
    )
    canonical_store_id = f"store.{resource_id}"
    coordinator = ResourceStoreCoordinator(
        canonical_store_id=canonical_store_id,
        resource_identity=identity,
    )
    outcome = TMMigrationService(
        resource_identity=identity,
        canonical_store_id=canonical_store_id,
        coordinator=coordinator,
    ).activate_initial(source, identity.resource_id)
    if type(outcome) is not MigrationReport:
        raise AssertionError("canonical activation fixture failed")
    return identity, canonical_store_id


def _rewrite_persisted_devices(
    journal_path: Path,
    *,
    persisted_device: int,
    persisted_source_device: int | None = None,
) -> None:
    """Simulate one APFS reboot changing only the live ``st_dev`` value."""

    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    sealed = payload["sealed_content_attestation"]
    active = payload["active_content_attestation"]
    assert isinstance(sealed, dict)
    assert isinstance(active, dict)
    for attestation in (sealed, active):
        for field_name in ("database", "manifest", "source"):
            proof = attestation[field_name]
            assert isinstance(proof, dict)
            proof["device"] = persisted_device
    for field_name in (
        "candidate_stage_db_identity",
        "candidate_manifest_temp_identity",
        "source_jsonl_identity",
    ):
        identity = payload[field_name]
        assert isinstance(identity, list)
        identity[0] = persisted_device
    if persisted_source_device is not None:
        for attestation in (sealed, active):
            proof = attestation["source"]
            assert isinstance(proof, dict)
            proof["device"] = persisted_source_device
        source_identity = payload["source_jsonl_identity"]
        assert isinstance(source_identity, list)
        source_identity[0] = persisted_source_device

    sealed_without_digest = dict(sealed)
    sealed_without_digest.pop("attestation_digest")
    sealed["attestation_digest"] = contract_module._stable_digest(
        sealed_without_digest
    )
    active["sealed_attestation_digest"] = sealed["attestation_digest"]
    active_without_digest = dict(active)
    active_without_digest.pop("attestation_digest")
    active["attestation_digest"] = contract_module._stable_digest(
        active_without_digest
    )
    payload.pop("record_digest")
    payload["record_digest"] = contract_module._stable_digest(payload)
    journal_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )


class CanonicalIdentityReattestationTests(unittest.TestCase):
    def test_device_number_only_drift_requires_explicit_reproof_then_reopens(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = (Path(temporary) / "primary.jsonl").resolve()
            source.write_bytes(SOURCE_BYTES)
            identity, canonical_store_id = _activate_source(
                source,
                resource_id="tm.primary",
            )
            self.assertTrue(TMEngine(str(source)).canonical_active)

            journal_path = _activation_journal_path(identity)
            journal_before_drift = journal_path.read_bytes()
            current_device = source.stat().st_dev
            _rewrite_persisted_devices(
                journal_path,
                persisted_device=current_device + 17,
            )
            drifted_journal = journal_path.read_bytes()
            self.assertNotEqual(drifted_journal, journal_before_drift)
            with self.assertRaisesRegex(
                ValueError,
                "TM.CANONICAL_REATTESTATION_REQUIRED",
            ):
                TMEngine(str(source))

            recovered = ResourceStoreCoordinator(
                canonical_store_id=canonical_store_id,
                resource_identity=identity,
            )
            report = recovered.reattest_completed_authority()
            self.assertEqual(report.action, "COMPLETED")
            self.assertEqual(report.generation, 0)
            self.assertEqual(recovered.current_generation, 0)
            self.assertNotEqual(journal_path.read_bytes(), drifted_journal)
            self.assertTrue(TMEngine(str(source)).canonical_active)

    def test_completed_update_generation_rebinds_without_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = (Path(temporary) / "primary.jsonl").resolve()
            source.write_bytes(SOURCE_BYTES)
            identity, canonical_store_id = _activate_source(
                source,
                resource_id="tm.primary",
            )
            source.write_bytes(
                SOURCE_BYTES + b'{"source":"new","target":"value"}\n'
            )
            coordinator = ResourceStoreCoordinator(
                canonical_store_id=canonical_store_id,
                resource_identity=identity,
            )
            recovered = coordinator.rehydrate_runtime_authority()
            self.assertIsNotNone(recovered)
            service = TMMigrationService(
                resource_identity=identity,
                canonical_store_id=canonical_store_id,
                coordinator=coordinator,
            )
            rebuilt = service.rebuild_from_snapshot(
                source,
                identity.resource_id,
            )
            self.assertIs(type(rebuilt), MigrationReport)
            assert isinstance(rebuilt, MigrationReport)
            self.assertEqual(rebuilt.activated_generation, 1)

            journal = _activation_journal_path(identity)
            _rewrite_persisted_devices(
                journal,
                persisted_device=source.stat().st_dev + 17,
            )
            with self.assertRaisesRegex(
                ValueError,
                "TM.CANONICAL_REATTESTATION_REQUIRED",
            ):
                TMEngine(str(source))

            repair_owner = ResourceStoreCoordinator(
                canonical_store_id=canonical_store_id,
                resource_identity=identity,
            )
            report = repair_owner.reattest_completed_authority()
            self.assertEqual(report.action, "COMPLETED")
            self.assertEqual(report.generation, 1)
            self.assertEqual(repair_owner.current_generation, 1)
            reopened = TMEngine(str(source))
            self.assertTrue(reopened.canonical_active)
            assert reopened.canonical_store is not None
            self.assertEqual(reopened.canonical_store.health().generation, 1)

    def test_same_bytes_new_inode_is_not_repairable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = (Path(temporary) / "primary.jsonl").resolve()
            source.write_bytes(SOURCE_BYTES)
            identity, canonical_store_id = _activate_source(
                source,
                resource_id="tm.primary",
            )
            replacement = source.with_suffix(".replacement")
            replacement.write_bytes(source.read_bytes())
            replacement.replace(source)
            journal = _activation_journal_path(identity)
            before = journal.read_bytes()
            coordinator = ResourceStoreCoordinator(
                canonical_store_id=canonical_store_id,
                resource_identity=identity,
            )

            self.assertFalse(
                coordinator.completed_authority_requires_reattestation()
            )
            with self.assertRaises(ActivationPreparationError):
                coordinator.reattest_completed_authority()
            self.assertEqual(journal.read_bytes(), before)

    def test_mixed_persisted_devices_are_not_repairable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = (Path(temporary) / "primary.jsonl").resolve()
            source.write_bytes(SOURCE_BYTES)
            identity, canonical_store_id = _activate_source(
                source,
                resource_id="tm.primary",
            )
            journal = _activation_journal_path(identity)
            live_device = source.stat().st_dev
            _rewrite_persisted_devices(
                journal,
                persisted_device=live_device + 17,
                persisted_source_device=live_device + 23,
            )
            before = journal.read_bytes()
            coordinator = ResourceStoreCoordinator(
                canonical_store_id=canonical_store_id,
                resource_identity=identity,
            )

            self.assertFalse(
                coordinator.completed_authority_requires_reattestation()
            )
            with self.assertRaises(ActivationPreparationError):
                coordinator.reattest_completed_authority()
            self.assertEqual(journal.read_bytes(), before)

    def test_source_byte_drift_is_not_repairable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = (Path(temporary) / "primary.jsonl").resolve()
            source.write_bytes(SOURCE_BYTES)
            identity, canonical_store_id = _activate_source(
                source,
                resource_id="tm.primary",
            )
            source.write_bytes(SOURCE_BYTES + b'\n')
            journal = _activation_journal_path(identity)
            before = journal.read_bytes()
            coordinator = ResourceStoreCoordinator(
                canonical_store_id=canonical_store_id,
                resource_identity=identity,
            )

            self.assertFalse(
                coordinator.completed_authority_requires_reattestation()
            )
            with self.assertRaises(ActivationPreparationError):
                coordinator.reattest_completed_authority()
            self.assertEqual(journal.read_bytes(), before)

    def test_controller_repairs_one_resource_without_running_fuzzy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = ResourceRepository(root / "app-data")
            primary = repository.create_resource(
                "Primary canonical",
                ResourceKind.TRANSLATION_MEMORY,
            )
            primary.path.write_bytes(SOURCE_BYTES)
            peer = repository.create_resource(
                "Peer legacy",
                ResourceKind.TRANSLATION_MEMORY,
            )
            peer.path.write_text(
                '{"source":"same","target":"peer"}\n',
                encoding="utf-8",
            )
            identity, _canonical_store_id = _activate_source(
                primary.path,
                resource_id=primary.id,
            )
            _rewrite_persisted_devices(
                _activation_journal_path(identity),
                persisted_device=primary.path.stat().st_dev + 17,
            )
            runtime = TMRuntimeHost(
                resolver=TMResourceResolver(),
                configs=repository.list_resources(),
            )
            adapter = EditorTMAdapter(
                runtime_host=runtime,
                capability_host=CapabilityHost(
                    evaluated_at_utc=datetime(
                        2030,
                        1,
                        1,
                        tzinfo=timezone.utc,
                    )
                ),
            )
            controller = EditorController(repository, tm_adapter=adapter)
            controller.set_project(
                EditorProject(
                    name="canonical re-attestation",
                    segments=(
                        EditorSegment(id="segment-1", source="same"),
                    ),
                )
            )

            before_retrieval = controller.tm_retrieval_status()
            before_statuses = controller.tm_resource_statuses()
            primary_before = next(
                status
                for status in before_statuses
                if status.resource_id == primary.id
            )
            peer_before = next(
                status
                for status in before_statuses
                if status.resource_id == peer.id
            )
            self.assertEqual(
                primary_before.safe_codes,
                ("TM.RUNTIME.CANONICAL_REATTESTATION_REQUIRED",),
            )
            self.assertEqual(
                peer_before.mode,
                TMResourceDisplayMode.LEGACY_EXACT_ONLY,
            )

            started = controller.reattest_tm_resource(primary.id)
            completed = controller.wait_tm_activation(
                started.operation_id,
                timeout=20.0,
            )

            self.assertTrue(completed.succeeded)
            self.assertEqual(
                controller.tm_retrieval_status(),
                before_retrieval,
            )
            after_statuses = controller.tm_resource_statuses()
            primary_after = next(
                status
                for status in after_statuses
                if status.resource_id == primary.id
            )
            peer_after = next(
                status
                for status in after_statuses
                if status.resource_id == peer.id
            )
            self.assertEqual(
                primary_after.mode,
                TMResourceDisplayMode.CANONICAL_ACTIVE,
            )
            self.assertEqual(
                peer_after.mode,
                TMResourceDisplayMode.LEGACY_EXACT_ONLY,
            )
            health = runtime.capture_operation_snapshot().canonical_ports[
                0
            ].handle.store.health()
            self.assertEqual(health.generation, 0)


if __name__ == "__main__":
    unittest.main()
