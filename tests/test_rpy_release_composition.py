"""RPY 发行声明与隔离 source 组合，不依赖游戏或 Ren'Py 环境。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools.build_windows_ordinary import ROOT, collect_inputs


class RpyReleaseCompositionTests(unittest.TestCase):
    def test_declared_rpy_roots_are_collected_as_code_and_input_digests(self):
        declaration = json.loads((ROOT / 'packaging/windows/frozen_roots.json').read_bytes())
        owners = [owner for owner in declaration['owners']
                  if owner['owning_spec'] == 'rpy-project-codec']
        self.assertEqual(len(owners), 1, 'RPY 应有独立的发行模块声明')
        required = {
            'parser_rpy_codec', 'rpy_text_rules', 'project_codec_settings',
            'rpy_project_adapter', 'rpy_project_export', 'rpy_project_batch_export',
            'project_export_contracts', 'editor_source_update',
            'qt_project_export_dialog', 'qt_project_source_update_dialog',
        }
        self.assertTrue(required <= set(owners[0]['module_roots']))
        inputs, data, modules = collect_inputs(ROOT)
        for module in required:
            with self.subTest(module=module):
                path = module + '.py'
                self.assertIn(module, modules)
                self.assertEqual(inputs[path], hashlib.sha256((ROOT / path).read_bytes()).hexdigest())
                self.assertNotIn(path, data)

    def test_source_composition_in_isolated_copy_enables_and_disables_rpy(self):
        # 复制产品模块后在独立解释器启动，不把原仓库加入 sys.path。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            for path in ROOT.glob('*.py'):
                shutil.copyfile(path, source / path.name)
            script = '''
import importlib.abc, importlib, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
class NoRenpy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'renpy':
            raise AssertionError('发行运行不应导入 Ren\\'Py')
sys.meta_path.insert(0, NoRenpy())
from project_codec_settings import (CodecSettings, CodecProviderSetting,
    CodecSettingsRepository, compose_project_codec_runtime)
from parser_contracts import (SelectionRequest, SelectionFailure,
    EffectivePurpose, FormatId)
data = root.parent / 'data'
store = CodecSettingsRepository(data)
for enabled in (True, False, True):
    store.save(CodecSettings((CodecProviderSetting('localcat.rpy', enabled),)))
    runtime = compose_project_codec_runtime(data)
    assert runtime.availability[0].available is enabled
    result = runtime.surface.select(SelectionRequest(
        EffectivePurpose.PROJECT_DOCUMENT, FormatId('renpy-tl-v1')))
    assert isinstance(result, SelectionFailure) is (not enabled)
    builtin = runtime.surface.select(SelectionRequest(
        EffectivePurpose.PROJECT_DOCUMENT, FormatId('localcat-json-v1')))
    assert not isinstance(builtin, SelectionFailure)
for name in ('parser_rpy_codec', 'rpy_project_adapter', 'rpy_project_batch_export',
             'qt_project_export_dialog', 'qt_project_source_update_dialog'):
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root)
print('isolated source: enabled, disabled, re-enabled; built-ins and UI available')
'''
            result = subprocess.run(
                [sys.executable, '-E', '-P', '-c', script, str(source)], cwd=root,
                env={key: value for key, value in os.environ.items()
                     if key not in ('PYTHONPATH', 'PYTHONHOME')},
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('isolated source:', result.stdout)


if __name__ == '__main__':
    unittest.main()
