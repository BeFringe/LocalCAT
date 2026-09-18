"""Post-build binding is not runtime W3 acceptance."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools import windows_frozen_release as release
from tools.windows_frozen_manifest import SourceEntry, prepare_manifest
from tools.windows_frozen_packaging import canonical, fact


class ReleaseBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.entries = [SourceEntry("critical", "critical.py", "critical-source", b"value = 1\n"),
                        SourceEntry("fixture", "fixture.txt", "fixture", b"fixture\n")]
        self.prepared = prepare_manifest(self.entries, candidate_input_digest="a" * 64,
                                         applied_sources_digest="b" * 64)
        for entry in self.entries:
            (self.dist / entry.path).write_bytes(entry.content)
        (self.dist / "localcat-runtime.manifest").write_bytes(self.prepared.runtime_bytes)
        (self.dist / "localcat-spike.exe").write_bytes(b"final packaged PE")
        self.context = {"repository_commit": "c" * 40,
                        "source_inventory": {"tools/producer.py": fact(b"tracked producer")}}
        self.evidence = {"prelink.json": fact(self.prepared.prelink_bytes),
                         "applied-sources.json": {"bytes": 7, "sha256": "b" * 64}}

    def create(self):
        return release.create_release_binding(
            self.dist, source_context=self.context, prepared=self.prepared,
            build_evidence=self.evidence)

    def verify(self, data, anchor=None, **overrides):
        arguments = dict(expected_release_sha256=anchor or fact(data)["sha256"],
                         expected_repository_commit="c" * 40,
                         expected_candidate_input_digest="a" * 64)
        arguments.update(overrides)
        return release.verify_release_binding(data, self.dist, **arguments)

    def test_binds_result_pe_prelink_runtime_and_complete_dist_without_self_reference(self):
        data = self.create()
        result = self.verify(data)
        self.assertEqual(data, self.create())
        self.assertEqual(result["classification"], "BUILD_BINDING_NOT_W3_ACCEPTANCE")
        self.assertEqual(result["prelink"], fact(self.prepared.prelink_bytes))
        self.assertEqual(result["runtime_manifest"], fact(self.prepared.runtime_bytes))
        self.assertEqual(result["executable"], fact(b"final packaged PE"))
        self.assertEqual(set(result["dist"]), {"critical.py", "fixture.txt", "localcat-runtime.manifest", "localcat-spike.exe"})
        self.assertNotIn("release", json.loads(self.prepared.prelink_bytes))
        self.assertNotIn(str(self.root), data.decode())

    def test_actual_payload_or_runtime_drift_is_rejected_before_binding(self):
        for name in ("critical.py", "fixture.txt", "localcat-runtime.manifest"):
            with self.subTest(name=name):
                path = self.dist / name
                original = path.read_bytes()
                path.write_bytes(original + b"drift")
                with self.assertRaises(release.ReleaseBindingError):
                    self.create()
                path.write_bytes(original)

    def test_missing_extra_and_bytecode_are_rejected(self):
        original = (self.dist / "fixture.txt").read_bytes()
        (self.dist / "fixture.txt").unlink()
        with self.assertRaises(release.ReleaseBindingError):
            self.create()
        (self.dist / "fixture.txt").write_bytes(original)
        for name in ("extra.dll", "critical.pyc", "critical.pyz"):
            path = self.dist / name
            path.write_bytes(b"extra")
            with self.subTest(name=name), self.assertRaises(release.ReleaseBindingError):
                self.create()
            path.unlink()

    def test_external_anchor_rejects_self_consistent_pe_and_manifest_substitution(self):
        first = self.create()
        anchor = fact(first)["sha256"]
        (self.dist / "localcat-spike.exe").write_bytes(b"substituted PE")
        with self.assertRaises(release.ReleaseBindingError):
            self.verify(first, anchor)
        replacement = self.create()
        with self.assertRaisesRegex(release.ReleaseBindingError, "anchor"):
            self.verify(replacement, anchor)

    def test_commit_candidate_and_scope_are_not_accepted_from_report_itself(self):
        data = self.create()
        for arguments in ({"expected_repository_commit": "d" * 40},
                          {"expected_candidate_input_digest": "e" * 64}):
            with self.subTest(arguments=arguments), self.assertRaises(release.ReleaseBindingError):
                self.verify(data, **arguments)
        forged = json.loads(data)
        forged["classification"] = "W3_PASS"
        with self.assertRaises(release.ReleaseBindingError):
            self.verify(canonical(forged))

    def test_duplicate_keys_unknown_keys_and_invalid_facts_rejected_even_with_matching_digest(self):
        data = self.create()
        duplicate = b'{"schema":"forged",' + data[1:]
        with self.assertRaises(release.ReleaseBindingError):
            self.verify(duplicate)
        original = json.loads(data)
        for change in (lambda value: value.update(accepted=True),
                       lambda value: value["dist"]["critical.py"].update(bytes=True),
                       lambda value: value["source_context"]["source_inventory"].update({"../outside": fact(b"x")})):
            value = copy.deepcopy(original)
            change(value)
            with self.subTest(value=value), self.assertRaises(release.ReleaseBindingError):
                self.verify(canonical(value))

    def test_build_evidence_must_include_exact_prelink_and_applied_source_anchor(self):
        self.evidence["prelink.json"] = fact(b"different")
        with self.assertRaises(release.ReleaseBindingError):
            self.create()

    @unittest.skipUnless(os.name == "nt", "Windows junction rejection")
    def test_distribution_root_and_member_junctions_rejected(self):
        alias = self.root / "alias"
        subprocess.run([os.environ["ComSpec"], "/d", "/c", "mklink", "/J", str(alias), str(self.dist)],
                       check=True, capture_output=True)
        data = self.create()
        with self.assertRaises(release.ReleaseBindingError):
            release.verify_release_binding(data, alias, expected_release_sha256=fact(data)["sha256"],
                                           expected_repository_commit="c" * 40,
                                           expected_candidate_input_digest="a" * 64)
        external = self.root / "external"
        external.mkdir()
        subprocess.run([os.environ["ComSpec"], "/d", "/c", "mklink", "/J", str(self.dist / "extra"), str(external)],
                       check=True, capture_output=True)
        with self.assertRaises(release.ReleaseBindingError):
            self.create()


class CleanSourceContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "LocalCAT test")
        self.git("config", "user.email", "localcat-test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        for name, data in {"tools/producer.py": b"value = 1\n",
                           "packaging/windows/frozen-entry/contract.json": b"{}\n",
                           ".gitignore": b"artifacts/\n*.ignored\n",
                           "unrelated.txt": b"original\n"}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.git("add", "tools/producer.py", "packaging/windows/frozen-entry/contract.json", ".gitignore", "unrelated.txt")
        self.git("commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True).stdout

    def test_context_binds_head_and_raw_build_inputs_but_not_output_artifacts(self):
        value = release.clean_source_context(self.root)
        self.assertEqual(value["repository_commit"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(set(value["source_inventory"]),
                         {"tools/producer.py", "packaging/windows/frozen-entry/contract.json"})
        (self.root / "artifacts").mkdir()
        (self.root / "artifacts/output").write_bytes(b"generated")
        self.assertEqual(value, release.clean_source_context(self.root))

    def test_dirty_tracked_untracked_and_staged_changes_rejected(self):
        for name in ("unrelated.txt", "new.txt"):
            path = self.root / name
            existed = path.exists()
            original = path.read_bytes() if existed else None
            path.write_bytes(b"changed")
            with self.subTest(name=name), self.assertRaises(release.ReleaseBindingError):
                release.clean_source_context(self.root)
            if existed:
                path.write_bytes(original)
            else:
                path.unlink()
        (self.root / "new.txt").write_bytes(b"staged")
        self.git("add", "new.txt")
        with self.assertRaises(release.ReleaseBindingError):
            release.clean_source_context(self.root)

    def test_assume_unchanged_and_ignored_input_cannot_hide_build_drift(self):
        self.git("update-index", "--assume-unchanged", "tools/producer.py")
        (self.root / "tools/producer.py").write_bytes(b"hidden drift")
        with self.assertRaises(release.ReleaseBindingError):
            release.clean_source_context(self.root)
        (self.root / "tools/producer.py").write_bytes(b"value = 1\n")
        (self.root / "tools/hidden.ignored").write_bytes(b"hidden input")
        with self.assertRaises(release.ReleaseBindingError):
            release.clean_source_context(self.root)


if __name__ == "__main__":
    unittest.main()
