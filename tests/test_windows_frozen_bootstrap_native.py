"""Diagnostic native builtin harness, not a frozen-entry gate."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest


class WindowsFrozenBootstrapNativeTests(unittest.TestCase):
    def test_handoff_implementation_exists(self):
        root = Path(__file__).resolve().parents[1]
        native = root / "packaging/windows/frozen-entry/native"
        self.assertTrue((native / "localcat_frozen_bootstrap.c").is_file(),
                        "compiled-in one-shot authority must be implemented")
        self.assertTrue((native / "localcat_frozen_bootstrap.h").is_file())
        lock = json.loads((root / "packaging/windows/frozen-entry/candidate-input.lock.json").read_text())
        declared = set(lock["entry_contract"]["python_c_api"]["function_symbols"])
        bound = set(re.findall(r"BIND\((Py\w+)\)", (native / "localcat_frozen_bootstrap.c").read_text()))
        self.assertGreater(len(bound), 10)
        self.assertFalse(bound - declared, bound - declared)

    @unittest.skipUnless(os.name == "nt", "requires real Win32 and PEP 741")
    def test_native_handoff_attacks(self):
        compiler = shutil.which("cl.exe")
        if compiler is None:
            self.skipTest("run in pinned MSVC x64 environment")
        root = Path(__file__).resolve().parents[1]
        native = root / "packaging/windows/frozen-entry/native"
        artifacts = root / "artifacts/windows"
        artifacts.mkdir(parents=True, exist_ok=True)
        lock = json.loads((root / "packaging/windows/frozen-entry/candidate-input.lock.json").read_text())
        runtime = Path(sys.base_prefix)
        self.assertEqual(hashlib.sha256((runtime / "python314.dll").read_bytes()).hexdigest(),
                         lock["runtime"]["cpython"]["dll"]["sha256"])
        with tempfile.TemporaryDirectory(prefix="handoff-", dir=artifacts) as temporary:
            work = Path(temporary)
            bundle = work / "bundle"
            internal = bundle / "_internal"
            internal.mkdir(parents=True)
            probe = work / "probe.c"
            probe.write_text('#include <windows.h>\nBOOL WINAPI DllMain(HINSTANCE a,DWORD b,LPVOID c){(void)a;(void)b;(void)c;return TRUE;}\n')
            self.run_command([compiler, "/nologo", "/W4", "/WX", "/MT", "/O2", "/LD", str(probe), "/Fe:" + str(internal / "probe.dll")], work)
            contents = [("probe.dll", "native-probe", 1, (internal / "probe.dll").read_bytes()),
                        ("fixture.txt", "fixture", 5, b"fixture\0bytes\n"),
                        ("critical.py", "critical-source", 4, b"answer = 42\n")]
            strings, records = bytearray(), bytearray()
            for filename, name, role, data in contents:
                (internal / filename).write_bytes(data)
                path, name = ("_internal\\" + filename).encode(), name.encode()
                path_offset = len(strings); strings.extend(path)
                name_offset = len(strings); strings.extend(name)
                records.extend(struct.pack("<8IQ32s", path_offset, len(path), name_offset, len(name), role, 0, 0, 0, len(data), hashlib.sha256(data).digest()))
            end = 80 + len(records)
            manifest = struct.pack("<8s10I32s", b"LCFMV001", 1, 80, len(contents), 72, 80, end, 0, end, len(strings), 0, bytes(32)) + records + strings
            (bundle / "localcat-runtime.manifest").write_bytes(manifest)
            for path in native.glob("*.h"):
                if path.name == "localcat_manifest.h":
                    header = path.read_text()
                    for key, value in {"CANDIDATE_INPUT_DIGEST": "0" * 64, "PRELINK_INPUT_DIGEST": "0" * 64,
                                       "RUNTIME_MANIFEST_DIGEST": ",".join(map(str, hashlib.sha256(manifest).digest())),
                                       "RUNTIME_ROOT_DIGEST": ",".join(["0"] * 32)}.items():
                        header = header.replace("@@" + key + "@@", value)
                    (work / path.name).write_text(header)
                else:
                    shutil.copyfile(path, work / path.name)
            sources = ["localcat_frozen_bootstrap.c", "localcat_rooted_io.c", "localcat_manifest.c", "localcat_sha256.c"]
            for name in sources:
                shutil.copyfile(native / name, work / name)
            exe = bundle / "handoff-test.exe"
            self.run_command([compiler, "/nologo", "/W4", "/WX", "/MT", "/O2", "/Brepro", "/guard:cf",
                              "/I" + str(work), "/I" + str(Path(sys.base_prefix) / "include"),
                              str(root / "tests/native/windows_frozen_bootstrap_test.c"),
                              *[str(work / name) for name in sources], "/Fe:" + str(exe),
                              "/link", "/Brepro", "/GUARD:CF", "user32.lib"], work)
            import pefile
            with pefile.PE(str(exe)) as pe:
                self.assertFalse(any(b"python" in row.dll.lower() for row in pe.DIRECTORY_ENTRY_IMPORT))
            script = work / "attacks.py"
            script.write_text(ATTACKS, encoding="utf-8")
            self.run_command([str(exe), sys.base_prefix, str(script), 'native-proof-failure'], work)
            self.run_command([str(exe), sys.base_prefix, str(script), 'normal'], work)
            script.write_text(DESTRUCTOR_ATTACKS, encoding="utf-8")
            self.run_command([str(exe), sys.base_prefix, str(script), 'normal'], work)
            script.write_text(WORKER_DESTRUCTOR_ATTACKS, encoding="utf-8")
            self.run_command([str(exe), sys.base_prefix, str(script), 'worker-destructor'], work)

    def run_command(self, command, cwd):
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if "background final reference:" in result.stdout:
            print(result.stdout.strip())


ATTACKS = r'''
import _localcat_frozen_bootstrap as b
import threading, copy, pickle
def denied(f, *args):
    try: f(*args)
    except (RuntimeError, TypeError, AttributeError): pass
    else: raise AssertionError('authority accepted forbidden operation')
errors = []
def wrong_take():
    try: denied(b.take_attestation)
    except BaseException as e: errors.append(e)
t = threading.Thread(target=wrong_take); t.start(); t.join(); assert not errors
a = b.take_attestation()
denied(b.take_attestation)
assert b.is_producer(object()) is False
denied(a.bind_producer, object())  # source/harness take is not the native bootstrap execution
denied(type(a)); denied(object.__new__, type(a))
denied(type, 'Forged', (type(a),), {})
denied(setattr, a, 'forged', True)
denied(setattr, type(a), 'forged', True)
denied(copy.copy, a); denied(copy.deepcopy, a); denied(pickle.dumps, a)
assert a.read_verified('fixture') == b'fixture\0bytes\n'
assert a.read_verified('critical-source') == b'answer = 42\n'
denied(a.read_verified, 'fixture\0suffix'); denied(a.read_verified, '../fixture'); denied(a.read_verified, 1)
p = a.module_reproof('native-probe')
assert type(p) is tuple and p[0] is True and p[1] == 1
assert p[6:9] == (bytes([1])*32, bytes([2])*32, bytes([3])*32)
assert p[9] == __import__('hashlib').sha256(bytes([5])*32 + b'E10.HANDOFF_TAKEN\0').digest()
assert a.module_reproof('critical-source')[0] is False
def wrong_read():
    try:
        denied(a.read_verified, 'fixture'); denied(a.module_reproof, 'native-probe'); denied(a.close)
    except BaseException as e: errors.append(e)
t = threading.Thread(target=wrong_read); t.start(); t.join(); assert not errors
import _interpreters
other = _interpreters.create('legacy')
try:
    result = _interpreters.run_string(other, "try:\n import _localcat_frozen_bootstrap as b\n b.take_attestation()\nexcept (RuntimeError, ImportError):\n pass\nelse:\n raise AssertionError('cross-interpreter authority')\n")
    assert result is None, result
finally: _interpreters.destroy(other)
held = a.read_verified
a.close()
assert b.is_producer(object()) is False
denied(a.close); denied(held, 'fixture'); denied(a.module_reproof, 'native-probe'); denied(b.take_attestation)
'''

DESTRUCTOR_ATTACKS = r'''
import _localcat_frozen_bootstrap as b
import gc
a = b.take_attestation()
assert a.read_verified('fixture') == b'fixture\0bytes\n'
del a
gc.collect()
try: b.take_attestation()
except RuntimeError: pass
else: raise AssertionError('destructor allowed authority remint')
'''

WORKER_DESTRUCTOR_ATTACKS = r'''
import _localcat_frozen_bootstrap as b
import gc, threading
holder = [b.take_attestation()]
def release():
    holder.clear()
    gc.collect()
t = threading.Thread(target=release); t.start(); t.join()
try: b.take_attestation()
except RuntimeError: pass
else: raise AssertionError('worker destructor allowed authority remint')
assert b.is_producer(object()) is False
'''


if __name__ == "__main__":
    unittest.main()
