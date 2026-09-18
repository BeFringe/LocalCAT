"""Diagnostic-only CNG replay; never an entry gate or provider authorization.

Pure tests: audit Python -B tests/test_windows_cng_selection.py
Native replay: same command --probe ABSOLUTE_PATH_TO_PROBE_EXE
Build the C source with pinned MSVC 14.44.35207, x64 SDK 10.0.26100.0.
The runner does not compile, change registry, load a provider, or call RNG.
"""
import argparse
import copy
import json
import re
from pathlib import Path
import subprocess
import sys
import unittest


SCHEMA = "localcat.cng-selection-diagnostic.v3"
LIMITATIONS = ["no_atomic_binding", "candidate_bound_mode_query", "partial_mode_interpretation", "no_preload_authority"]
PROBE = None


def _require(condition, message="invalid diagnostic shape"):
    if not condition:
        raise ValueError(message)


def _keys(obj, names):
    _require(type(obj) is dict and set(obj) == set(names.split()))


def _uint(value):
    _require(type(value) is int and 0 <= value <= 0xFFFFFFFF)


def _string(value):
    _require(type(value) is str and 0 < len(value) <= 32768 and "\x00" not in value)


def _array(value):
    _require(type(value) is list and len(value) <= 512)


def _hex(value, size=None):
    _require(type(value) is str and len(value) <= 131072 and len(value) % 2 == 0
             and re.fullmatch("[0-9a-f]*", value) is not None)
    if size is not None:
        _require(len(value) == size * 2)


def _mode(obj):
    _keys(obj, "query86_status query86_buffer_hex query0_status query0_buffer_hex derived_mode")
    _uint(obj["query86_status"]); _hex(obj["query86_buffer_hex"], 176)
    expected = None
    if obj["query86_status"] == 0xC0000003:
        _uint(obj["query0_status"]); _hex(obj["query0_buffer_hex"], 64)
        if obj["query0_status"] == 0:
            flags = int.from_bytes(bytes.fromhex(obj["query0_buffer_hex"])[56:60], "little")
            expected = 4 if flags & 128 else 1
    else:
        _require(obj["query0_status"] is None and obj["query0_buffer_hex"] is None)
    _require(obj["derived_mode"] is None if expected is None else
             type(obj["derived_mode"]) is int and obj["derived_mode"] == expected)


def _image(obj, registered=False):
    if obj is None:
        return
    _keys(obj, "image interfaces" if registered else "image flags")
    _string(obj["image"])
    if not registered:
        _uint(obj["flags"])
        return
    _array(obj["interfaces"])
    for interface in obj["interfaces"]:
        _keys(interface, "interface flags functions")
        _uint(interface["interface"]); _uint(interface["flags"])
        _array(interface["functions"])
        for function in interface["functions"]:
            _string(function)


def _provider(provider, registered=False):
    _keys(provider, "name interface function properties um km" + (" registration" if registered else ""))
    _string(provider["name"]); _string(provider["function"]); _uint(provider["interface"])
    _array(provider["properties"])
    for prop in provider["properties"]:
        _keys(prop, "name value_hex"); _string(prop["name"]); _hex(prop["value_hex"])
    _image(provider["um"]); _image(provider["km"])
    if registered:
        reg = provider["registration"]
        _keys(reg, "status aliases um km"); _uint(reg["status"]); _array(reg["aliases"])
        for alias in reg["aliases"]:
            _string(alias)
        _image(reg["um"], True); _image(reg["km"], True)
        _require(reg["status"] == 0 or (reg["aliases"] == [] and reg["um"] is None and reg["km"] is None))


