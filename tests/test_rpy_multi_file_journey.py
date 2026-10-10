"""多 TL 保存、禁用 codec 中立编辑、独立进程冷开及精确字节导出。"""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from editor_controller import compose_project_enabled_editor_controller
from project_codec_settings import CodecSettings, CodecProviderSetting, CodecSettingsRepository
from resource_repository import ResourceRepository
from tests.test_rpy_single_file_journey import _finish, _segments


ROOT = Path(__file__).resolve().parents[1]


def _batch_export(controller, target):
    target.mkdir()
    preview = _finish(controller, controller.begin_tl_export_preview(target), export=True)
    assert preview.status == 'ready'
    assert preview.missing_directories
    prepared = _finish(controller, controller.begin_tl_export_directory_preparation(preview, target), export=True)
    assert prepared.outcome == 'prepared'
    assert not list(target.rglob('*.rpy'))
    fresh = _finish(controller, controller.begin_tl_export_preview(target), export=True)
    assert fresh.status == 'ready' and not fresh.missing_directories
    assert fresh.preview_id != preview.preview_id
    result = _finish(controller, controller.begin_tl_export_publish(fresh, target), export=True)
    assert result.outcome == 'published'
    for item in result.files:
        raw = Path(item.target_path).read_bytes()
        assert item.output_sha256 == hashlib.sha256(raw).hexdigest()
        assert item.output_byte_count == len(raw)
    return result


def _cold_batch(package, data, output):
    controller = compose_project_enabled_editor_controller(ResourceRepository(data))
    try:
        _finish(controller, controller.begin_file_open(package))
        state = _segments(controller)
        result = _batch_export(controller, output)
        return {'pid': os.getpid(), 'segments': state, 'dirty': controller.active_project_dirty,
                'files': [asdict(item) for item in result.files]}
    finally:
        controller.close_project()
        controller.abandon_file_jobs()


class RpyMultiFileJourneyTests(unittest.TestCase):
    def test_same_names_mixed_tl_disabled_edits_and_cold_export_without_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'game'
            originals = {
                'b/intro.rpy': b'\xef\xbb\xbf' + (
                    '# synthetic dialogue\r\ntranslate chinese intro_b:\r\n'
                    '    # guide "Hello [name]."\r\n    guide "你好 [name]。"\r\n'
                ).encode('utf-8'),
                'a/intro.rpy': (
                    '# synthetic strings\ntranslate chinese strings:\n'
                    '    old "Open"\n    new ""\n'
                ).encode('utf-8'),
            }
            for ref, raw in originals.items():
                path = source / ref
                path.parent.mkdir(parents=True)
                path.write_bytes(raw)
            package, data = root / 'project.localcat-project', root / 'data'
            controller = compose_project_enabled_editor_controller(ResourceRepository(data))
            try:
                imported = _finish(controller, controller.begin_selected_tl_publish(
                    source, tuple(source / ref for ref in originals), package,
                    name='多文件发行旅程', source_locale='en', target_locale='zh'))
                self.assertTrue(imported.receipt.durable)
                self.assertEqual([d.source_ref for d in controller.workspace_view.documents], list(originals))
                self.assertTrue(all(not item.confirmed for item in controller.workspace_view.segments))
                _batch_export(controller, root / 'no-op')
                for ref, raw in originals.items():
                    self.assertEqual((root / 'no-op' / ref).read_bytes(), raw)
                controller.update_workspace_target('旅程 [name]。')
                controller.confirm_current()
                self.assertTrue(_finish(controller, controller.begin_file_save()).receipt.durable)
                controller.close_project()

                CodecSettingsRepository(data).save(CodecSettings((CodecProviderSetting('localcat.rpy', False),)))
                _finish(controller, controller.begin_file_open(package))
                self.assertIn('禁用', controller.tl_export_unavailable_reason)
                controller.select_workspace_document(controller.workspace_view.documents[1].identity)
                controller.update_workspace_target('离线修改')
                self.assertTrue(_finish(controller, controller.begin_file_save()).receipt.durable)
                state = _segments(controller)
            finally:
                controller.close_project()
                controller.abandon_file_jobs()
            saved = package.read_bytes()
            source.rename(root / 'source-offline')
            process = subprocess.run(
                [sys.executable, '-B', '-c',
                 'import json, sys; from pathlib import Path; '
                 'from tests.test_rpy_multi_file_journey import _cold_batch; '
                 'print(json.dumps(_cold_batch(*(Path(p) for p in sys.argv[1:]))))',
                 str(package), str(root / 'second-device'), str(root / 'cold-export')],
                cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=60,
            )
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            cold = json.loads(process.stdout)
            self.assertNotEqual(cold['pid'], os.getpid())
            self.assertEqual(cold['segments'], state)
            self.assertFalse(cold['dirty'])
            self.assertEqual([item['source_ref'] for item in cold['files']], list(originals))
            expected = {
                'b/intro.rpy': originals['b/intro.rpy'].replace('你好 [name]。'.encode(), '旅程 [name]。'.encode()),
                'a/intro.rpy': originals['a/intro.rpy'].replace(b'new ""', 'new "离线修改"'.encode()),
            }
            for ref, raw in expected.items():
                self.assertEqual((root / 'cold-export' / ref).read_bytes(), raw)
                self.assertEqual((root / 'source-offline' / ref).read_bytes(), originals[ref])
            self.assertEqual(package.read_bytes(), saved)


if __name__ == '__main__':
    unittest.main()
