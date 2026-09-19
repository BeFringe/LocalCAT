"""Closure discovery tests use bytes, never import the discovered application."""
from __future__ import annotations

import hashlib
import base64
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import Any
import zipfile

from tools import generate_windows_frozen_manifest as generator


ROOT = Path(__file__).resolve().parents[1]


def encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


class SourceGeneratorTests(unittest.TestCase):
    root: Path = ROOT

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.write("app.py", b"import leaf\nimport importlib\nNAME = 'dyn' + 'amic'\ndef load():\n    return importlib.import_module(NAME)\nraise RuntimeError('MUST NOT EXECUTE')\n")
        self.write("leaf.py", b"import app\n")
        self.write("dynamic.py", b"VALUE = 42\n")
        self.write("fixture.txt", b"fixture bytes\n")
        self.write("packaging/windows/frozen_roots.json", encode({
            "schema_version": "localcat-frozen-owner-roots-v1",
            "scope": "prebuild-owner-declarations",
            "collection_policy": {"project_source_modules": "py", "gate_inputs": "preserve-repository-relative-layout", "recursive_closure_owner": "windows-platform-enablement 7.1", "native_bootstrap_owner": "windows-platform-enablement 7.2"},
            "owners": [{"dispatch_id": "TEST", "revision": "R1", "owning_spec": "test", "reason": "test application", "module_roots": ["app"],
                        "assets": [{"path": "fixture.txt", "kind": "fixture", "reason": "test resource"}],
                        "dynamic_imports": [{"source": "app.py", "symbol": "NAME", "phase": "post-authority", "reason": "test dynamic"}]}],
            "platform": {"owning_spec": "test", "module_roots": ["leaf"]},
        }))
        self.git("init", "--quiet")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.commit()

    def git(self, *args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.STDOUT)

    def commit(self) -> None:
        self.git("add", "--", "app.py", "leaf.py", "dynamic.py", "fixture.txt", "packaging/windows/frozen_roots.json")
        self.git("commit", "--quiet", "-m", "isolated fixture")

    def write(self, name: str, content: bytes) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def generate(self):
        return generator.generate(self.root, mode="development")

    def test_recursive_cycle_dynamic_and_resource_are_real_bytes(self) -> None:
        result = self.generate()
        entries = {entry["path"]: entry for entry in result.manifest["entries"]}
        self.assertTrue({"app.py", "leaf.py", "dynamic.py", "fixture.txt"} <= entries.keys())
        self.assertEqual(entries["fixture.txt"]["sha256"], hashlib.sha256(b"fixture bytes\n").hexdigest())
        self.assertIn("leaf.py", entries["app.py"]["dependencies"])
        self.assertIn("app.py", entries["leaf.py"]["dependencies"])
        self.assertIn("dynamic.py", entries["app.py"]["dependencies"])
        self.assertIn(b"'app': 'py'", result.hook_bytes)
        self.assertEqual(result.manifest["classification"], "DEVELOPMENT_CLOSURE_NOT_RELEASE")

    def test_deterministic_bytes_and_reasons(self) -> None:
        left, right = self.generate(), self.generate()
        self.assertEqual(left.manifest_bytes, right.manifest_bytes)
        self.assertEqual(left.hook_bytes, right.hook_bytes)
        self.assertTrue(all(entry["reasons"] for entry in left.manifest["entries"]))
        paths = [entry["path"] for entry in left.manifest["entries"]]
        self.assertEqual(paths, sorted(paths, key=lambda value: value.encode("utf-8")))

    def test_missing_transitive_source_is_rejected(self) -> None:
        (self.root / "leaf.py").unlink()
        with self.assertRaisesRegex(generator.ManifestError, "leaf.py"):
            self.generate()

    def test_unknown_import_is_rejected(self) -> None:
        self.write("leaf.py", b"import unapproved_business_dependency\n")
        with self.assertRaisesRegex(generator.ManifestError, "unapproved_business_dependency"):
            self.generate()

    def test_unknown_dynamic_call_is_rejected_even_in_declared_module(self) -> None:
        self.write("app.py", (self.root / "app.py").read_bytes() + b"\ndef surprise(name):\n    return importlib.import_module(name)\n")
        with self.assertRaisesRegex(generator.ManifestError, "dynamic"):
            self.generate()

    def test_undeclared_literal_dynamic_is_rejected(self) -> None:
        self.write("app.py", b"import importlib\nNAME = 'dynamic'\nimportlib.import_module('hidden')\n")
        self.write("hidden.py", b"secret = True\n")
        with self.assertRaisesRegex(generator.ManifestError, "hidden"):
            self.generate()

    def test_missing_fixture_is_rejected(self) -> None:
        (self.root / "fixture.txt").unlink()
        with self.assertRaisesRegex(generator.ManifestError, "fixture.txt"):
            self.generate()

    def test_unsafe_owner_path_is_rejected(self) -> None:
        roots = self.root / "packaging/windows/frozen_roots.json"
        value = json.loads(roots.read_bytes())
        for path in ("../outside.txt", "C:/outside.txt", "sub/CON.txt", "sub\\file.txt", "sub/__pycache__/file.pyc"):
            with self.subTest(path=path):
                value["owners"][0]["assets"][0]["path"] = path
                roots.write_bytes(encode(value))
                with self.assertRaises(generator.ManifestError):
                    self.generate()

    def test_untracked_actual_input_is_rejected(self) -> None:
        self.git("rm", "--cached", "--", "dynamic.py")
        with self.assertRaisesRegex(generator.ManifestError, "untracked.*dynamic.py"):
            self.generate()

    def test_unrelated_persistent_lock_does_not_become_input(self) -> None:
        self.write(".persistent-owner.lock", b"owner lock\n")
        self.assertNotIn(".persistent-owner.lock", [entry["path"] for entry in self.generate().manifest["entries"]])

    def test_production_rejects_missing_product_build_inputs(self) -> None:
        with self.assertRaisesRegex(generator.ManifestError, "build.*input|production"):
            generator.generate(self.root, mode="production")

    def test_real_checkout_closure_contains_owner_gates_and_assets(self) -> None:
        result = generator.generate(ROOT, mode="development")
        entries = {entry["path"]: entry for entry in result.manifest["entries"]}
        mandatory = {"capability_host.py", "tm_benchmark_process.py", "tm_benchmark_query_process.py", "tm_gate_inputs.py", "platform_fs_windows.py", "tm_stage_sealer.py", "tests/fixtures/unicode-16.0.0-WordBreakTest.txt", "tests/fixtures/retrieval_gate_c_vectors_v1.json", "benchmark_tm_contract.json", "tm.jsonl", "terms.csv", "LocalCAT-logo-silver.png"}
        self.assertTrue(mandatory <= entries.keys(), sorted(mandatory - entries.keys()))
        self.assertGreater(sum(entry["kind"] == "critical-source" for entry in entries.values()), 100)
        self.assertNotIn("durability_profiles.json", entries)
        for path in mandatory:
            self.assertEqual(entries[path]["sha256"], hashlib.sha256((ROOT / path).read_bytes()).hexdigest())

    def test_dynamic_import_alias_cannot_escape_inventory(self) -> None:
        self.write("app.py", b"import importlib\nNAME = 'dynamic'\nf = importlib.import_module\nf('secret')\n")
        with self.assertRaisesRegex(generator.ManifestError, "dynamic"):
            self.generate()

    def test_unknown_nested_module_is_not_satisfied_by_package_parent(self) -> None:
        self.write("pkg/__init__.py", b"")
        self.git("add", "--", "pkg/__init__.py")
        self.git("commit", "--quiet", "-m", "package fixture")
        self.write("leaf.py", b"import pkg.missing\n")
        with self.assertRaisesRegex(generator.ManifestError, "pkg.missing"):
            self.generate()

    def test_production_rejects_unrelated_tracked_dirty_tree(self) -> None:
        self.write("leaf.py", b"VALUE = 'dirty'\n")
        with self.assertRaisesRegex(generator.ManifestError, "clean tracked"):
            generator.generate(self.root, mode="production")


