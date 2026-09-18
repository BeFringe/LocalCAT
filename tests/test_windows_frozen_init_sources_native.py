"""Actual isolated initialization with retained stdlib bytes, no path fallback."""
from pathlib import Path
from contextlib import nullcontext
import hashlib
import importlib.machinery
import importlib._bootstrap_external
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools.windows_frozen_manifest import SourceEntry, prepare_manifest


class RetainedInitializationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows CPython 3.14 native diagnostic')
    def test_retained_native_bootstrap_and_critical_source(self):
        self.run_initialization(full=True)

    @unittest.skipUnless(os.name == 'nt', 'Windows CPython 3.14 native diagnostic')
    def test_no_disk_search_during_initialization(self):
        self.run_initialization(full=False)

    @unittest.skipUnless(os.name == 'nt', 'Windows CPython 3.14 native diagnostic')
    def test_retained_external_failures_revoke_authority(self):
        for fault in ('syntax', 'execution', 'install'):
            with self.subTest(fault=fault):
                self.run_initialization(full=True, fault=fault)

    @unittest.skipUnless(os.name == 'nt', 'Windows CPython 3.14 native diagnostic')
    def test_reject_multi_item_path_layout(self):
        self.run_initialization(full=True, layout='bundle;extra')

    @unittest.skipUnless(os.name == 'nt', 'Windows CPython 3.14 native diagnostic')
    def test_early_clear_failure_is_terminal(self):
        self.run_initialization(full=True, native_fault='clear')

    @unittest.skipUnless(os.name == 'nt', 'Windows CPython 3.14 native diagnostic')
    def test_real_path_allocation_failure_is_fatal(self):
        self.run_initialization(full=True, native_fault='oom')

    @unittest.skipUnless(os.name == 'nt', 'Windows CPython 3.14 native diagnostic')
    def test_install_failure_does_not_execute_added_traceback(self):
        self.run_initialization(full=True, fault='install', native_fault='install')

    def run_initialization(self, *, full, fault=None, layout='bundle', native_fault=None):
        compiler = shutil.which('cl.exe')
        if compiler is None:
            self.skipTest('run in the pinned MSVC x64 environment')
        root = Path(__file__).resolve().parents[1]
        native = root / 'packaging/windows/frozen-entry/native'
        runtime = Path(sys.base_prefix)
        lock = json.loads((root / 'packaging/windows/frozen-entry/candidate-input.lock.json').read_text())
        self.assertEqual(hashlib.sha256((runtime / 'python314.dll').read_bytes()).hexdigest(),
                         lock['runtime']['cpython']['dll']['sha256'])
        entries = [SourceEntry(name, '_internal/encodings/' + file, 'interpreter',
                               (runtime / 'Lib/encodings' / file).read_bytes())
                   for name, file in [('encoding-package','__init__.py'), ('encoding-aliases','aliases.py'),
                                      ('encoding-utf8','utf_8.py'), ('encoding-win','_win_cp_codecs.py')]]
        external = (runtime / 'Lib/importlib/_bootstrap_external.py').read_bytes()
        for fact in lock['runtime']['cpython']['interpreter_sources']:
            data = (runtime / fact['path']).read_bytes()
            self.assertEqual((len(data),hashlib.sha256(data).hexdigest()),(fact['bytes'],fact['sha256']))
        if fault:
            # Each fault is a separate, freshly bound test input, never a
            # replacement for the pinned runtime's production source facts.
            if fault == 'syntax':
                external += b'\ninvalid syntax for retained fixture\n'
            elif fault == 'execution':
                external += (b"\nimport sys\nassert sys.path == []\n"
                             b"raise RuntimeError('RETAINED_EXECUTION_FAULT')\n")
            elif fault == 'install':
                external += (b"\n_original_install = _install\n"
                             b"def _install(bootstrap):\n"
                             b"    _original_install(bootstrap)\n"
                             b"    import sys\n    assert sys.path == []\n"
                             b"    raise RuntimeError('RETAINED_INSTALL_FAULT')\n")
        entries.append(SourceEntry('importlib-external','_internal/importlib/_bootstrap_external.py',
                                   'interpreter',external))
        if full:
            spike = root / 'packaging/windows/frozen-entry/spike'
            entries.extend([
                SourceEntry('vcruntime-runtime','_internal/vcruntime140.dll','native',(runtime / 'vcruntime140.dll').read_bytes()),
                SourceEntry('python-runtime','_internal/python314.dll','native',(runtime / 'python314.dll').read_bytes(),('vcruntime-runtime',)),
                SourceEntry('bootstrap','_internal/localcat_frozen_bootstrap.py','bootstrap',(spike / 'localcat_frozen_bootstrap.py').read_bytes()),
                SourceEntry('critical-source','_internal/localcat_spike_critical.py','critical-source',(spike / 'localcat_spike_critical.py').read_bytes()),
                SourceEntry('fixture','_internal/localcat_spike_fixture.txt','fixture',(spike / 'localcat_spike_fixture.txt').read_bytes()),
                SourceEntry('exe-path-config','retained-init._pth','fixture',b'../ambient\nimport site\n'),
                SourceEntry('dll-path-config','_internal/python314._pth','fixture',b'../../ambient\nimport site\n'),
            ])
        prepared = prepare_manifest(entries, candidate_input_digest=lock['candidate_input_digest'],
                                    applied_sources_digest='1'*64)
        artifacts = root / 'artifacts/windows'
        artifacts.mkdir(parents=True, exist_ok=True)
        directory = (nullcontext(tempfile.mkdtemp(prefix='retained-init-fault-',dir=artifacts))
                     if native_fault else tempfile.TemporaryDirectory(prefix='retained-init-',dir=artifacts))
        with directory as temporary:
            work = Path(temporary)
            if native_fault:
                print('RETAINED_INIT_FAULT_ARTIFACT=' + str(work),flush=True)
            bundle = work / layout
            bundle.mkdir()
            for entry in entries:
                path = bundle / entry.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(entry.content)
            (bundle / 'localcat-runtime.manifest').write_bytes(prepared.runtime_bytes)
            for path in native.glob('*.h'):
                if path.name == 'localcat_manifest.h':
                    (work / path.name).write_text(prepared.render_header(path.read_text()))
                else:
                    shutil.copyfile(path, work / path.name)
            sources = ['localcat_frozen_bootstrap.c','localcat_rooted_io.c','localcat_manifest.c','localcat_sha256.c']
            for name in sources:
                shutil.copyfile(native / name, work / name)
            if native_fault:
                sources.remove('localcat_frozen_bootstrap.c')
            exe = bundle / ('retained_init_fault.exe' if native_fault else 'retained-init.exe')
            build = [compiler,'/nologo','/W4','/WX','/MT','/O2','/Brepro','/guard:cf',
                     '/I'+str(work),'/I'+str(runtime / 'include'),
                     str(root / 'tests/native/windows_frozen_init_sources_test.c'),
                     *[str(work / name) for name in sources], '/Fe:'+str(exe),
                     '/link','/Brepro','/GUARD:CF','user32.lib']
            if native_fault:
                build.insert(1,'/DLOCALCAT_TEST_INITIALIZATION_FAULTS')
                build.append('/IMPLIB:' + str(work / 'fault-probe.lib'))
            result = subprocess.run(build,cwd=work,capture_output=True,text=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            fault_evidence = {
                'scope': 'retained initialization component fault diagnostic; not E0-E11',
                'candidate_input_digest': lock['candidate_input_digest'],
                'runtime_manifest_sha256': hashlib.sha256(prepared.runtime_bytes).hexdigest(),
                'executable_sha256': hashlib.sha256(exe.read_bytes()).hexdigest(),
                'build_command': build, 'build_stdout': result.stdout, 'build_stderr': result.stderr,
                'sources': {name: hashlib.sha256((work / name).read_bytes()).hexdigest()
                            for name in ['localcat_frozen_bootstrap.c',*sources]},
                'fault_driver_sha256': hashlib.sha256((root / 'tests/native/windows_frozen_init_faults.h').read_bytes()).hexdigest()
                                      if native_fault else None,
                'runs': [],
            }
            source = bundle / '_internal/encodings/utf_8.py'
            payload = compile("raise RuntimeError('INJECTED_PYC_EXECUTED')", str(source), 'exec')
            poisoned = work / 'poison.pyc'
            poisoned.write_bytes(importlib._bootstrap_external._code_to_timestamp_pyc(
                payload, int(source.stat().st_mtime), source.stat().st_size))
            cache_dir = source.parent / '__pycache__'
            startup = work / 'ambient-startup.py'
            startup.write_text("raise RuntimeError('AMBIENT_STARTUP_EXECUTED')\n")
            modes = (('full','full-polluted','full-launcher','full-empty-launcher','full-parent-config','full-early','full-badpath','full-badflags',
                      'full-no-stage-sink','full-null-stage-sink','full-readonly-stage-sink')
                     if full else ('clean','early','late'))
            if fault:
                modes = ('full-source-failure','full-source-failure-no-stage-sink')
            elif ';' in layout:
                modes = ('full-path-layout',)
            if native_fault:
                modes = ('full-native-' + native_fault,)
                if native_fault == 'oom':
                    modes *= 3
            for mode in modes:
                environment = os.environ.copy()
                parent_config = work / 'pyvenv.cfg'
                if mode == 'full-parent-config':
                    # Outside the manifest-bound bundle, inside this disposable
                    # test directory. An undeclared read exceeds getpath's limit.
                    parent_config.write_bytes(b'#' * (32 * 1024))
                if mode == 'full-polluted':
                    environment.update(PYTHONHOME=str(work / 'nonexistent-home'), PYTHONPATH=str(work),
                                       PYTHONSTARTUP=str(startup), PYTHONINSPECT='1', PYTHONSAFEPATH='0',
                                       PYTHONFAULTHANDLER='1', PYTHONTRACEMALLOC='5', PYTHONPROFILEIMPORTTIME='1')
                if mode == 'full-launcher':
                    environment['__PYVENV_LAUNCHER__'] = str(work / 'ambient-launcher.exe')
                if mode == 'full-empty-launcher':
                    environment['__PYVENV_LAUNCHER__'] = ''
                result = subprocess.run([str(exe),str(runtime / 'python314.dll'),mode,str(poisoned)],cwd=work,
                                        env=environment,capture_output=True,text=True,timeout=30)
                if native_fault:
                    fault_evidence['runs'].append({'mode':mode,'exit_code':result.returncode,
                                                  'stdout':result.stdout,'stderr':result.stderr})
                    (work / 'fault-evidence.json').write_text(json.dumps(fault_evidence,indent=2),encoding='utf-8')
                if parent_config.exists():
                    parent_config.unlink()
                with self.subTest(mode=mode):
                    self.assertNotIn('INJECTED_PYC_EXECUTED',result.stdout+result.stderr)
                    self.assertNotIn('AMBIENT_STARTUP_EXECUTED',result.stdout+result.stderr)
                    if mode == 'full-native-oom':
                        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                        self.assertIn('FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN',result.stderr)
                        self.assertIn('TEST_ONLY.JOB_MEMORY_LIMIT_64_MIB',result.stderr)
                        self.assertIn('TEST_ONLY.RAW_ALLOCATOR_EXHAUSTED',result.stderr)
                        self.assertIn('Fatal Python error: Py_SetPath: out of memory',result.stderr)
                        self.assertNotIn('TEST_ONLY.PATH_CALL_RETURNED',result.stderr)
                    elif mode == 'full-native-clear':
                        self.assertEqual(result.returncode,13,result.stdout+result.stderr)
                        self.assertIn('TEST_ONLY.CLEAR_FAILED_BEFORE_SOURCE',result.stderr)
                        self.assertIn('initialization failure revoked authority',result.stdout)
                        self.assertIn('TEST_ONLY.EXTERNAL_BODY_MARKER=0',result.stderr)
                    elif mode == 'full-native-install':
                        self.assertEqual(result.returncode,13,result.stdout+result.stderr)
                        self.assertIn('TEST_ONLY.TRACEBACK_CREATED_AFTER_PROOF',result.stderr)
                        self.assertIn('TEST_ONLY.EXTERNAL_BODY_MARKER=0',result.stderr)
                        self.assertIn('RETAINED_INSTALL_FAULT',result.stderr)
                        self.assertIn('<localcat-retained:importlib-external>',result.stderr)
                        self.assertIn('initialization failure revoked authority',result.stdout)
                    elif mode in ('full-source-failure', 'full-source-failure-no-stage-sink', 'full-path-layout'):
                        self.assertEqual(result.returncode,13,result.stdout+result.stderr)
                        self.assertIn('initialization failure revoked authority',result.stdout)
                        if fault and mode == 'full-source-failure':
                            self.assertIn('FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN',result.stderr)
                        if fault == 'install' and mode == 'full-source-failure':
                            self.assertIn('<localcat-retained:importlib-external>',result.stderr)
                    elif mode in ('full-launcher', 'full-empty-launcher'):
                        self.assertEqual(result.returncode,12,result.stdout+result.stderr)
                        self.assertIn('launcher rejected before Python load',result.stdout)
                    elif mode in ('early','full-early'):
                        self.assertEqual(result.returncode,10,result.stdout+result.stderr)
                        self.assertIn('interpreter failure closed retained handles',result.stdout)
                    elif mode in ('full-badpath','full-badflags'):
                        self.assertEqual(result.returncode,11,result.stdout+result.stderr)
                        self.assertIn('isolation mismatch revoked authority',result.stdout)
                    else:
                        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                        self.assertIn('retained interpreter initialization: PASS', result.stdout)
                    if mode in ('early','late','full-early'):
                        # Control: stock SourceFileLoader really accepts this timestamp/
                        # size-valid cache. The builtin retained path must never execute it.
                        loader = importlib.machinery.SourceFileLoader('evil_control',str(source))
                        with self.assertRaisesRegex(RuntimeError,'INJECTED_PYC_EXECUTED'):
                            exec(loader.get_code('evil_control'),{})
                if cache_dir.exists():
                    (cache_dir / 'utf_8.cpython-314.pyc').unlink()
                    cache_dir.rmdir()


if __name__ == '__main__':
    unittest.main()
