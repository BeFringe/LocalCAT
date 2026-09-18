"""Actual PyInstaller packaging boundaries, not the complete W3 matrix."""
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from tools import windows_frozen_packaging as packaging


class FrozenPackagingTests(unittest.TestCase):
    def test_readonly_dependency_projection_includes_transitive_helpers_and_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for modules, pattern in packaging.DEPENDENCIES:
                info = root / pattern.replace("*", "1.0")
                info.mkdir()
                (info / "METADATA").write_bytes(b"Name: fixture\nVersion: 1.0\n")
                for module in modules:
                    path = root / module
                    if path.suffix == ".py":
                        path.write_bytes(b"# exact module\n")
                    else:
                        path.mkdir()
                        (path / "__init__.py").write_bytes(b"# exact package\n")
            members = packaging.dependency_members(root)
            self.assertIn("ordlookup/__init__.py", members)
            self.assertIn("peutils.py", members)
            self.assertIn("pefile-1.0.dist-info/METADATA", members)
            (root / "ambient.pth").write_bytes(b"import arbitrary\n")
            self.assertEqual(members, packaging.dependency_members(root))
            output = root / "copied"
            inventory = packaging.copy_dependencies(root, output)
            expected = {"dependency_inventory": packaging.inventory_fact(inventory)}
            (output / "ordlookup/__init__.py").write_bytes(b"# changed after snapshot\n")
            with self.assertRaises(packaging.PackagingInputError):
                packaging.require_candidate_dependencies(packaging.tree_inventory(output), expected)
            (root / "pefile-2.0.dist-info").mkdir()
            with self.assertRaisesRegex(packaging.PackagingInputError, "ambiguous"):
                packaging.dependency_members(root)

    def test_candidate_packaging_fact_binds_source_and_dependency_bytes_portably(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in packaging.BUILD_SOURCE_FILES:
                source = root / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"# exact build driver\n")
            dependencies = {"pefile.py": b"# original\n", "ordlookup/__init__.py": b"# lookup\n"}
            with mock.patch.object(packaging, "dependency_members", return_value=dependencies):
                first = packaging.candidate_packaging_fact(root, root / "site")
                self.assertEqual(first, packaging.candidate_packaging_fact(root, root / "elsewhere"))
                source.write_bytes(b"# changed driver\n")
                self.assertNotEqual(first, packaging.candidate_packaging_fact(root, root / "site"))
                source.write_bytes(b"# exact build driver\n")
                dependencies["ordlookup/__init__.py"] = b"# changed dependency\n"
                self.assertNotEqual(first, packaging.candidate_packaging_fact(root, root / "site"))

    def test_snapshot_must_match_candidate_before_packaging_child_can_run(self):
        dependencies = {"module.py": packaging.fact(b"original")}
        expected = {"dependency_inventory": packaging.inventory_fact(dependencies)}
        packaging.require_candidate_dependencies(dependencies, expected)
        for changed in ({}, {**dependencies, "extra.py": packaging.fact(b"extra")},
                        {"module.py": packaging.fact(b"changed")}):
            with self.subTest(changed=changed), self.assertRaises(packaging.PackagingInputError):
                packaging.require_candidate_dependencies(changed, expected)

    def test_spec_collects_exact_paths_without_analysis_or_bytecode(self):
        spec = packaging.render_spec([
            ("_internal/critical.py", "payload/critical.py", "DATA"),
            ("localcat-runtime.manifest", "payload/runtime", "DATA"),
            ("_internal/python314.dll", "payload/python314.dll", "BINARY"),
        ])
        observed = {}

        class EXE:
            def __init__(self, *args, **kwargs):
                observed["args"] = args
                observed["kwargs"] = kwargs
                self.manifest = b"upstream inserts Common-Controls"
                self.__postinit__()

            def __postinit__(self):
                observed["manifest_at_guts_and_assemble"] = self.manifest

        def collect(exe, toc, **kwargs):
            observed["toc"] = toc
            observed["collect"] = kwargs

        exec(compile(spec, "generated.spec", "exec"),
             {"EXE": EXE, "COLLECT": collect, "os": os, "SPECPATH": "bound"})
        self.assertEqual(observed["args"], ([],))
        self.assertFalse(observed["kwargs"]["console"])
        self.assertTrue(observed["kwargs"]["exclude_binaries"])
        self.assertTrue(observed["kwargs"]["append_pkg"])
        self.assertEqual(observed["kwargs"]["contents_directory"], ".")
        self.assertEqual(observed["kwargs"]["icon"], "NONE")
        self.assertNotIn(b"dependency", observed["manifest_at_guts_and_assemble"])
        self.assertIn(b'asInvoker', observed["manifest_at_guts_and_assemble"])
        self.assertEqual(observed["toc"][0][0], "_internal/critical.py")
        self.assertEqual(observed["toc"][0][1], os.path.join("bound", "payload/critical.py"))
        self.assertNotIn("Analysis(", spec)
        self.assertNotIn("PYZ(", spec)

    def test_spec_requires_relative_input_paths_for_identical_build_inputs(self):
        with self.assertRaises(packaging.PackagingInputError):
            packaging.render_spec([("fixture.txt", "C:/machine-specific/fixture.txt", "DATA")])

    def test_child_cannot_self_rebase_inputs_and_parent_rejects_other_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inputs = {"payload": {"fixture.txt": packaging.fact(b"fixture")}}
            initial = packaging.canonical(inputs)
            digest = packaging.fact(initial)["sha256"]
            (root / "inputs.json").write_bytes(initial)
            self.assertEqual(packaging.load_packaging_inputs(root, digest), inputs)
            inputs["payload"]["fixture.txt"] = packaging.fact(b"other")
            (root / "inputs.json").write_bytes(packaging.canonical(inputs))
            with self.assertRaisesRegex(packaging.PackagingInputError, "input anchor"):
                packaging.load_packaging_inputs(root, digest)
            (root / "result.json").write_text(json.dumps({"inputs_digest": "0" * 64}))
            with self.assertRaisesRegex(packaging.PackagingInputError, "result anchor"):
                packaging.verify_packaging_result(root, json.loads(initial))

    def test_spec_rejects_duplicate_or_outside_or_executable_source_members(self):
        for toc in (
            [("a.py", "x", "PYSOURCE")],
            [("../a", "x", "DATA")],
            [("C:/a", "x", "DATA")],
            [("a", "x", "DATA"), ("A", "y", "DATA")],
            [("a.pyc", "x", "DATA")],
            [("__pycache__/a", "x", "DATA")],
        ):
            with self.subTest(toc=toc), self.assertRaises(packaging.PackagingInputError):
                packaging.render_spec(toc)

    def test_sdist_is_authenticated_before_extracting_package_and_rejects_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                content = b"__version__ = 'pinned'\n"
                info = tarfile.TarInfo("pinned/PyInstaller/__init__.py")
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
            expected = packaging.fact(archive.read_bytes())
            destination = root / "package"
            packaging.extract_packager(archive, expected, destination)
            self.assertEqual((destination / "PyInstaller/__init__.py").read_bytes(), content)
            with self.assertRaises(packaging.PackagingInputError):
                packaging.extract_packager(archive, {**expected, "sha256": "0" * 64}, root / "wrong")
            self.assertFalse((root / "wrong").exists())
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("pinned/PyInstaller/evil")
                info.type = tarfile.SYMTYPE
                info.linkname = "../../outside"
                tar.addfile(info)
            with self.assertRaises(packaging.PackagingInputError):
                packaging.extract_packager(archive, packaging.fact(archive.read_bytes()), root / "link")

    def test_dist_verification_requires_exact_inventory_and_unchanged_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "localcat-spike.exe").write_bytes(b"final PE")
            (root / "fixture.txt").write_bytes(b"fixture")
            expected = {"fixture.txt": packaging.fact(b"fixture")}
            release = packaging.release_inventory(root, expected)
            self.assertEqual(release["localcat-spike.exe"], packaging.fact(b"final PE"))
            (root / "extra.pyz").write_bytes(b"duplicate")
            with self.assertRaises(packaging.PackagingInputError):
                packaging.release_inventory(root, expected)
            (root / "extra.pyz").unlink()
            (root / "fixture.txt").write_bytes(b"tampered")
            with self.assertRaisesRegex(packaging.PackagingInputError, "payload"):
                packaging.release_inventory(root, expected)


if __name__ == "__main__":
    unittest.main()