def make_wheel() -> tuple[bytes, bytes]:
    payload = b"# isolated wheel fixture\n"
    name = "fixturedep/__init__.py"
    record = "fixturedep-1.dist-info/RECORD"
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(name, payload)
        archive.writestr(record, f"{name},sha256={digest},{len(payload)}\n{record},,\n")
    return stream.getvalue(), payload


class BuildInputTests(SourceGeneratorTests):
    bindings: dict[str, Path] = {}
    plan: dict[str, Any] = {}

    def setUp(self) -> None:
        super().setUp()
        wheel, payload = make_wheel()
        stdlib_stream = io.BytesIO()
        with zipfile.ZipFile(stdlib_stream, "w") as archive:
            archive.writestr("encodings/__init__.py", b"# isolated stdlib input\n")
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        base = Path(external.name)
        files = {
            "wheels": ("locked-wheels", {"fixturedep-1-py3-none-any.whl": wheel}),
            "python": ("python-runtime", {"python314.dll": b"MZisolated Python input", "python314.zip": stdlib_stream.getvalue()}),
            "native": ("native-runtime", {"fixturedep/__init__.py": payload, "plugins/qwindows.dll": b"MZisolated Qt plugin input"}),
            "boot": ("bootloader", {"runw.exe": b"MZisolated bootloader input"}),
            "toolchain": ("build-toolchain", {"compiler.bin": b"isolated compiler input"}),
        }
        self.bindings = {}
        trees = {}
        for name, (kind, members) in files.items():
            location = base / name
            self.bindings[name] = location
            for path, content in members.items():
                target = location / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            trees[name] = {"kind": kind, "files": {path: generator.fact(content) for path, content in members.items()}}
        # Synthetic input graphs deliberately do not claim real native proof.
        source_files = {
            "packaging/windows/LocalCAT.spec": b"# isolated specification fixture\n",
            "frozen_source_bootstrap.py": b"# isolated bootstrap fixture\n",
            "packaging/windows/LocalCAT.ico": b"isolated icon input",
            "packaging/windows/version_info.txt": b"isolated version input",
            "packaging/windows/requirements-build.lock": b"fixturedep==1\n",
            generator.GENERATOR_PATH: (ROOT / generator.GENERATOR_PATH).read_bytes(),
        }
        for path, content in source_files.items():
            self.write(path, content)
        roots_path = self.root / generator.ROOTS_PATH
        roots = json.loads(roots_path.read_bytes())
        roots["owners"][0]["runtime_import_roots"] = ["fixturedep"]
        roots["owners"][0]["qt_plugin_roots"] = ["plugins/qwindows.dll"]
        roots_path.write_bytes(encode(roots))
        self.plan = {
            "schema_version": generator.BUILD_SCHEMA,
            "build_sources": {path: generator.fact(content) for path, content in source_files.items()},
            "input_trees": trees,
            "runtime_imports": {"fixturedep": "native/fixturedep/__init__.py"},
            "wheel_members": {"native/fixturedep/__init__.py": {"wheel": "wheels/fixturedep-1-py3-none-any.whl", "member": "fixturedep/__init__.py"}},
            "native": {
                "entries": {"python/python314.dll": {"bundle_path": "python314.dll", "dependencies": []}, "native/plugins/qwindows.dll": {"bundle_path": "plugins/qwindows.dll", "dependencies": ["python/python314.dll"]}, "boot/runw.exe": {"bundle_path": "runw.exe", "dependencies": ["python/python314.dll"]}},
                "dynamic_roots": ["native/plugins/qwindows.dll"], "python_dll": "python/python314.dll", "bootloader": "boot/runw.exe",
            },
            "resulting_pe": "LocalCAT.exe",
        }
        self.save_plan()
        self.git("add", "--", *source_files, generator.ROOTS_PATH, generator.BUILD_PLAN_PATH)
        self.git("commit", "--quiet", "-m", "isolated production input contract fixture")

    def save_plan(self) -> None:
        self.write(generator.BUILD_PLAN_PATH, encode(self.plan))

    def commit_plan(self) -> None:
        self.save_plan()
        self.git("add", "--", generator.BUILD_PLAN_PATH)
        self.git("commit", "--quiet", "-m", "isolated changed input contract")

    def production(self):
        return generator.generate(self.root, mode="production", input_roots=self.bindings)

    def test_isolated_complete_input_contract_is_deterministic(self) -> None:
        result = self.production()
        self.assertEqual(result.manifest_bytes, self.production().manifest_bytes)
        inputs = result.manifest["build_inputs"]
        self.assertEqual(inputs["generated_hook"]["sha256"], hashlib.sha256(result.hook_bytes).hexdigest())
        self.assertEqual(inputs["native"]["graph_kind"], "native-input-dag-not-runtime-attestation")
        self.assertEqual(inputs["resulting_pe_excluded"], "LocalCAT.exe")

    def test_unlisted_real_input_tree_member_is_rejected(self) -> None:
        (self.bindings["native"] / "extra.dll").write_bytes(b"MZnot declared")
        with self.assertRaisesRegex(generator.ManifestError, "membership mismatch"):
            self.production()

    def test_missing_and_tampered_tree_member_is_rejected(self) -> None:
        target = self.bindings["python"] / "python314.dll"
        target.write_bytes(b"changed")
        with self.assertRaisesRegex(generator.ManifestError, "digest/bytes mismatch"):
            self.production()
        target.unlink()
        with self.assertRaisesRegex(generator.ManifestError, "membership mismatch"):
            self.production()

    def test_missing_input_digest_is_rejected(self) -> None:
        del self.plan["input_trees"]["python"]["files"]["python314.dll"]["sha256"]
        self.commit_plan()
        with self.assertRaisesRegex(generator.ManifestError, "content address"):
            self.production()

    def test_wheel_provenance_is_checked_against_real_archive_bytes(self) -> None:
        target = self.bindings["native"] / "fixturedep/__init__.py"
        target.write_bytes(b"tampered deployed package")
        self.plan["input_trees"]["native"]["files"]["fixturedep/__init__.py"] = generator.fact(target.read_bytes())
        self.commit_plan()
        with self.assertRaisesRegex(generator.ManifestError, "wheel/runtime byte mismatch"):
            self.production()

    def test_native_cycle_and_unknown_dynamic_root_are_rejected(self) -> None:
        native = self.plan["native"]
        native["entries"]["python/python314.dll"]["dependencies"] = ["boot/runw.exe"]
        self.commit_plan()
        with self.assertRaisesRegex(generator.ManifestError, "native dependency cycle"):
            self.production()
        native["entries"]["python/python314.dll"]["dependencies"] = []
        native["dynamic_roots"] = ["native/unknown.dll"]
        self.commit_plan()
        with self.assertRaisesRegex(generator.ManifestError, "dynamic native root"):
            self.production()

    def test_resulting_pe_self_reference_is_rejected(self) -> None:
        target = self.bindings["boot"] / "LocalCAT.exe"
        target.write_bytes(b"MZfuture result")
        self.plan["input_trees"]["boot"]["files"]["LocalCAT.exe"] = generator.fact(target.read_bytes())
        self.commit_plan()
        with self.assertRaisesRegex(generator.ManifestError, "resulting PE"):
            self.production()

    def test_bytecode_and_durability_inputs_are_rejected(self) -> None:
        for name in ("app.pyc", "__pycache__/app.pyc", "durability_profiles.json", "power-loss.json"):
            with self.subTest(name=name):
                with self.assertRaises(generator.ManifestError):
                    generator.relative(name)

    def test_untracked_input_tree_inside_checkout_is_rejected(self) -> None:
        self.bindings["boot"] = self.root / "untracked-inputs"
        with self.assertRaisesRegex(generator.ManifestError, "untracked repository input tree"):
            self.production()

    def test_unlisted_actual_build_helper_is_rejected(self) -> None:
        self.write("build_helper.py", b"VALUE = 1\n")
        self.write("packaging/windows/LocalCAT.spec", b"import build_helper\n")
        self.plan["build_sources"]["packaging/windows/LocalCAT.spec"] = generator.fact(b"import build_helper\n")
        self.save_plan()
        self.git("add", "--", "build_helper.py", "packaging/windows/LocalCAT.spec", generator.BUILD_PLAN_PATH)
        self.git("commit", "--quiet", "-m", "unlisted helper fixture")
        with self.assertRaisesRegex(generator.ManifestError, "unlisted transitive build sources"):
            self.production()

    def test_untracked_hook_in_actual_definition_directory_is_rejected(self) -> None:
        self.write("packaging/windows/hooks/hook-hidden.py", b"# untracked build hook\n")
        with self.assertRaisesRegex(generator.ManifestError, "untracked build definitions"):
            self.production()

    def test_duplicate_critical_source_in_native_tree_is_rejected(self) -> None:
        content = (self.root / "app.py").read_bytes()
        (self.bindings["native"] / "app.py").write_bytes(content)
        self.plan["input_trees"]["native"]["files"]["app.py"] = generator.fact(content)
        self.commit_plan()
        with self.assertRaisesRegex(generator.ManifestError, "duplicate critical"):
            self.production()

    def test_critical_bytecode_hidden_in_runtime_zip_is_rejected(self) -> None:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("__pycache__/app.cpython-314.pyc", b"hidden critical bytecode")
        target = self.bindings["python"] / "python314.zip"
        target.write_bytes(stream.getvalue())
        self.plan["input_trees"]["python"]["files"]["python314.zip"] = generator.fact(target.read_bytes())
        self.commit_plan()
        with self.assertRaisesRegex(generator.ManifestError, "duplicate critical archive"):
            self.production()

    def test_collection_missing_tamper_bytecode_and_pyz_duplicate_fail_closed(self) -> None:
        manifest = self.generate()
        members = {entry["path"]: (self.root / entry["path"]).read_bytes() for entry in manifest.manifest["entries"]}
        generator.validate_collection(manifest, members, pyz_modules=[])
        with self.assertRaisesRegex(generator.ManifestError, "PYZ duplicate"):
            generator.validate_collection(manifest, members, pyz_modules=["app"])
        for added in ("other/app.py", "app.pyc", "__pycache__/app.pyc"):
            with self.subTest(added=added), self.assertRaises(generator.ManifestError):
                generator.validate_collection(manifest, {**members, added: members["app.py"]}, pyz_modules=[])
        with self.assertRaisesRegex(generator.ManifestError, "digest/bytes mismatch"):
            generator.validate_collection(manifest, {**members, "app.py": b"changed"}, pyz_modules=[])
        del members["fixture.txt"]
        with self.assertRaisesRegex(generator.ManifestError, "missing collected inputs"):
            generator.validate_collection(manifest, members, pyz_modules=[])

    def test_missing_product_build_inputs(self) -> None:
        (self.root / "packaging/windows/LocalCAT.ico").unlink()
        with self.assertRaisesRegex(generator.ManifestError, "clean tracked"):
            self.production()

    # The base fixture's missing-plan expectation does not apply to this one.
    def test_production_rejects_missing_product_build_inputs(self) -> None:
        with self.assertRaisesRegex(generator.ManifestError, "input root binding"):
            generator.generate(self.root, mode="production")

    def remediation_commit(self, changes: dict[str, bytes], *, build_files: tuple[str, ...] = ()) -> None:
        for path, content in changes.items():
            self.write(path, content)
            if path in self.plan["build_sources"] or path in build_files:
                self.plan["build_sources"][path] = generator.fact(content)
        self.save_plan()
        self.git("add", "--", *changes, generator.BUILD_PLAN_PATH)
        self.git("commit", "--quiet", "-m", "isolated remediation counterexample")

    def test_remediation_dynamic_parameter_shadow_rejected(self) -> None:
        self.remediation_commit({"app.py": b"import importlib\nNAME = 'dynamic'\ndef load(NAME):\n    return importlib.import_module(NAME)\n"})
        with self.assertRaises(generator.ManifestError):
            self.production()

    def test_remediation_r2_global_binding_forms_reject_stale_constant(self) -> None:
        bodies = (
            "from chosen import TARGET as NAME",
            "import chosen as NAME",
            "match 'hidden':\n    case NAME:\n        pass",
            "match ['hidden']:\n    case [*NAME]:\n        pass",
            "match {'a': 'hidden'}:\n    case {**NAME}:\n        pass",
            "try:\n    pass\nexcept Exception as NAME:\n    pass",
            "try:\n    pass\nexcept* Exception as NAME:\n    pass",
            "def NAME():\n    pass",
            "class NAME:\n    pass",
        )
        for body in bodies:
            with self.subTest(body=body):
                source = "import importlib\nNAME = 'dynamic'\ndef switch():\n    global NAME\n" + "\n".join("    " + line for line in body.splitlines()) + "\ndef load():\n    return importlib.import_module(NAME)\n"
                self.remediation_commit({"app.py": source.encode(), "chosen.py": b"TARGET = 'hidden'\n"})
                with self.assertRaisesRegex(generator.ManifestError, "nonliteral"):
                    self.production()

    def test_remediation_r2_global_callable_rebinding_rejected(self) -> None:
        for declaration, name, call in (("import importlib", "importlib", "importlib.import_module(NAME)"), ("from importlib import import_module as load", "load", "load(NAME)"), ("", "__import__", "__import__(NAME)")):
            for binding in (f"import os as {name}", f"match object():\n    case {name}:\n        pass", f"def {name}():\n    pass"):
                with self.subTest(name=name, binding=binding):
                    source = declaration + "\nNAME = 'dynamic'\ndef switch():\n    global " + name + "\n" + "\n".join("    " + line for line in binding.splitlines()) + "\ndef run():\n    return " + call + "\n"
                    self.remediation_commit({"app.py": source.encode()})
                    with self.assertRaisesRegex(generator.ManifestError, "shadowed dynamic callable binding"):
                        self.production()

    def test_remediation_r2_nonlocal_binding_rejected(self) -> None:
        sources = (
            "import importlib\nNAME = 'dynamic'\ndef outer():\n    NAME = 'dynamic'\n    def switch():\n        nonlocal NAME\n        from chosen import TARGET as NAME\n    def load():\n        return importlib.import_module(NAME)\n",
            "NAME = 'dynamic'\ndef outer():\n    import importlib\n    def switch():\n        nonlocal importlib\n        import os as importlib\n    def load():\n        return importlib.import_module(NAME)\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.remediation_commit({"app.py": source.encode(), "chosen.py": b"TARGET = 'hidden'\n"})
                with self.assertRaisesRegex(generator.ManifestError, "shadowed dynamic"):
                    self.production()

    def test_remediation_r2_correct_scope_preserves_global_constant(self) -> None:
        sources = (
            "import importlib\nNAME = 'dynamic'\ndef outer():\n    NAME = 'hidden'\n    def load():\n        global NAME\n        return importlib.import_module(NAME)\n",
            "import importlib\nNAME = 'dynamic'\nclass Holder:\n    NAME = 'hidden'\n    def load(self):\n        return importlib.import_module(NAME)\n",
            "import importlib\nNAME = 'dynamic'\nvalues = [NAME for NAME in ('hidden',)]\ndef load():\n    return importlib.import_module(NAME)\n",
            "import importlib\nNAME = 'dynamic'\ndef unrelated(NAME):\n    NAME = 'hidden'\ndef load():\n    return importlib.import_module(NAME)\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.remediation_commit({"app.py": source.encode()})
                manifest = self.production().manifest
                self.assertIn("dynamic.py", {entry["path"] for entry in manifest["entries"]})

    def test_remediation_r2_class_global_and_outer_expression_writes_rejected(self) -> None:
        sources = (
            "class Holder:\n    global NAME\n    from chosen import TARGET as NAME\n",
            "def switch(value=(NAME := 'hidden')):\n    pass\n",
            "values = [(NAME := 'hidden') for item in (1,)]\n",
            "def switch():\n    global NAME\n    values = [(NAME := 'hidden') for item in (1,)]\n",
        )
        for source in sources:
            with self.subTest(source=source):
                complete = "import importlib\nNAME = 'dynamic'\n" + source + "def load():\n    return importlib.import_module(NAME)\n"
                self.remediation_commit({"app.py": complete.encode(), "chosen.py": b"TARGET = 'hidden'\n"})
                with self.assertRaisesRegex(generator.ManifestError, "nonliteral"):
                    self.production()

    def test_remediation_r2_unknown_annotation_scope_rejected(self) -> None:
        for source in ("def load[NAME]():\n    return importlib.import_module(NAME)\n", "type Alias[NAME] = importlib.import_module(NAME)\n"):
            with self.subTest(source=source):
                self.remediation_commit({"app.py": ("import importlib\nNAME = 'dynamic'\n" + source).encode()})
                with self.assertRaises(generator.ManifestError):
                    self.production()

    def test_remediation_r2_local_alias_cannot_mutate_owner_mapping(self) -> None:
        roots = json.loads((self.root / generator.ROOTS_PATH).read_bytes())
        roots["owners"][0]["worker_dispatch"] = {"source": "app.py", "symbol": "WORKERS", "reason": "scope-aware mutable owner mapping"}
        self.remediation_commit({"app.py": b"NAME = 'dynamic'\nWORKERS = {'migration': 'dynamic'}\ndef switch():\n    alias = WORKERS\n    alias['query'] = 'hidden'\n", generator.ROOTS_PATH: encode(roots)})
        with self.assertRaisesRegex(generator.ManifestError, "nonliteral"):
            self.production()

    def test_remediation_dynamic_callable_rebinding_rejected(self) -> None:
        bindings = (
            "import importlib\nimport os as importlib\nimportlib.import_module('dynamic')\n",
            "from importlib import import_module as load\nfrom os import getenv as load\nload('dynamic')\n",
            "import importlib\ntry:\n    pass\nexcept Exception as importlib:\n    importlib.import_module('dynamic')\n",
            "from importlib import import_module as load\ntry:\n    pass\nexcept Exception as load:\n    load('dynamic')\n",
            "import importlib\nmatch object():\n    case importlib:\n        importlib.import_module('dynamic')\n",
            "from importlib import import_module as load\nmatch object():\n    case load:\n        load('dynamic')\n",
        )
        for source in bindings:
            with self.subTest(source=source):
                self.remediation_commit({"app.py": b"NAME = 'dynamic'\n" + source.encode()})
                with self.assertRaisesRegex(generator.ManifestError, "shadowed dynamic callable binding"):
                    self.production()

    def test_remediation_dynamic_local_shadow_rejected(self) -> None:
        self.remediation_commit({"app.py": b"import importlib\nNAME = 'dynamic'\ndef load():\n    NAME = 'hidden'\n    return importlib.import_module(NAME)\n", "hidden.py": b"VALUE = 'hidden'\n"})
        with self.assertRaises(generator.ManifestError):
            self.production()

    def test_remediation_dynamic_comprehension_shadow_rejected(self) -> None:
        self.remediation_commit({"app.py": b"import importlib\nNAME = 'dynamic'\ndef load(names):\n    return [importlib.import_module(NAME) for NAME in names]\n"})
        with self.assertRaises(generator.ManifestError):
            self.production()

    def test_remediation_visible_global_rebinding_rejected(self) -> None:
        self.remediation_commit({"app.py": b"import importlib\nNAME = 'dynamic'\ndef switch():\n    global NAME\n    NAME = 'hidden'\ndef load():\n    return importlib.import_module(NAME)\n", "hidden.py": b"VALUE = 'hidden'\n"})
        with self.assertRaises(generator.ManifestError):
            self.production()

    def test_remediation_reflective_global_rebinding_rejected(self) -> None:
        self.remediation_commit({"app.py": b"import importlib\nNAME = 'dynamic'\nglobals()['NAME'] = 'hidden'\ndef load():\n    return importlib.import_module(NAME)\n", "hidden.py": b"VALUE = 'hidden'\n"})
        with self.assertRaises(generator.ManifestError):
            self.production()

    def test_remediation_conditional_module_rebinding_rejected(self) -> None:
        self.remediation_commit({"app.py": b"import importlib\nNAME = 'dynamic'\nif True:\n    NAME = 'hidden'\ndef load():\n    return importlib.import_module(NAME)\n", "hidden.py": b"VALUE = 'hidden'\n"})
        with self.assertRaises(generator.ManifestError):
            self.production()

    def test_remediation_worker_dictionary_unpack_and_override_complete(self) -> None:
        source = b"NAME = 'dynamic'\nEXTRA = {'query': 'hidden'}\nWORKERS = {'migration': 'dynamic', 'query': 'dynamic', **EXTRA}\n"
        roots_path = self.root / generator.ROOTS_PATH
        roots = json.loads(roots_path.read_bytes())
        roots["owners"][0]["worker_dispatch"] = {"source": "app.py", "symbol": "WORKERS", "reason": "original dictionary unpack counterexample"}
        self.remediation_commit({"app.py": source, "hidden.py": b"VALUE = 'hidden'\n", generator.ROOTS_PATH: encode(roots)})
        result = self.production()
        self.assertIn("hidden.py", {entry["path"] for entry in result.manifest["entries"]})

    def test_remediation_unresolved_worker_dictionary_unpack_rejected(self) -> None:
        source = b"NAME = 'dynamic'\ndef build_workers():\n    return {'query': 'hidden'}\nEXTRA = build_workers()\nWORKERS = {'migration': 'dynamic', **EXTRA}\n"
        roots = json.loads((self.root / generator.ROOTS_PATH).read_bytes())
        roots["owners"][0]["worker_dispatch"] = {"source": "app.py", "symbol": "WORKERS", "reason": "unresolved dictionary unpack counterexample"}
        self.remediation_commit({"app.py": source, "hidden.py": b"VALUE = 'hidden'\n", generator.ROOTS_PATH: encode(roots)})
        with self.assertRaises(generator.ManifestError):
            self.production()

    def test_remediation_spec_child_import_requires_actual_child_digest(self) -> None:
        child = "build_helpers/hidden_helper.py"
        self.remediation_commit({"packaging/windows/LocalCAT.spec": b"from build_helpers import hidden_helper\n", "build_helpers/__init__.py": b"", child: b"VALUE = 'actual build child'\n"}, build_files=("build_helpers/__init__.py",))
        with self.assertRaises(generator.ManifestError):
            self.production()
        self.plan["build_sources"][child] = generator.fact((self.root / child).read_bytes())
        self.commit_plan()
        self.assertIn(child, self.production().manifest["build_inputs"]["repository_files"])

    def test_remediation_spec_dynamic_import_requires_actual_helper_digest(self) -> None:
        self.remediation_commit({"packaging/windows/LocalCAT.spec": b"import importlib\nhelper = importlib.import_module('hidden')\n", "hidden.py": b"VALUE = 'actual dynamic build helper'\n"})
        with self.assertRaises(generator.ManifestError):
            self.production()
        self.plan["build_sources"]["hidden.py"] = generator.fact((self.root / "hidden.py").read_bytes())
        self.commit_plan()
        self.assertIn("hidden.py", self.production().manifest["build_inputs"]["repository_files"])

    def test_remediation_spec_unknown_dynamic_target_rejected(self) -> None:
        self.remediation_commit({"packaging/windows/LocalCAT.spec": b"import importlib\ndef helper(name):\n    return importlib.import_module(name)\n"})
        with self.assertRaises(generator.ManifestError):
            self.production()

    def test_remediation_complete_relative_spec_helper_is_allowed(self) -> None:
        helper = "packaging/windows/relative_helper.py"
        self.remediation_commit({"packaging/windows/LocalCAT.spec": b"from . import relative_helper\n", helper: b"VALUE = 'relative build helper'\n"}, build_files=(helper,))
        self.assertIn(helper, self.production().manifest["build_inputs"]["repository_files"])

    def test_remediation_native_critical_input_and_bundle_alias_rejected(self) -> None:
        payload = b"MZsynthetic native input; never executed"
        path = "implementation.pyd"
        (self.bindings["native"] / path).write_bytes(payload)
        self.plan["input_trees"]["native"]["files"][path] = generator.fact(payload)
        self.plan["native"]["entries"]["native/" + path] = {"bundle_path": "app.pyd", "dependencies": []}
        self.commit_plan()
        with self.assertRaises(generator.ManifestError):
            self.production()

    def test_remediation_native_critical_loose_input_rejected(self) -> None:
        payload = b"MZsynthetic native input; never executed"
        (self.bindings["native"] / "app.pyd").write_bytes(payload)
        self.plan["input_trees"]["native"]["files"]["app.pyd"] = generator.fact(payload)
        self.plan["native"]["entries"]["native/app.pyd"] = {"bundle_path": "innocent.pyd", "dependencies": []}
        self.commit_plan()
        with self.assertRaises(generator.ManifestError):
            self.production()

    def test_remediation_collection_native_case_package_and_archive_duplicates_rejected(self) -> None:
        manifest = self.generate()
        members = {entry["path"]: (self.root / entry["path"]).read_bytes() for entry in manifest.manifest["entries"]}
        for path in ("app.pyd", "APP.cp314-win_amd64.pyd", "other/APP.py", "app/__init__.pyd", "other/App/__init__.cp314-win_amd64.pyd"):
            with self.subTest(path=path), self.assertRaises(generator.ManifestError):
                generator.validate_collection(manifest, {**members, path: b"MZcompeting source"}, pyz_modules=[])
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("other/APP/__init__.cp314-win_amd64.pyd", b"MZcompeting source")
        with self.assertRaises(generator.ManifestError):
            generator.validate_collection(manifest, {**members, "library.zip": stream.getvalue()}, pyz_modules=[])

    def test_remediation_normal_third_party_extension_stays_allowed(self) -> None:
        payload = b"MZsynthetic third party input; never executed"
        path = "fixturedep/_native.cp314-win_amd64.pyd"
        self.write("irrelevant.txt", b"unrelated input is not consumed")
        target = self.bindings["native"] / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        self.plan["input_trees"]["native"]["files"][path] = generator.fact(payload)
        self.plan["native"]["entries"]["native/" + path] = {"bundle_path": path, "dependencies": []}
        self.commit_plan()
        manifest = self.production()
        members = {entry["path"]: (self.root / entry["path"]).read_bytes() for entry in manifest.manifest["entries"]}
        generator.validate_collection(manifest, {**members, path: payload}, pyz_modules=["fixturedep"])


if __name__ == "__main__":
    unittest.main()
