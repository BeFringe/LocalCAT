from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import tm_benchmark_gate
from platform_fs_contracts import (
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateStorageProof,
)
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
            if os.name == "posix":
                self.assertEqual(state_root.stat().st_mode & 0o777, 0o700)
                self.assertEqual(
                    (state_root / "device.key").stat().st_mode & 0o777,
                    0o600,
                )
                self.assertEqual(
                    (state_root / "qualification.json").stat().st_mode & 0o777,
                    0o600,
                )
            else:
                payload = json.loads(
                    (state_root / "qualification.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    payload["schema_version"],
                    "localcat.gate-d-attestation.v2",
                )
                self.assertEqual(
                    payload["windows_private_proof"]["security_profile_id"],
                    "WindowsPrivateSecurityV2",
                )

    @unittest.skipUnless(sys.platform == "win32", "Windows W2 owner contract")
    def test_windows_second_persist_replaces_under_child_w1_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = (Path(temporary) / "gate-d").resolve()
            manifest = _base_capability_manifest()
            original = self._run_result()
            for _ in range(2):
                tm_benchmark_gate._persist_gate_d_attestation(
                    contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                    state_root=state_root,
                    base_manifest=manifest,
                    run_result=original,
                    issued_at_utc=EVALUATED_AT,
                )
            restored = tm_benchmark_gate._restore_gate_d_attestation(
                contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                state_root=state_root,
                base_manifest=manifest,
            )
            self.assertEqual(restored.bundle_digest, original.bundle_digest)

    @unittest.skipUnless(sys.platform == "win32", "Windows W2 owner contract")
    def test_windows_terminal_private_proof_reuses_exact_qualification_handle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = (Path(temporary) / "gate-d").resolve()
            manifest = _base_capability_manifest()
            observed: list[object] = []
            evidences: list[object] = []
            original = PrivateStorageProof.prove_private

            def observe(backend: object, authority: object):
                evidence = original(backend, authority)
                observed.append(authority)
                evidences.append(evidence)
                return evidence

            with patch.object(PrivateStorageProof, "prove_private", observe):
                tm_benchmark_gate._persist_gate_d_attestation(
                    contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                    state_root=state_root,
                    base_manifest=manifest,
                    run_result=self._run_result(),
                    issued_at_utc=EVALUATED_AT,
                )
            self.assertEqual(len(observed), 4)
            self.assertIs(observed[-2], observed[-1])
            self.assertTrue(all(evidence.closed for evidence in evidences))

            observed.clear()
            evidences.clear()
            with patch.object(PrivateStorageProof, "prove_private", observe):
                tm_benchmark_gate._restore_gate_d_attestation(
                    contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                    state_root=state_root,
                    base_manifest=manifest,
                )
            self.assertEqual(len(observed), 4)
            self.assertIs(observed[-2], observed[-1])
            self.assertTrue(all(evidence.closed for evidence in evidences))

    @unittest.skipUnless(sys.platform == "win32", "Windows owner dispatch")
    def test_windows_recovery_required_maps_to_revalidation(self) -> None:
        error = PlatformFileError(
            PlatformFileErrorCode.RECOVERY_REQUIRED,
            retryable=True,
        )
        common = {
            "contract_path": (ROOT / "benchmark_tm_contract.json").resolve(),
            "state_root": (ROOT / ".unused-gate-d-state").resolve(),
            "base_manifest": _base_capability_manifest(),
        }
        with patch.object(
            tm_benchmark_gate,
            "_persist_gate_d_attestation_windows",
            side_effect=error,
        ):
            with self.assertRaises(tm_benchmark_gate.BenchmarkGateDError) as ctx:
                tm_benchmark_gate._persist_gate_d_attestation(
                    **common,
                    run_result=self._run_result(),
                    issued_at_utc=EVALUATED_AT,
                )
        self.assertEqual(ctx.exception.error_code, "GATE_D.REVALIDATION_REQUIRED")

        with patch.object(
            tm_benchmark_gate,
            "_restore_gate_d_attestation_windows",
            side_effect=error,
        ):
            with self.assertRaises(tm_benchmark_gate.BenchmarkGateDError) as ctx:
                tm_benchmark_gate._restore_gate_d_attestation(**common)
        self.assertEqual(ctx.exception.error_code, "GATE_D.REVALIDATION_REQUIRED")

    @unittest.skipUnless(sys.platform == "win32", "Windows W2 owner contract")
    def test_windows_fresh_process_restores_v2_private_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = (Path(temporary) / "gate-d").resolve()

            def run_worker(mode: str) -> dict[str, object]:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tests.windows_tm_gate_d_attestation_worker",
                        mode,
                        str(state_root),
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(completed.stdout)

            persisted = run_worker("persist")
            restored = run_worker("restore")

            self.assertEqual(
                persisted["schema_version"],
                "localcat.gate-d-attestation.v2",
            )
            self.assertEqual(
                restored["security_profile_id"],
                "WindowsPrivateSecurityV2",
            )
            self.assertEqual(
                persisted["private_proof_fields"],
                restored["private_proof_fields"],
            )
            self.assertEqual(
                persisted["private_proof_digest"],
                restored["private_proof_digest"],
            )
            self.assertEqual(
                persisted["artifact_digest"],
                restored["restored_artifact_digest"],
            )
            self.assertEqual(
                persisted["bundle_digest"],
                restored["restored_bundle_digest"],
            )

    @unittest.skipUnless(sys.platform == "win32", "Windows W2 owner contract")
    def test_windows_v1_and_nested_private_proof_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = (Path(temporary) / "gate-d").resolve()
            manifest = _base_capability_manifest()
            kwargs = {
                "contract_path": (ROOT / "benchmark_tm_contract.json").resolve(),
                "state_root": state_root,
                "base_manifest": manifest,
            }
            tm_benchmark_gate._persist_gate_d_attestation(
                **kwargs,
                run_result=self._run_result(),
                issued_at_utc=EVALUATED_AT,
            )
            attestation = state_root / "qualification.json"
            key = (state_root / "device.key").read_bytes()
            payload = json.loads(attestation.read_text(encoding="utf-8"))

            legacy = dict(payload)
            legacy.pop("windows_private_proof")
            legacy["schema_version"] = "localcat.gate-d-attestation.v1"
            unsigned = dict(legacy)
            unsigned.pop("signature")
            legacy["signature"] = hmac.new(
                key,
                tm_benchmark_gate._gate_d_attestation_canonical_json(
                    unsigned
                ).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            attestation.write_text(
                tm_benchmark_gate._gate_d_attestation_canonical_json(legacy)
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(tm_benchmark_gate.BenchmarkGateDError) as ctx:
                tm_benchmark_gate._restore_gate_d_attestation(**kwargs)
            self.assertEqual(ctx.exception.error_code, "GATE_D.ATTESTATION_INVALID")

            tm_benchmark_gate._persist_gate_d_attestation(
                **kwargs,
                run_result=self._run_result(),
                issued_at_utc=EVALUATED_AT,
            )
            payload = json.loads(attestation.read_text(encoding="utf-8"))
            payload["windows_private_proof"]["device_secret_mac"] = "0" * 64
            unsigned = dict(payload)
            unsigned.pop("signature")
            payload["signature"] = hmac.new(
                key,
                tm_benchmark_gate._gate_d_attestation_canonical_json(
                    unsigned
                ).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            attestation.write_text(
                tm_benchmark_gate._gate_d_attestation_canonical_json(payload)
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(tm_benchmark_gate.BenchmarkGateDError) as ctx:
                tm_benchmark_gate._restore_gate_d_attestation(**kwargs)
            self.assertEqual(ctx.exception.error_code, "GATE_D.ATTESTATION_INVALID")

    @unittest.skipUnless(sys.platform == "win32", "Windows owner dispatch")
    def test_windows_programmer_error_is_not_mapped_to_operational_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            tm_benchmark_gate,
            "compose_platform_file_backend",
            side_effect=TypeError("programmer defect"),
        ):
            with self.assertRaisesRegex(TypeError, "programmer defect"):
                tm_benchmark_gate._restore_gate_d_attestation(
                    contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                    state_root=(Path(temporary) / "gate-d").resolve(),
                    base_manifest=_base_capability_manifest(),
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
