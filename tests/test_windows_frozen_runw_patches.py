"""Actual runw patch application and build routing, not E0/entry execution proof."""
import ast
import io
import json
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.audit_w3_stock_bootloader import audit_sources
from tools.prepare_windows_frozen_entry_inputs import _directory_input_fact
from tools.render_windows_frozen_native_patches import NATIVE_PATCH_MEMBERS, render_patch_set
from tools.windows_frozen_patch_series import Patch, replay_patch_series, source_inventory_digest


ROOT = Path(__file__).resolve().parents[1]
PATCHES = ROOT / "packaging/windows/frozen-entry/patches"
PRISTINE = Path(os.environ.get("LOCALCAT_W3_PRISTINE", str(
    ROOT.parent / "w3-sources/pyinstaller-6.22.2")))


class Env(SimpleNamespace):
    def append_value(self, key, value):
        old = getattr(self, key, [])
        setattr(self, key, old + (value if isinstance(value, list) else [value]))


class Context:
    def __init__(self):
        self.env = Env(DEST_OS="win32", CC_NAME="msvc", PYI_ARCH="64bit",
                       PYI_MACHINE="intel", LOCALCAT_PYTHON_INCLUDE="python",
                       LOCALCAT_GENERATED_INCLUDE="generated", PYI_SYSTEM="Windows")
        self.variant = "releasew"
        self.calls = []

    def fatal(self, message):
        raise ValueError(message)

    def program(self, **kwargs):
        self.calls.append(kwargs)

    def is_musl(self):
        return False


class NativePatchRenderingTests(unittest.TestCase):
    def test_windows_git_checkout_keeps_patch_bytes_exact(self):
        if not shutil.which("git"):
            self.skipTest("git required for checkout byte verification")
        relative = "packaging/windows/frozen-entry/patches/entry-policy.patch"
        content = (ROOT / relative).read_bytes()
        with tempfile.TemporaryDirectory(prefix="native-patch-checkout-") as temporary:
            root = Path(temporary)
            (root / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
            (root / relative).parent.mkdir(parents=True)
            (root / relative).write_bytes(content)
            commands = (["init", "--quiet"],
                        ["-c", "core.autocrlf=true", "add", "--", ".gitattributes", relative],
                        ["-c", "core.autocrlf=true", "checkout-index", "--all", "--prefix=checkout/"],
                        ["diff", "--cached", "--check"])
            for command in commands:
                result = subprocess.run(["git", *command], cwd=root, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode() + result.stdout.decode())
            self.assertEqual((root / "checkout" / relative).read_bytes(), content)

    def test_apply_patch_request_preserves_unicode_non_lf_separators(self):
        from tools.render_windows_frozen_native_patches import main
        content = "// left\u2028middle\x85right\n".encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="native-patch-render-") as temporary:
            root = Path(temporary)
            native = root / "packaging/windows/frozen-entry/native"
            patches = native.parent / "patches"
            native.mkdir(parents=True)
            patches.mkdir()
            for members in NATIVE_PATCH_MEMBERS.values():
                for name in members:
                    (native / name).write_bytes(content)
            (patches / "entry-policy.patch").write_bytes((PATCHES / "entry-policy.patch").read_bytes())
            output = io.BytesIO()
            with mock.patch("tools.render_windows_frozen_native_patches.sys.stdout", SimpleNamespace(buffer=output)):
                self.assertEqual(main(["--repository", str(root)]), 0)
            self.assertTrue(b"++" + content in output.getvalue(), "non-LF source separators changed")

    def test_native_patch_regeneration_is_exact_and_idempotent(self):
        native = ROOT / "packaging/windows/frozen-entry/native"
        sources = {name: (native / name).read_bytes()
                   for members in NATIVE_PATCH_MEMBERS.values() for name in members}
        rendered = render_patch_set(sources, (PATCHES / "entry-policy.patch").read_bytes())
        self.assertEqual(rendered, render_patch_set(sources, rendered["entry-policy"]))
        for patch_id, content in rendered.items():
            with self.subTest(patch=patch_id):
                self.assertEqual(content, (PATCHES / (patch_id + ".patch")).read_bytes())

    def test_renderer_refuses_unrepresentable_source_bytes_or_incomplete_inventory(self):
        sources = {name: b"exact source\n" for members in NATIVE_PATCH_MEMBERS.values() for name in members}
        hook = (PATCHES / "entry-policy.patch").read_bytes()
        for invalid in (b"", b"missing final newline", b"CRLF\r\n", b"NUL\0\n", b"\xff\n"):
            with self.subTest(content=invalid), self.assertRaises((ValueError, UnicodeError)):
                render_patch_set({**sources, "localcat_manifest.h": invalid}, hook)
        with self.assertRaises(ValueError):
            render_patch_set({}, hook)
        with self.assertRaises(ValueError):
            render_patch_set(sources, b"not an entry policy\n")


class RunwPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (PRISTINE / "bootloader/src/main.c").is_file():
            raise unittest.SkipTest("provide locked pristine source via LOCALCAT_W3_PRISTINE")
        lock = json.loads((ROOT / "packaging/windows/frozen-entry/candidate-input.lock.json").read_text())
        source = lock["runtime"]["pyinstaller"]
        audit = audit_sources(PRISTINE)
        if not audit["pinned_hashes_match"] or audit["source_scan"] != source["source_scan"]:
            raise AssertionError("pristine upstream source mismatch")
        if _directory_input_fact(PRISTINE / "bootloader", source["build_driver"]["role"]) != source["build_driver"]:
            raise AssertionError("pristine full bootloader inventory mismatch")
        cls.pristine_files = {path.relative_to(PRISTINE).as_posix(): path.read_bytes()
                              for path in (PRISTINE / "bootloader").rglob("*") if path.is_file()}
        cls.contract = lock["entry_contract"]["patch"]
        cls.patches = [Patch(item["id"], (PATCHES / (item["id"] + ".patch")).read_bytes())
                       for item in cls.contract["series"]]
        cls.patch_bytes = [patch.content for patch in cls.patches]
        cls.replayed = replay_patch_series(cls.pristine_files, cls.patches,
            patch_contract=cls.contract, expected_base_digest=source_inventory_digest(cls.pristine_files))
        cls.files = cls.replayed.files
        tree = ast.parse(cls.files["bootloader/wscript"])
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"_localcat_build", "_localcat_configure", "_localcat_msvc_inputs", "build"}]
        cls.namespace = {"os": os, "variants": {"releasew": "runw", "release": "run"}}
        exec(compile(ast.Module(body=functions, type_ignores=[]), "wscript", "exec"), cls.namespace)

    def test_git_apply_matches_strict_replayer(self):
        if not shutil.which("git"):
            self.skipTest("git required for independent patch application")
        with tempfile.TemporaryDirectory(prefix="localcat-runw-patches-") as temporary:
            directory = Path(temporary)
            for name, content in self.pristine_files.items():
                destination = directory / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            for data in self.patch_bytes:
                result = subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "--check", "-"], cwd=directory,
                                        input=data, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                result = subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "-"], cwd=directory,
                                        input=data, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
            for name, expected in self.files.items():
                self.assertEqual((directory / name).read_bytes(), expected)

    def test_full_series_adds_exact_native_templates_without_post_replay_copy(self):
        self.assertEqual([patch.id for patch in self.patches], ["entry-policy", "manifest-and-hash",
            "native-closure", "bootstrap-handoff", "reproducible-build"])
        self.assertEqual(len(self.contract["owned_new_files"]), 12)
        native = ROOT / "packaging/windows/frozen-entry/native"
        for name in self.contract["owned_new_files"]:
            with self.subTest(path=name):
                self.assertNotIn(name, self.pristine_files)
                self.assertEqual(self.files[name], (native / Path(name).name).read_bytes())
        provenance = json.loads(self.replayed.provenance_bytes)
        expected = {
            "entry-policy": {"main.c", "localcat_frozen_entry.c", "localcat_frozen_entry.h"},
            "manifest-and-hash": {"localcat_manifest.c", "localcat_manifest.h", "localcat_sha256.c", "localcat_sha256.h"},
            "native-closure": {"localcat_rooted_io.c", "localcat_rooted_io.h", "localcat_native_closure.c", "localcat_native_closure.h"},
            "bootstrap-handoff": {"localcat_frozen_bootstrap.c", "localcat_frozen_bootstrap.h"},
            "reproducible-build": {"wscript"},
        }
        self.assertEqual({item["id"]: {Path(path).name for path in item["paths"]}
                          for item in provenance["patches"]}, expected)

    def test_windowed_entry_has_no_stock_launch_route(self):
        text = self.files["bootloader/src/main.c"].decode()
        body = text.split("wWinMain(", 1)[1].split("#else /* defined(WINDOWED) */", 1)[0]
        self.assertIn("return localcat_frozen_entry();", body)
        self.assertNotIn("pyi_main(", body)
        self.assertNotIn("global_pyi_ctx", body)
        self.assertIn('#error "LocalCAT frozen builds require the windowed entry"', text)

    def test_msvc_configuration_disables_rediscovery_and_drops_ambient_search_roots(self):
        context = Context()
        context.options = SimpleNamespace(clang=False, gcc=False)
        context.environ = {"INCLUDE": "ambient", "LIB": "ambient", "LIBPATH": "ambient"}
        with tempfile.TemporaryDirectory() as folder:
            msvc, sdk = str(Path(folder) / "msvc"), str(Path(folder) / "sdk")
            with mock.patch.dict(os.environ, {"VCToolsInstallDir": msvc, "WindowsSdkDir": sdk,
                                               "WindowsSDKVersion": "10.0.26100.0\\",
                                               "VisualStudioVersion": "17.0", "LIBPATH": "ambient"}), \
                    mock.patch.object(os.path, "isdir", return_value=True):
                self.namespace["_localcat_msvc_inputs"](context)
                self.assertTrue(context.env.NO_MSVC_DETECT)
                self.assertEqual(context.env.MSVC_COMPILER, "msvc")
                self.assertEqual(context.env.INCLUDES, [])
                self.assertEqual(context.env.LIBPATH, [])
                self.assertEqual(len(context.env.PATH), 2)
                self.assertEqual(len(context.environ["INCLUDE"].split(os.pathsep)), 6)
                self.assertEqual(len(context.environ["LIB"].split(os.pathsep)), 3)
                self.assertEqual(context.environ["LIBPATH"], "")
                self.assertNotIn("LIBPATH", os.environ)
                self.assertNotIn("ambient", repr(context.environ))
        text = self.files["bootloader/wscript"].decode()
        configure = text.split("def configure(ctx):", 1)[1].split("global is_cross", 1)[0]
        self.assertLess(configure.index("_localcat_msvc_inputs(ctx)"), configure.index("ctx.load('msvc')"))

    def test_build_selects_only_explicit_native_sources_and_no_python_import_lib(self):
        context = Context()
        self.assertTrue(self.namespace["_localcat_build"](context, "runw", "install"))
        self.assertEqual(len(context.calls), 1)
        call = context.calls[0]
        self.assertEqual(call["source"], ["src/main.c"] + ["src/" + name + ".c" for name in (
            "localcat_frozen_entry", "localcat_frozen_bootstrap", "localcat_manifest",
            "localcat_native_closure", "localcat_rooted_io", "localcat_sha256")])
        self.assertEqual(call["use"], "USER32 KERNEL32 ADVAPI32 GDI32")
        self.assertIn("Py_NO_LINK_LIB", context.env.DEFINES)
        self.assertIn("_WIN32_WINNT=0x0A00", context.env.DEFINES)
        self.assertIn("NTDDI_VERSION=0x0A000000", context.env.DEFINES)
        for flag in ("/MT", "/O2", "/Brepro", "/guard:cf", "/WX"):
            self.assertIn(flag, context.env.CFLAGS)
        self.assertIn("/Brepro", context.env.LINKFLAGS)
        self.assertIn("/NOCOFFGRPINFO", context.env.LINKFLAGS)
        self.assertNotIn("/DEBUG", context.env.LINKFLAGS)
        self.assertEqual(call["includes"], ["src", "python", "generated"])

    def test_non_release_or_non_locked_architecture_never_uses_stock_fallback(self):
        for field, value in (("variant", "release"), ("variant", "debugw"),
                             ("CC_NAME", "gcc"), ("PYI_ARCH", "32bit"),
                             ("PYI_MACHINE", "arm")):
            context = Context()
            setattr(context if field == "variant" else context.env, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.namespace["_localcat_build"](context, "runw", "install")
            self.assertFalse(context.calls)

    def test_actual_waf_build_returns_before_stock_objects_and_archive_sources(self):
        context = Context()
        # No objects(), recurse() or path.ant_glob() exists on this context;
        # reaching the stock branch would fail, even after custom program creation.
        self.namespace["build"](context)
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(context.calls[0]["target"], "runw")
        context.variant = "release"
        with self.assertRaises(ValueError):
            self.namespace["build"](context)
        self.assertEqual(len(context.calls), 1)

    def test_configure_requires_explicit_existing_include_directories(self):
        with tempfile.TemporaryDirectory(prefix="localcat-runw-includes-") as temporary:
            context = Context()
            context.options = SimpleNamespace(debug=False, enable_cfg=True,
                localcat_python_include=temporary, localcat_generated_include=temporary)
            self.namespace["_localcat_configure"](context)
            self.assertEqual(context.env.LOCALCAT_PYTHON_INCLUDE, temporary)
            for key, value in (("debug", True), ("enable_cfg", False),
                               ("localcat_python_include", None),
                               ("localcat_generated_include", "relative")):
                original = getattr(context.options, key)
                setattr(context.options, key, value)
                with self.subTest(key=key), self.assertRaises(ValueError):
                    self.namespace["_localcat_configure"](context)
                setattr(context.options, key, original)


if __name__ == "__main__":
    unittest.main()