def parse_diagnostic(raw):
    """Validate an observation, NOT the provider's safety or an authorization."""
    _require(type(raw) is str and len(raw) <= 4 * 1024 * 1024)
    def pairs(items):
        obj = {}
        for key, value in items:
            _require(key not in obj, "duplicate JSON key")
            obj[key] = value
        return obj
    def invalid_constant(value):
        raise ValueError("nonfinite JSON number: " + value)
    obj = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    _keys(obj, "schema diagnostic_only limitations bcrypt_path selector request mode_before mode_after provider_loaded resolve_status providers")
    _require(obj["schema"] == SCHEMA and obj["diagnostic_only"] is True and obj["limitations"] == LIMITATIONS)
    _string(obj["bcrypt_path"])
    selector = obj["selector"]
    _keys(selector, "request status providers selected_function")
    _keys(selector["request"], "context interface function provider mode flags")
    for field in ("interface", "mode", "flags"):
        _uint(selector["request"][field])
    _require(selector["request"] == {"context": None, "interface": 6, "function": None,
                                      "provider": None, "mode": 1, "flags": 0})
    _uint(selector["status"]); _array(selector["providers"])
    _require(selector["status"] == 0 or not selector["providers"])
    for provider in selector["providers"]:
        _provider(provider)
    selected = selector["providers"][0]["function"] if selector["providers"] else None
    _require(selector["selected_function"] == selected)
    request = obj["request"]
    if selected is None:
        _require(request is None and obj["resolve_status"] is None and obj["providers"] == [])
    else:
        _keys(request, "context interface function provider mode flags")
        for field in ("interface", "mode", "flags"):
            _uint(request[field])
        _require(request == {"context": None, "interface": 6, "function": selected,
                            "provider": None, "mode": 1, "flags": 2})
        _uint(obj["resolve_status"])
    _mode(obj["mode_before"]); _mode(obj["mode_after"])
    _keys(obj["provider_loaded"], "before_bcrypt before_queries after_selector after_resolve after_registration")
    _require(all(type(value) is bool for value in obj["provider_loaded"].values()))
    _array(obj["providers"])
    _require(obj["resolve_status"] == 0 or not obj["providers"])
    names = set()
    for provider in obj["providers"]:
        _provider(provider, True)
        _require(provider["name"].casefold() not in names, "case-duplicate provider")
        names.add(provider["name"].casefold())
    return obj


def diagnostic_differences(observed, baseline):
    """Fixture comparison only. Even [] has no security-gate meaning."""
    differences = []
    before, after = observed["mode_before"]["derived_mode"], observed["mode_after"]["derived_mode"]
    if before != after:
        differences.append("mode_changed_during_queries")
    if before is None or after is None:
        differences.append("mode_uninterpreted")
    if before != baseline["mode_before"]["derived_mode"] or after != baseline["mode_after"]["derived_mode"]:
        differences.append("mode_changed_from_fixture")
    if observed["providers"] != baseline["providers"]:
        differences.append("providers_changed")
    if observed["selector"] != baseline["selector"]:
        differences.append("selector_changed")
    if observed["selector"]["status"] != 0:
        differences.append("selector_query_failed")
    elif not observed["selector"]["providers"]:
        differences.append("selector_empty")
    if observed["selector"]["selected_function"] != baseline["selector"]["selected_function"]:
        differences.append("selected_function_changed")
    if observed["resolve_status"] not in (0, None):
        differences.append("resolve_query_failed")
    if any(observed["provider_loaded"].values()):
        differences.append("provider_module_seen")
    return differences


def fixture():
    mode = {"query86_status": 0xC0000003, "query86_buffer_hex": "00" * 176,
            "query0_status": 0, "query0_buffer_hex": "40" + "00" * 63,
            "derived_mode": 1}
    result = {"schema": SCHEMA, "diagnostic_only": True, "limitations": LIMITATIONS[:],
            "bcrypt_path": "C:\\Windows\\System32\\bcrypt.dll",
            "request": {"context": None, "interface": 6, "function": "RNG",
                        "provider": None, "mode": 1, "flags": 2},
            "mode_before": mode, "mode_after": copy.deepcopy(mode),
            "provider_loaded": {"before_bcrypt": False, "before_queries": False,
                                "after_selector": False, "after_resolve": False, "after_registration": False},
            "resolve_status": 0,
            "providers": [{"name": "Microsoft Primitive Provider", "interface": 6,
                           "function": "RNG", "properties": [],
                           "um": {"image": "bcryptprimitives.dll", "flags": 1}, "km": None,
                           "registration": {"status": 0, "aliases": [], "km": None,
                                            "um": {"image": "bcryptprimitives.dll",
                                                   "interfaces": [{"interface": 6, "flags": 1,
                                                                   "functions": ["RNG"]}]}}}]}
    first = copy.deepcopy(result["providers"][0]); del first["registration"]
    result["selector"] = {"request": {"context": None, "interface": 6, "function": None,
                                      "provider": None, "mode": 1, "flags": 0},
                          "status": 0, "providers": [first], "selected_function": "RNG"}
    return result


