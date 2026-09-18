"""Clean-build orchestration boundaries; not W3 runtime acceptance."""
import argparse
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import build_windows_frozen_spike as build
from tools.windows_frozen_packaging import APPLICATION_MANIFEST, fact
from tools.windows_frozen_release import ReleaseBindingError


class SpikeBuildTests(unittest.TestCase):
    def test_vcvars_environment_uses_case_insensitive_windows_names_and_restores_parent(self):
        parent = {"VCTOOLSINSTALLDIR": "compiler", "VCTOOLSVERSION": "v1",
                  "WINDOWSSDKDIR": "sdk", "WINDOWSSDKVERSION": "v2", "UCRTVERSION": "v2",
                  "UNIVERSALCRTSDKDIR": "sdk", "SYSTEMROOT": "system", "MIMALLOC_SHOW_STATS": "1",
                  "CXX": "ambient.exe", "LINK_CXX": "ambient.exe", "AR": "ambient.exe",
                  "MT": "ambient.exe", "WINRC": "ambient.exe", "CPPFLAGS": "/Iambient"}
        lock = {"toolchain": {"msvc": {"toolset_version": "v1"}, "windows_sdk": {"version": "v2"}}}

        def finalize(environment, *_):
            self.assertEqual(environment["VCToolsInstallDir"], "compiler")
            self.assertEqual(environment["UniversalCRTSdkDir"], "sdk")
            return environment, {}

        with mock.patch.dict(os.environ, parent, clear=True), \
                mock.patch.object(build, "finalize_compile_environment", side_effect=finalize), \
                mock.patch.object(build.shutil, "which", side_effect=["compiler/bin/Hostx64/x64/cl.exe", "sdk/bin/v2/x64/rc.exe"]):
            original = dict(os.environ)
            with build.compile_environment(lock):
                self.assertNotIn("MIMALLOC_SHOW_STATS", os.environ)
                for name in ("CXX", "LINK_CXX", "AR", "MT", "WINRC", "CPPFLAGS"):
                    self.assertNotIn(name, os.environ)
            self.assertEqual(dict(os.environ), original)

    def test_dirty_source_stops_before_probe_or_output_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "not-created"
            args = argparse.Namespace(output=output, expected_commit="a" * 40,
                                      expected_candidate_lock_sha256="b" * 64)
            with mock.patch.object(build, "clean_source_context", side_effect=ReleaseBindingError("dirty")), \
                    mock.patch.object(build.probe, "run_probe") as run:
                with self.assertRaises(ReleaseBindingError):
                    build.build_spike(args)
            self.assertFalse(output.exists())
            run.assert_not_called()

    def test_pe_check_uses_actual_manifest_imports_debug_and_exact_empty_archive(self):
        actual = {"subsystem": 2, "timestamp": 0, "security_directory_size": 0,
                  "debug_directory_types": [16], "imports": [], "delay_imports": []}
        expected = {"imports": [], "delay_imports": []}
        overlay = build.empty_carchive("3.14.7")
        build.check_pe_contract(actual, [APPLICATION_MANIFEST], overlay, expected, "3.14.7")
        mutations = [("subsystem", 3), ("timestamp", 1), ("security_directory_size", 8),
                     ("debug_directory_types", [2, 16]),
                     ("imports", [{"dll": "unapproved.dll", "symbols": ["bad"]}]),
                     ("delay_imports", [{"dll": "unapproved.dll", "symbols": ["bad"]}])]
        for key, value in mutations:
            changed = copy.deepcopy(actual)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ReleaseBindingError):
                build.check_pe_contract(changed, [APPLICATION_MANIFEST], overlay, expected, "3.14.7")
        for manifests, data in (([], overlay), ([APPLICATION_MANIFEST, APPLICATION_MANIFEST], overlay),
                                ([b"different"], overlay), ([APPLICATION_MANIFEST], overlay + b"hidden"),
                                ([APPLICATION_MANIFEST], b"hidden" + overlay)):
            with self.subTest(manifests=manifests), self.assertRaises(ReleaseBindingError):
                build.check_pe_contract(actual, manifests, data, expected, "3.14.7")

    def test_build_files_checked_as_bytes_including_generated_header(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            expected = {"prelink.json": b"prelink", "generated/header.h": b"header"}
            for name, data in expected.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            self.assertEqual(build.verify_files(root, expected), {name: fact(data) for name, data in expected.items()})
            (root / "generated/header.h").write_bytes(b"replaced")
            with self.assertRaises(ReleaseBindingError):
                build.verify_files(root, expected)

    def test_effective_waf_cache_and_commands_must_match_locked_roots(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bootloader = root / "pyinstaller/bootloader"
            cache_file = bootloader / "build/c4che/releasew_cache.py"
            cache_file.parent.mkdir(parents=True)
            tools = {role: {"path": str(root / "tools" / (role + ".exe"))}
                     for role in ("cl", "link", "rc", "mt")}
            include, libraries = [str(root / "include")], [str(root / "lib")]
            projection = {"include_roots": [{"path": include[0]}], "lib_roots": [{"path": libraries[0]}]}
            cache = {key: [tools[role]["path"]] for key, role in (
                ("CC", "cl"), ("CXX", "cl"), ("LINK_CC", "link"), ("LINK_CXX", "link"), ("WINRC", "rc"), ("MT", "mt"))}
            cache.update(NO_MSVC_DETECT=True, INCLUDES=include, LIBPATH=libraries)
            runtime = root / "python"
            commands = []
            for name in ("main", *build.probe.MODULES):
                commands.append([tools["cl"]["path"], "/MT", "/Brepro", "/guard:cf", "/WX",
                                 "/Isrc", "/I../../src", "/I" + str(runtime / "include"),
                                 "/I" + str(root / "generated"), "/I" + include[0],
                                 "../../src/" + name + ".c"])
            commands.append([tools["link"]["path"], "/Brepro", "/LIBPATH:" + libraries[0]])

            def write(current_cache, current_commands):
                cache_file.write_text("".join(key + " = " + repr(value) + "\n" for key, value in current_cache.items()), encoding="utf-8")
                (root / "waf.stdout").write_text("Waf: Entering directory fixture\n" +
                                                "".join("runner " + repr(command) + "\n" for command in current_commands), encoding="utf-8")

            write(cache, commands)
            build.verify_waf_inputs(root, {"tools": tools}, projection, runtime)
            for key, value in (("CC", ["foreign.exe"]), ("INCLUDES", include + ["ambient"]),
                               ("LIBPATH", libraries + ["ambient"]), ("NO_MSVC_DETECT", False)):
                write({**cache, key: value}, commands)
                with self.subTest(key=key), self.assertRaises(ReleaseBindingError):
                    build.verify_waf_inputs(root, {"tools": tools}, projection, runtime)
            changed = copy.deepcopy(commands)
            changed[0].append("/Iambient")
            for current in (changed, commands[:-1], commands + [commands[0]]):
                write(cache, current)
                with self.assertRaises(ReleaseBindingError):
                    build.verify_waf_inputs(root, {"tools": tools}, projection, runtime)


if __name__ == "__main__":
    unittest.main()
