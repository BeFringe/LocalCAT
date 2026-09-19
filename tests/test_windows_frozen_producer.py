"""Producer boundary tests; native acceptance is a separate realized-EXE gate."""
from __future__ import annotations

from importlib.machinery import BuiltinImporter, ModuleSpec
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType
from typing import Any, cast
import unittest
from unittest import mock

import tm_gate_inputs


ROOT = Path(__file__).resolve().parents[1]


class ProducerAdmissionBoundaryTests(unittest.TestCase):
    def test_python_module_builtin_spec_and_callback_do_not_attest_producer(self):
        native = ModuleType("_localcat_frozen_bootstrap")
        native.__spec__ = ModuleSpec(native.__name__, cast(Any, BuiltinImporter), origin="built-in")
        callback = mock.Mock(return_value=True)
        setattr(native, "is_producer", callback)
        with mock.patch.dict(sys.modules, {native.__name__: native}), mock.patch.object(sys, "frozen", True, create=True):
            with self.assertRaisesRegex(RuntimeError, "native E10"):
                tm_gate_inputs._compose_frozen_input_owner(object())
        callback.assert_not_called()

    def test_foreign_builtin_function_cannot_replace_native_identity_query(self):
        native = ModuleType("_localcat_frozen_bootstrap")
        native.__spec__ = ModuleSpec(native.__name__, cast(Any, BuiltinImporter), origin="built-in")
        setattr(native, "is_producer", len)
        with mock.patch.dict(sys.modules, {native.__name__: native}):
            with self.assertRaisesRegex(RuntimeError, "native E10"):
                tm_gate_inputs._compose_frozen_input_owner({"key": object()})

    def test_source_import_cannot_run_the_native_bootstrap(self):
        result = subprocess.run([sys.executable, "-B", "-c", "import frozen_source_bootstrap"],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("_localcat_frozen_bootstrap", result.stderr)


@unittest.skipUnless(os.name == "nt", "native Windows publication contract")
class DefaultResourceInitializationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "artifacts/windows/task72").mkdir(parents=True, exist_ok=True)

    def test_publication_readback_and_terminal_failures_never_report_seed_success(self):
        from frozen_product_entry import _seed_default_resources
        from platform_fs import compose_platform_file_backend
        from platform_fs_contracts import BoundDirectoryAuthority, PendingPublication
        from platform_fs_windows import _WindowsBoundRegularFile

        faults: tuple[tuple[type, str, dict[str, Any]], ...] = ((BoundDirectoryAuthority, "begin_publish", {"side_effect": PermissionError("publication denied")}),
                  (_WindowsBoundRegularFile, "read_all", {"return_value": b"different bytes"}),
                  (PendingPublication, "terminal_reproof", {"side_effect": RuntimeError("terminal revoked")}))
        for owner, method, effect in faults:
            with self.subTest(method=method), tempfile.TemporaryDirectory(
                    dir=ROOT / "artifacts/windows/task72", prefix="default-fault-") as temporary:
                directory = Path(temporary)
                backend = compose_platform_file_backend(directory)
                with mock.patch.object(owner, method, **effect) as injected:
                    with self.assertRaises((RuntimeError, PermissionError)):
                        _seed_default_resources(backend, directory, {"tm.jsonl": b"retained default"})
                    injected.assert_called_once()
                if (directory / "tm.jsonl").exists():
                    self.assertEqual((directory / "tm.jsonl").read_bytes(), b"retained default")
                self.assertFalse(list(directory.glob(".localcat-default-*.tmp")))

    def test_retained_default_payload_is_published_once_without_overwrite(self):
        from frozen_product_entry import _seed_default_resources
        from platform_fs import compose_platform_file_backend

        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts/windows/task72", prefix="default-assets-") as temporary:
            directory = Path(temporary)
            backend = compose_platform_file_backend(directory)
            first = {"tm.jsonl": b'{"source":"first"}\n', "terms.csv": b"source,target\n"}
            _seed_default_resources(backend, directory, first)
            self.assertEqual({name: (directory / name).read_bytes() for name in first}, first)
            _seed_default_resources(backend, directory, {name: b"must not overwrite" for name in first})
            self.assertEqual({name: (directory / name).read_bytes() for name in first}, first)
            self.assertFalse(list(directory.glob(".localcat-default-*.tmp")))

    def test_two_fresh_processes_never_replace_the_winning_default(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts/windows/task72", prefix="default-race-") as temporary:
            directory = Path(temporary)
            code = ("from pathlib import Path; import sys; "
                    "from platform_fs import compose_platform_file_backend; "
                    "from frozen_product_entry import _seed_default_resources; "
                    "p=Path(sys.argv[1]); _seed_default_resources(compose_platform_file_backend(p),p,"
                    "{'tm.jsonl':sys.argv[2].encode()})")
            processes = [subprocess.Popen([sys.executable, "-B", "-c", code, str(directory), payload],
                                           cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                         for payload in ("first", "second")]
            outputs = [process.communicate(timeout=30) for process in processes]
            self.assertTrue(any(process.returncode == 0 for process in processes), outputs)
            self.assertIn((directory / "tm.jsonl").read_bytes(), (b"first", b"second"))
            self.assertFalse(list(directory.glob(".localcat-default-*.tmp")))


if __name__ == "__main__":
    unittest.main()
