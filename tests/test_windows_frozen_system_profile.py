from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import unittest
from unittest import mock

from tools.windows_frozen_system_profile import (
    ProfileError, canonical_digest, collect_profile, parse_json, validate_profile, validate_target,
)


def target():
    return {
        "schema": "localcat.windows-system-provider-target.v1",
        "phase": "pre-E10",
        "trigger": {"dll": "bcrypt.dll", "symbol": "BCryptGenRandom", "hAlgorithm": 0, "flags": 2},
        "members": [
            {"role": "api_host", "basename": "bcrypt.dll"},
            {"role": "provider", "basename": "bcryptprimitives.dll"},
        ],
    }


def profile():
    return {
        "schema": "localcat.windows-system-provider-profile.v1",
        "scope": "explicit-system-roots",
        "target_sha256": canonical_digest(target()),
        "windows": {"major": 10, "minor": 0, "build": 26200, "ubr": 9168, "architecture": "AMD64"},
        "members": [
            {**item, "size": 100, "sha256": "a" * 64, "file_version": "10.0.26100.9168",
             "pe": {"machine": 0x8664, "static_imports": [{"dll": "ntdll.dll", "symbols": ["RtlInitUnicodeString"]}],
                    "delay_imports": [], "exports": [{"ordinal": 1, "name": "BCryptGenRandom" if item["role"] == "api_host" else "ProcessPrng", "rva": 4096, "forwarder": None}]}}
            for item in target()["members"]
        ],
    }


class SystemProviderProfileTests(unittest.TestCase):
    def test_valid_explicit_roots_and_replay(self):
        validate_target(target())
        validate_profile(profile(), target(), expected=copy.deepcopy(profile()))

    def test_target_rejects_scope_expansion(self):
        variants = []
        for key, value in [("phase", "post-E10"), ("schema", "future"), ("extra", True)]:
            item = target(); item[key] = value; variants.append(item)
        for key, value in [("flags", 0), ("hAlgorithm", False), ("symbol", "BCryptOpenAlgorithmProvider")]:
            item = target(); item["trigger"][key] = value; variants.append(item)
        item = target(); item["members"].append({"role": "provider", "basename": "BCRYPTPRIMITIVES.DLL"}); variants.append(item)
        item = target(); item["members"][1]["basename"] = "other.dll"; variants.append(item)
        item = target(); item["members"][1]["basename"] = "../bcryptprimitives.dll"; variants.append(item)
        for item in variants:
            with self.subTest(target=item), self.assertRaises(ProfileError):
                validate_target(item)

    def test_profile_rejects_fabricated_scope_and_shape(self):
        variants = []
        for key, value in [("schema", "future"), ("scope", "complete-closure"), ("target_sha256", "b" * 64), ("pass", True)]:
            item = profile(); item[key] = value; variants.append(item)
        item = profile(); item["members"].append(copy.deepcopy(item["members"][1])); variants.append(item)
        item = profile(); item["members"][1]["basename"] = "other.dll"; variants.append(item)
        item = profile(); item["windows"]["build"] = True; variants.append(item)
        item = profile(); item["members"][0]["pe"]["machine"] = 0x14c; variants.append(item)
        item = profile(); item["members"][0]["sha256"] = "a"; variants.append(item)
        item = profile(); item["members"][0]["pe"]["exports"] = []; variants.append(item)
        item = profile(); item["members"][0]["pe"]["static_imports"].append({"dll": "NTDLL.DLL", "symbols": ["Other"]}); variants.append(item)
        for item in variants:
            with self.subTest(profile=item), self.assertRaises(ProfileError):
                validate_profile(item, target())

    def test_replay_rejects_self_consistent_byte_or_import_drift(self):
        for change in ("bytes", "imports", "build"):
            item = profile()
            if change == "bytes": item["members"][1]["sha256"] = "b" * 64
            if change == "imports": item["members"][1]["pe"]["delay_imports"] = [{"dll": "other.dll", "symbols": ["Export"]}]
            if change == "build": item["windows"]["ubr"] += 1
            with self.subTest(change=change), self.assertRaisesRegex(ProfileError, "replay"):
                validate_profile(item, target(), expected=profile())

    def test_json_rejects_duplicate_keys_and_nonfinite(self):
        for raw in ('{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}', '{"x":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ProfileError):
                parse_json(raw)

    def test_collector_reads_only_approved_roots_and_hashes_parsed_bytes(self):
        baseline = profile()
        root = Path("/os/System32")
        observed = []

        def read(path):
            observed.append(path)
            return path.name.encode("ascii")

        def parse(data):
            index = 0 if data == b"bcrypt.dll" else 1
            return baseline["members"][index]["file_version"], baseline["members"][index]["pe"]

        with mock.patch("tools.windows_frozen_system_profile._windows_context", return_value=(baseline["windows"], root)), \
             mock.patch.object(Path, "read_bytes", read), \
             mock.patch("tools.windows_frozen_system_profile._pe_projection", side_effect=parse):
            result = collect_profile(target())
        self.assertEqual(observed, [root / item["basename"] for item in target()["members"]])
        for item in result["members"]:
            self.assertEqual(item["sha256"], hashlib.sha256(item["basename"].encode("ascii")).hexdigest())
        validate_profile(result, target())

    def test_target_rejected_before_any_host_inspection(self):
        item = target(); item["members"][1]["basename"] = "unexpected.dll"
        with mock.patch("tools.windows_frozen_system_profile._windows_context") as host:
            with self.assertRaises(ProfileError):
                collect_profile(item)
            host.assert_not_called()


if __name__ == "__main__":
    unittest.main()
