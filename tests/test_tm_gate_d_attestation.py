from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import tm_benchmark_gate
from tests.test_tm_benchmark_gate import (
    _base_capability_manifest,
    _combined_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


class GateDAttestationTests(unittest.TestCase):
    @staticmethod
    def _run_result():
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        artifact = tm_benchmark_gate.benchmark_evidence_bundle_to_json(
            bundle
        ).encode("utf-8")
        return tm_benchmark_gate._issue_benchmark_gate_d_run_result(
            bundle=bundle,
            bundle_digest=bundle.bundle_digest,
            artifact_size=len(artifact),
            artifact_digest=hashlib.sha256(artifact).hexdigest(),
            test_mode=False,
        )

    def test_real_receipt_round_trips_as_same_device_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "gate-d"
            original = self._run_result()
            manifest = _base_capability_manifest()

            tm_benchmark_gate._persist_gate_d_attestation(
                contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                state_root=state_root.resolve(),
                base_manifest=manifest,
                run_result=original,
                issued_at_utc=EVALUATED_AT,
            )
            restored = tm_benchmark_gate._restore_gate_d_attestation(
                contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                state_root=state_root.resolve(),
                base_manifest=manifest,
            )

            self.assertIsNot(restored, original)
            self.assertIsNot(restored._receipt, original._receipt)
            self.assertEqual(restored.bundle, original.bundle)
            self.assertEqual(restored.bundle_digest, original.bundle_digest)
            self.assertFalse(restored.test_mode)
            self.assertEqual(state_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (state_root / "device.key").stat().st_mode & 0o777,
                0o600,
            )
            self.assertEqual(
                (state_root / "qualification.json").stat().st_mode & 0o777,
                0o600,
            )

    def test_tamper_and_compatibility_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = (Path(temporary) / "gate-d").resolve()
            manifest = _base_capability_manifest()
            tm_benchmark_gate._persist_gate_d_attestation(
                contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                state_root=state_root,
                base_manifest=manifest,
                run_result=self._run_result(),
                issued_at_utc=EVALUATED_AT,
            )
            attestation = state_root / "qualification.json"
            payload = json.loads(attestation.read_text(encoding="utf-8"))
            payload["bundle_digest"] = "0" * 64
            attestation.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            os.chmod(attestation, 0o600)
            with self.assertRaises(tm_benchmark_gate.BenchmarkGateDError) as ctx:
                tm_benchmark_gate._restore_gate_d_attestation(
                    contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                    state_root=state_root,
                    base_manifest=manifest,
                )
            self.assertEqual(ctx.exception.error_code, "GATE_D.ATTESTATION_INVALID")

            tm_benchmark_gate._persist_gate_d_attestation(
                contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                state_root=state_root,
                base_manifest=manifest,
                run_result=self._run_result(),
                issued_at_utc=EVALUATED_AT,
            )
            with patch.object(
                tm_benchmark_gate,
                "benchmark_implementation_fingerprint",
                return_value="f" * 64,
            ):
                with self.assertRaises(tm_benchmark_gate.BenchmarkGateDError) as ctx:
                    tm_benchmark_gate._restore_gate_d_attestation(
                        contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                        state_root=state_root,
                        base_manifest=manifest,
                    )
            self.assertEqual(
                ctx.exception.error_code,
                "GATE_D.REVALIDATION_REQUIRED",
            )

    def test_missing_attestation_requires_manual_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(tm_benchmark_gate.BenchmarkGateDError) as ctx:
                tm_benchmark_gate._restore_gate_d_attestation(
                    contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                    state_root=(Path(temporary) / "missing").resolve(),
                    base_manifest=_base_capability_manifest(),
                )
            self.assertEqual(
                ctx.exception.error_code,
                "GATE_D.REVALIDATION_REQUIRED",
            )

    def test_gate_c_time_renewal_does_not_invalidate_device_qualification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = (Path(temporary) / "gate-d").resolve()
            manifest = _base_capability_manifest()
            tm_benchmark_gate._persist_gate_d_attestation(
                contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                state_root=state_root,
                base_manifest=manifest,
                run_result=self._run_result(),
                issued_at_utc=EVALUATED_AT,
            )
            renewed = replace(
                manifest,
                generated_at_utc="2031-01-01T00:00:00Z",
                valid_until_utc="2031-01-02T00:00:00Z",
                context_cohorts=tuple(
                    replace(
                        cohort,
                        generated_at_utc="2031-01-01T00:00:00Z",
                        valid_until_utc="2031-01-02T00:00:00Z",
                    )
                    for cohort in manifest.context_cohorts
                ),
                fuzzy_core_cohorts=tuple(
                    replace(
                        cohort,
                        generated_at_utc="2031-01-01T00:00:00Z",
                        valid_until_utc="2031-01-02T00:00:00Z",
                    )
                    for cohort in manifest.fuzzy_core_cohorts
                ),
            )

            restored = tm_benchmark_gate._restore_gate_d_attestation(
                contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                state_root=state_root,
                base_manifest=renewed,
            )

            self.assertEqual(restored.bundle, self._run_result().bundle)

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow contract")
    def test_symlink_attestation_is_rejected_without_following_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = (Path(temporary) / "gate-d").resolve()
            manifest = _base_capability_manifest()
            tm_benchmark_gate._persist_gate_d_attestation(
                contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                state_root=state_root,
                base_manifest=manifest,
                run_result=self._run_result(),
                issued_at_utc=EVALUATED_AT,
            )
            attestation = state_root / "qualification.json"
            target = Path(temporary) / "foreign.json"
            target.write_bytes(attestation.read_bytes())
            before = target.read_bytes()
            attestation.unlink()
            attestation.symlink_to(target)

            with self.assertRaises(tm_benchmark_gate.BenchmarkGateDError) as ctx:
                tm_benchmark_gate._restore_gate_d_attestation(
                    contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                    state_root=state_root,
                    base_manifest=manifest,
                )

            self.assertEqual(ctx.exception.error_code, "GATE_D.ATTESTATION_INVALID")
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
