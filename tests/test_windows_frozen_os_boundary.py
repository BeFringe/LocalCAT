"""Keep host diagnostics separate from frozen application inputs."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.prepare_windows_frozen_entry_inputs import CandidateInputError, _load_entry_contract


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "packaging/windows/frozen-entry/candidate-contract.json"


class WindowsFrozenOsBoundaryTests(unittest.TestCase):
    def test_producer_import_does_not_require_os_diagnostic_collector(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; sys.modules['tools.windows_frozen_system_profile'] = None; "
             "from tools import prepare_windows_frozen_entry_inputs"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_contract_delegates_rng_to_os_without_provider_or_host_pin(self) -> None:
        contract, _ = _load_entry_contract(CONTRACT)
        self.assertEqual(contract["schema"], "localcat.windows-frozen-entry-target-contract.v3")
        loader = contract["dynamic_loader"]
        self.assertNotIn("system_provider_target", loader)
        self.assertEqual(loader["system_service_boundary"], {
            "authority": "Windows OS",
            "rng": {"dll": "bcrypt.dll", "symbol": "BCryptGenRandom",
                    "hAlgorithm": 0, "flags": 2},
        })
        self.assertEqual(loader["allowed_flags"], [
            "LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR", "LOAD_LIBRARY_SEARCH_SYSTEM32",
        ])

    def test_old_provider_gate_and_new_host_qualifications_are_rejected(self) -> None:
        original, _ = _load_entry_contract(CONTRACT)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contract.json"
            for key in ("system_provider_target", "system_provider_profile", "ubr",
                        "kernel_cache", "crypto_operators", "identity_provider"):
                changed = json.loads(json.dumps(original))
                changed["dynamic_loader"][key] = {"mandatory": True}
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.subTest(key=key), self.assertRaises(CandidateInputError):
                    _load_entry_contract(path)

    def test_system_service_cannot_be_widened_or_given_a_provider_pin(self) -> None:
        original, _ = _load_entry_contract(CONTRACT)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contract.json"
            for mutation in ("missing", "provider", "flags", "wildcard", "bool_handle"):
                changed = json.loads(json.dumps(original))
                loader = changed["dynamic_loader"]
                boundary = loader["system_service_boundary"]
                if mutation == "missing":
                    del loader["system_service_boundary"]
                elif mutation == "provider":
                    boundary["provider"] = "bcryptprimitives.dll"
                elif mutation == "flags":
                    boundary["rng"]["flags"] = 0
                elif mutation == "bool_handle":
                    boundary["rng"]["hAlgorithm"] = False
                else:
                    boundary["rng"]["dll"] = "*.dll"
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.subTest(mutation=mutation), self.assertRaises(CandidateInputError):
                    _load_entry_contract(path)

    def test_candidate_contains_no_os_snapshot_or_collector_authority(self) -> None:
        record = json.loads((CONTRACT.parent / "candidate-input.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], "localcat.windows-frozen-entry-candidate-input.v3")
        self.assertNotIn("system_profile_collector", record["evidence_producer"])
        self.assertNotIn("system_provider_profile", record["entry_contract"])
        self.assertNotIn("system_provider_target", record["entry_contract"]["dynamic_loader"])
        self.assertEqual(record["target_platform"], {"system": "Windows", "machine": "AMD64"})


if __name__ == "__main__":
    unittest.main()