class DiagnosticTests(unittest.TestCase):
    def parse(self, obj):
        return parse_diagnostic(json.dumps(obj))

    def test_selector_is_exact_cold_request(self):
        obj = fixture()
        self.assertEqual(obj["schema"], "localcat.cng-selection-diagnostic.v3")
        self.assertEqual(obj["selector"]["request"],
                         {"context": None, "interface": 6, "function": None,
                          "provider": None, "mode": 1, "flags": 0})
        self.assertEqual(obj["selector"]["selected_function"], "RNG")

    def test_changed_algorithm_queries_selected_function_not_fixed_rng(self):
        obj = fixture()
        obj["selector"]["providers"][0]["function"] = "OTHER-RNG"
        obj["selector"]["selected_function"] = "OTHER-RNG"
        obj["request"]["function"] = "OTHER-RNG"
        obj["providers"][0]["function"] = "OTHER-RNG"
        self.assertIn("selected_function_changed", diagnostic_differences(self.parse(obj), fixture()))
        obj["request"]["function"] = "RNG"
        with self.assertRaises(ValueError): self.parse(obj)

    def test_failed_or_empty_selector_never_falls_back_to_rng(self):
        for status in (0, 0xC0000001):
            obj = fixture()
            obj["selector"].update(status=status, providers=[], selected_function=None)
            obj.update(request=None, resolve_status=None, providers=[])
            parsed = self.parse(obj)
            self.assertIn("selector_empty" if status == 0 else "selector_query_failed",
                          diagnostic_differences(parsed, fixture()))
            obj["request"] = fixture()["request"]
            with self.assertRaises(ValueError): self.parse(obj)

    def test_selector_drift_and_inconsistent_selection_are_distinct(self):
        obj = fixture()
        obj["selector"]["providers"][0]["name"] = "Another provider"
        self.assertIn("selector_changed", diagnostic_differences(self.parse(obj), fixture()))
        obj["selector"]["selected_function"] = "different"
        with self.assertRaises(ValueError): self.parse(obj)
        obj = fixture(); obj["selector"]["status"] = 0xC0000001
        with self.assertRaises(ValueError): self.parse(obj)
        obj = fixture(); obj["selector"]["request"]["flags"] = False
        with self.assertRaises(ValueError): self.parse(obj)

    def test_fixture_roundtrip_never_authorizes(self):
        observation = self.parse(fixture())
        self.assertEqual(diagnostic_differences(observation, fixture()), [])
        self.assertTrue(observation["diagnostic_only"])

    def test_unknown_schema_extra_field_and_duplicate_keys_rejected(self):
        for key, value in [("schema", "unknown"), ("schema", "localcat.cng-selection-diagnostic.v2"),
                           ("PASS", True), ("diagnostic_only", False)]:
            obj = fixture(); obj[key] = value
            with self.assertRaises(ValueError): self.parse(obj)
        with self.assertRaises(ValueError): parse_diagnostic('{"schema":1,"schema":2}')

    def test_nested_types_limits_and_extra_keys_rejected(self):
        cases = []
        obj = fixture(); obj["providers"][0]["um"]["flags"] = True; cases.append(obj)
        obj = fixture(); obj["mode_before"]["query86_buffer_hex"] = "00"; cases.append(obj)
        obj = fixture(); obj["request"]["function"] = "AES"; cases.append(obj)
        obj = fixture(); obj["providers"][0]["registration"]["authorized"] = True; cases.append(obj)
        obj = fixture(); obj["mode_after"]["derived_mode"] = 4; cases.append(obj)
        for obj in cases:
            with self.assertRaises(ValueError): self.parse(obj)

    def test_extra_provider_is_drift_not_auto_allowlist(self):
        obj = fixture(); extra = copy.deepcopy(obj["providers"][0]); extra["name"] = "Other"
        extra["um"]["image"] = "other.dll"; obj["providers"].append(extra)
        parsed = self.parse(obj)
        self.assertIn("providers_changed", diagnostic_differences(parsed, fixture()))

    def test_mode_change_is_observed_not_authorized(self):
        obj = fixture(); data = bytearray.fromhex(obj["mode_after"]["query0_buffer_hex"])
        data[56] = 128; obj["mode_after"]["query0_buffer_hex"] = data.hex()
        obj["mode_after"]["derived_mode"] = 4
        parsed = self.parse(obj)
        self.assertIn("mode_changed_during_queries", diagnostic_differences(parsed, fixture()))

    def test_loaded_provider_does_not_establish_preauthority(self):
        obj = fixture(); obj["provider_loaded"]["before_queries"] = True
        self.assertIn("provider_module_seen", diagnostic_differences(self.parse(obj), fixture()))

    def test_case_duplicate_provider_rejected(self):
        obj = fixture(); duplicate = copy.deepcopy(obj["providers"][0]); duplicate["name"] = duplicate["name"].upper()
        obj["providers"].append(duplicate)
        with self.assertRaises(ValueError): self.parse(obj)

    def test_unrecognized_mode_response_is_not_interpreted(self):
        # Even another failure remains raw: this is not a complete DLL emulator.
        for status in (0, 0xC0000022):
            obj = fixture()
            for name in ("mode_before", "mode_after"):
                obj[name].update(query86_status=status, query0_status=None,
                                 query0_buffer_hex=None, derived_mode=None)
            self.assertIn("mode_uninterpreted", diagnostic_differences(self.parse(obj), fixture()))
            obj["mode_before"]["derived_mode"] = 1
            with self.assertRaises(ValueError): self.parse(obj)

    def test_query_failure_is_observation_not_empty_success(self):
        obj = fixture(); obj["resolve_status"] = 0xC0000001; obj["providers"] = []
        self.assertIn("resolve_query_failed", diagnostic_differences(self.parse(obj), fixture()))
        obj["providers"] = fixture()["providers"]
        with self.assertRaises(ValueError): self.parse(obj)

    def test_nonfinite_and_oversize_records_rejected(self):
        with self.assertRaises(ValueError): parse_diagnostic('{"number":NaN}')
        obj = fixture(); obj["providers"] *= 513
        with self.assertRaises(ValueError): self.parse(obj)

    def test_provider_image_and_flags_are_exact_observations(self):
        for field, value in (("image", "other.dll"), ("flags", 0)):
            obj = fixture(); obj["providers"][0]["um"][field] = value
            self.assertIn("providers_changed", diagnostic_differences(self.parse(obj), fixture()))

    def test_native_source_is_diagnostic_only(self):
        source = (Path(__file__).resolve().parents[1] / "tools/probe_windows_cng_selection.c").read_text()
        self.assertNotIn('GetProcAddress(bcrypt, "BCryptGenRandom")', source)
        self.assertNotIn('GetProcAddress(bcrypt, "BCryptOpenAlgorithmProvider")', source)
        self.assertNotIn('L"RNG"', source)  # No hidden fixed-function fallback.
        self.assertIn('LOAD_LIBRARY_SEARCH_SYSTEM32', source)

    def test_native_mode_query_uses_evaluated_class_not_instruction_displacement(self):
        source = (Path(__file__).resolve().parents[1] / "tools/probe_windows_cng_selection.c").read_text()
        # Actual instruction: r9=0xb0; lea edx,[r9-0x5a] -> class 0x56.
        query_class = re.search(r'query\(GetCurrentProcess\(\), (0x[0-9a-f]+),', source)
        self.assertIsNotNone(query_class)
        self.assertEqual(query_class.group(1), '0x56')

    def test_native_probe(self):
        if PROBE is None:
            self.skipTest("use --probe for explicit native replay")
        result = subprocess.run([str(PROBE)], capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = parse_diagnostic(result.stdout)
        self.assertEqual(observed["schema"], "localcat.cng-selection-diagnostic.v3")
        self.assertIn("selector", observed)
        print(json.dumps({"diagnostic_differences_from_fixture": diagnostic_differences(observed, fixture()),
                          "diagnostic_only": True}, sort_keys=True))


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--probe", type=Path)
    options, remaining = args.parse_known_args()
    if options.probe:
        if not options.probe.is_absolute() or not options.probe.is_file():
            args.error("--probe requires an existing absolute executable path")
        PROBE = options.probe
    unittest.main(argv=[sys.argv[0], *remaining])
