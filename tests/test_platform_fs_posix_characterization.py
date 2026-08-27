from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest


ROOT = Path(__file__).parents[1]
MATRIX_PATH = (
    ROOT
    / ".kiro"
    / "specs"
    / "windows-platform-enablement"
    / "posix-characterization-matrix.json"
)
RUNNER_PATH = ROOT / "tools" / "run_posix_characterization.py"

EXPECTED_OWNERS = {"parser", "chunk", "project", "resource", "tmx", "tm"}
EXPECTED_KINDS = {"success", "error", "receipt", "recovery", "bytes"}
REQUIRED_PRIMITIVES = {
    "openat",
    "dir_fd",
    "O_NOFOLLOW",
    "flock",
    "file_fsync",
    "directory_fsync",
    "candidate",
    "readback",
    "LKG",
}
HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _git(*arguments: str) -> bytes:
    return subprocess.run(
        ("git", *arguments),
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _baseline_bytes(commit: str, relative_path: str) -> bytes:
    return _git("show", f"{commit}:{relative_path}")


def _oracle_location(test_id: str) -> tuple[str, str, str]:
    parts = test_id.split(".")
    if len(parts) < 4 or parts[0] != "tests":
        raise AssertionError(f"invalid oracle test id: {test_id}")
    module = "/".join(parts[:-2]) + ".py"
    return module, parts[-2], parts[-1]


def _method_node(payload: bytes, class_name: str, method_name: str) -> ast.AST:
    tree = ast.parse(payload)
    matching_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if len(matching_classes) != 1:
        raise AssertionError(f"missing or ambiguous oracle class: {class_name}")
    matching_methods = [
        node
        for node in matching_classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    ]
    if len(matching_methods) != 1:
        raise AssertionError(f"missing or ambiguous oracle method: {class_name}.{method_name}")
    return matching_methods[0]


def _normalized_method_bytes(payload: bytes, class_name: str, method_name: str) -> bytes:
    node = _method_node(payload, class_name, method_name)
    text = payload.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    return "".join(lines[node.lineno - 1 : node.end_lineno]).encode("utf-8")


class PosixCharacterizationMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))

    def test_matrix_has_one_owner_partition_and_explicit_host_gate(self) -> None:
        self.assertEqual(self.matrix["schema_version"], 1)
        self.assertEqual(
            self.matrix["execution_policy"],
            {
                "posix_runtime_required": True,
                "windows_static_validation_only": True,
                "mandatory_skip_is_pass": False,
                "oracle_rule": (
                    "每项业务不变量由 baseline commit 中的精确 unittest ID 定义；"
                    "direct boundary 只描述该历史 tree 的实现，不得提升为跨平台业务合同。"
                ),
            },
        )
        owners = self.matrix["owners"]
        self.assertEqual({entry["owner"] for entry in owners}, EXPECTED_OWNERS)
        self.assertEqual(len(owners), len(EXPECTED_OWNERS))

        invariant_ids: set[str] = set()
        observed_kinds: set[str] = set()
        observed_primitives: set[str] = set()
        for owner in owners:
            with self.subTest(owner=owner["owner"]):
                support = owner["support"]
                self.assertEqual(support["runtime_hosts"], ["darwin", "linux"])
                self.assertEqual(support["runtime_gate"], "os.name == 'posix'")
                self.assertEqual(support["windows_disposition"], "NOT_EXECUTED")
                self.assertIs(support["skip_counts_as_pass"], False)

                invariants = owner["business_invariants"]
                owner_kinds = {item["kind"] for item in invariants}
                self.assertTrue({"success", "error", "bytes"}.issubset(owner_kinds))
                observed_kinds.update(owner_kinds)
                missing_kinds = EXPECTED_KINDS - owner_kinds
                self.assertEqual(
                    set(owner["not_applicable"]),
                    missing_kinds,
                    owner["owner"],
                )
                self.assertTrue(all(owner["not_applicable"].values()))
                for invariant in invariants:
                    self.assertNotIn(invariant["id"], invariant_ids)
                    invariant_ids.add(invariant["id"])
                    self.assertTrue(invariant["expected"])
                    self.assertTrue(invariant["oracle_test"].startswith("tests."))

                facts = owner["posix_implementation_facts"]
                self.assertTrue(facts["direct_boundaries"])
                observed_primitives.update(facts["primitives"])

        self.assertEqual(observed_kinds, EXPECTED_KINDS)
        self.assertTrue(REQUIRED_PRIMITIVES.issubset(observed_primitives))

    def test_baseline_commit_and_tree_bind_exact_historical_bytes(self) -> None:
        baseline = self.matrix["baseline"]
        commit = baseline["commit"]
        tree = baseline["tree"]
        self.assertRegex(commit, HEX40)
        self.assertRegex(tree, HEX40)
        self.assertEqual(_git("show", "-s", "--format=%T", commit).decode().strip(), tree)
        subprocess.run(
            ("git", "merge-base", "--is-ancestor", commit, "HEAD"),
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_oracle_tests_and_direct_primitive_boundaries_exist_at_baseline(self) -> None:
        commit = self.matrix["baseline"]["commit"]
        baseline_modules: dict[str, bytes] = {}
        current_modules: dict[str, bytes] = {}
        source_bytes: dict[str, bytes] = {}

        for owner in self.matrix["owners"]:
            for invariant in owner["business_invariants"]:
                test_id = invariant["oracle_test"]
                module_path, class_name, method_name = _oracle_location(test_id)
                baseline_payload = baseline_modules.setdefault(
                    module_path,
                    _baseline_bytes(commit, module_path),
                )
                current_payload = current_modules.setdefault(
                    module_path,
                    ROOT.joinpath(module_path).read_bytes(),
                )
                baseline_method = _normalized_method_bytes(
                    baseline_payload,
                    class_name,
                    method_name,
                )
                current_method = _normalized_method_bytes(
                    current_payload,
                    class_name,
                    method_name,
                )
                self.assertEqual(
                    hashlib.sha256(current_method).digest(),
                    hashlib.sha256(baseline_method).digest(),
                    test_id,
                )

            for boundary in owner["posix_implementation_facts"]["direct_boundaries"]:
                relative_path = boundary["path"]
                payload = source_bytes.setdefault(
                    relative_path,
                    _baseline_bytes(commit, relative_path),
                )
                for token in boundary["tokens"]:
                    self.assertIn(token.encode("utf-8"), payload, f"{relative_path}: {token}")

    def test_declared_direct_boundary_set_equals_approved_baseline_scan(self) -> None:
        commit = self.matrix["baseline"]["commit"]
        inventory_pattern = re.compile(
            rb"fcntl|flock|dir_fd|O_DIRECTORY|O_NOFOLLOW|os\.fsync\(|st_dev|st_ino"
        )
        paths = _git("ls-tree", "-r", "--name-only", commit).decode().splitlines()
        observed = {
            path
            for path in paths
            if path.endswith(".py")
            and "/" not in path
            and inventory_pattern.search(_baseline_bytes(commit, path))
        }
        declared = {
            boundary["path"]
            for owner in self.matrix["owners"]
            for boundary in owner["posix_implementation_facts"]["direct_boundaries"]
        }
        declared.update(
            boundary["path"] for boundary in self.matrix["deferred_boundaries"]
        )
        self.assertEqual(declared, observed)
        self.assertEqual(len(declared), 32)
        for boundary in self.matrix["deferred_boundaries"]:
            payload = _baseline_bytes(commit, boundary["path"])
            for token in boundary["tokens"]:
                self.assertIn(token.encode("utf-8"), payload, boundary)

    def test_primitive_evidence_distinguishes_file_and_directory_durability(self) -> None:
        commit = self.matrix["baseline"]["commit"]
        evidence = self.matrix["primitive_evidence"]
        self.assertEqual({item["primitive"] for item in evidence}, REQUIRED_PRIMITIVES)
        for item in evidence:
            payload = _baseline_bytes(commit, item["path"])
            self.assertIn(item["source_token"].encode("utf-8"), payload, item)
        by_primitive = {item["primitive"]: item for item in evidence}
        self.assertNotEqual(
            by_primitive["file_fsync"]["source_token"],
            by_primitive["directory_fsync"]["source_token"],
        )

    def test_runtime_runner_never_reports_static_windows_validation_as_pass(self) -> None:
        completed = subprocess.run(
            (sys.executable, "-B", str(RUNNER_PATH), "--matrix", str(MATRIX_PATH)),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        report = json.loads(completed.stdout)
        if os.name == "posix" and sys.platform in {"darwin", "linux"}:
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["skipped"], 0)
            self.assertEqual(report["failures"], 0)
            self.assertEqual(report["errors"], 0)
            self.assertGreater(report["tests_run"], 0)
        else:
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(report["status"], "STATIC_ONLY")
            self.assertEqual(report["tests_run"], 0)
            self.assertEqual(report["reason"], "POSIX_RUNTIME_REQUIRED")


if __name__ == "__main__":
    unittest.main()
