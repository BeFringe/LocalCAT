"""ADR-027 direct TMX output-lock retirement and recovery tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from platform_fs import compose_platform_file_backend
from platform_fs_contracts import OutputLockFinishResult
from tmx_artifact_save import TmxDirectArtifactSaver
from tmx_context_contracts import (
    TmxContextError,
    TmxEffectiveLocales,
    TmxExportUnit,
    TmxScopeBinding,
    TmxScopeKind,
)
from tmx_context_interchange import ParserTmxColdValidator, prepare_tmx_payload


def _lock_path(destination: Path) -> Path:
    digest = hashlib.sha256(destination.name.encode("utf-8", "strict")).hexdigest()
    return destination.with_name(f".localcat-tmx-{digest[:32]}.lock")


def _binding(kind: TmxScopeKind) -> TmxScopeBinding:
    common = {
        "scope_kind": kind,
        "scope_id": kind.value,
        "binding_digest": hashlib.sha256(kind.value.encode("ascii")).hexdigest(),
        "unit_count": 1,
        "attached_count": 1,
    }
    if kind is TmxScopeKind.ENTIRE_PROJECT:
        common.update(project_id="project-1", document_count=1)
    elif kind is TmxScopeKind.SELECTED_CHUNK:
        common.update(
            project_id="project-1",
            chunk_plan_id="plan-1",
            chunk_plan_revision=1,
            chunk_id="chunk-1",
            document_count=1,
        )
    return TmxScopeBinding(**common)


def _payload(binding: TmxScopeBinding):
    return prepare_tmx_payload(
        binding,
        TmxEffectiveLocales("en-US", "zh-CN"),
        (TmxExportUnit("record-1", "source", "target", True),),
    )


@unittest.skipUnless(os.name == "nt", "ADR-027 output retirement is Windows-only")
class TmxOutputLockRetirementWindowsTests(unittest.TestCase):
    def test_all_direct_scopes_leave_only_final_tmx_and_keep_receipt_facts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            for kind in (
                TmxScopeKind.ENTIRE_PROJECT,
                TmxScopeKind.MANAGED_RESOURCE,
                TmxScopeKind.SELECTED_CHUNK,
            ):
                with self.subTest(kind=kind):
                    binding = _binding(kind)
                    payload = _payload(binding)
                    destination = root / f"{kind.value}.tmx"
                    saver = TmxDirectArtifactSaver(
                        ParserTmxColdValidator(),
                        lambda actual, expected=binding: self.assertEqual(actual, expected),
                    )
                    _preview, plan = saver.preview(binding, payload, destination)

                    receipt = saver.apply(plan)

                    self.assertTrue(receipt.durable)
                    self.assertEqual(receipt.after_digest, payload.proof.payload_digest)
                    self.assertEqual(destination.read_bytes(), payload.data)
                    self.assertFalse(_lock_path(destination).exists())
                    self.assertEqual(
                        {path.name for path in root.iterdir()},
                        {f"{item.value}.tmx" for item in (
                            TmxScopeKind.ENTIRE_PROJECT,
                            TmxScopeKind.MANAGED_RESOURCE,
                            TmxScopeKind.SELECTED_CHUNK,
                        ) if (root / f"{item.value}.tmx").exists()},
                    )

    def test_no_journal_recovery_retires_its_fresh_output_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            destination = root / "quiet.tmx"
            binding = _binding(TmxScopeKind.MANAGED_RESOURCE)
            saver = TmxDirectArtifactSaver(
                ParserTmxColdValidator(),
                lambda actual: self.assertEqual(actual, binding),
            )

            self.assertIsNone(saver.recover(destination))
            self.assertFalse(destination.exists())
            self.assertFalse(_lock_path(destination).exists())

    def test_cold_recovery_cleans_the_journal_then_retires_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            destination = root / "recovery.tmx"
            binding = _binding(TmxScopeKind.MANAGED_RESOURCE)
            payload = _payload(binding)

            def fail_after_publish(phase: str) -> None:
                if phase == "after_target_publish":
                    raise RuntimeError("injected after publication")

            interrupted = TmxDirectArtifactSaver(
                ParserTmxColdValidator(),
                lambda actual: self.assertEqual(actual, binding),
                fault_hook=fail_after_publish,
            )
            _preview, plan = interrupted.preview(binding, payload, destination)
            with self.assertRaises(TmxContextError) as caught:
                interrupted.apply(plan)
            self.assertEqual(caught.exception.code, "TMX.RECOVERY_REQUIRED")
            self.assertTrue(_lock_path(destination).exists())

            recovered = TmxDirectArtifactSaver(
                ParserTmxColdValidator(),
                lambda actual: self.assertEqual(actual, binding),
            ).recover(destination)

            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.after_digest, payload.proof.payload_digest)
            self.assertFalse(_lock_path(destination).exists())
            self.assertEqual({path.name for path in root.iterdir()}, {destination.name})

    def test_unclosed_journal_and_sidecar_keep_recovery_required_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            destination = root / "unclosed.tmx"
            binding = _binding(TmxScopeKind.MANAGED_RESOURCE)
            payload = _payload(binding)

            def fail_before_cleanup(phase: str) -> None:
                if phase == "terminal_reproof":
                    raise RuntimeError("injected before journal and sidecar cleanup")

            saver = TmxDirectArtifactSaver(
                ParserTmxColdValidator(),
                lambda actual: self.assertEqual(actual, binding),
                fault_hook=fail_before_cleanup,
            )
            _preview, plan = saver.preview(binding, payload, destination)

            with self.assertRaises(TmxContextError) as caught:
                saver.apply(plan)

            self.assertEqual(caught.exception.code, "TMX.RECOVERY_REQUIRED")
            self.assertTrue(_lock_path(destination).exists())
            self.assertEqual(len(tuple(root.glob(".localcat-tmx-*.journal"))), 1)
            self.assertEqual(len(tuple(root.glob(".localcat-tmx-*.stage.tmx"))), 1)
            self.assertTrue(destination.is_file())

    def test_retirement_not_proven_does_not_overturn_direct_success(self) -> None:
        faults = (
            {"return_value": OutputLockFinishResult.NOT_PROVEN},
            {"side_effect": RuntimeError("injected platform finish failure")},
        )
        for index, behavior in enumerate(faults):
            with self.subTest(behavior=tuple(behavior)), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                destination = root / f"not-proven-{index}.tmx"
                binding = _binding(TmxScopeKind.MANAGED_RESOURCE)
                payload = _payload(binding)
                backend = compose_platform_file_backend(root)
                saver = TmxDirectArtifactSaver(
                    ParserTmxColdValidator(),
                    lambda actual: self.assertEqual(actual, binding),
                    backend=backend,
                )
                _preview, plan = saver.preview(binding, payload, destination)

                with mock.patch.object(
                    type(backend),
                    "finish_output_lock",
                    **behavior,
                ) as finish:
                    receipt = saver.apply(plan)

                self.assertTrue(receipt.durable)
                self.assertEqual(finish.call_count, 1)
                self.assertTrue(_lock_path(destination).exists())


if __name__ == "__main__":
    unittest.main()
