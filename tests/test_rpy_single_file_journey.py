"""真实单文件旅程：桌面命令接缝、包持久化、独立进程冷开与保真发布。"""
from __future__ import annotations

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
from resource_repository import ResourceRepository


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests' / 'fixtures' / 'rpy' / 'payloads'


def _finish(controller, job, *, export=False):
    job.run()
    outcome = (controller.finish_tl_export_job(job) if export
               else controller.finish_file_job(job))
    if not outcome.accepted:
        raise AssertionError(f'实际 Controller 拒绝了旅程操作：{outcome!r}')
    return outcome.result


def _export(controller, path):
    view = _finish(controller, controller.begin_tl_export_preview(path), export=True)
    result = _finish(controller, controller.begin_tl_export_publish(view, path), export=True)
    return {
        'status': view.status,
        'modified': view.modified_count,
        'empty': view.empty_count,
        'unconfirmed': view.unconfirmed_count,
        'segments': view.segment_count,
        'outcome': result.outcome,
        'sha256': result.output_sha256,
        'bytes': result.output_byte_count,
    }


def _segments(controller):
    return [
        {
            'identity': asdict(segment.identity.segment_identity),
            'source': segment.source,
            'target': segment.target,
            'speaker': segment.raw_speaker,
            'confirmed': segment.confirmed,
        }
        for segment in controller.workspace_view.segments
    ]


def _cold_export(package, data, target):
    """由独立解释器调用；只接收项目包与新设备本地数据目录。"""
    controller = compose_project_enabled_editor_controller(ResourceRepository(data))
    try:
        opened = controller.begin_file_open(package)
        _finish(controller, opened)
        state = _segments(controller)
        clean_before = not controller.active_project_dirty
        exported = _export(controller, target)
        clean_after = not controller.active_project_dirty
        # 导出未保存修改也不能推进项目包保存基线。
        controller.go_to_workspace_segment(controller.workspace_view.segments[0].identity)
        controller.update_workspace_target('')
        unsaved = _export(controller, target.with_name('unsaved-empty.rpy'))
        return {
            'pid': os.getpid(),
            'platform': sys.platform,
            'segments': state,
            'clean_before': clean_before,
            'clean_after': clean_after,
            'export': exported,
            'unsaved_export': unsaved,
            'dirty_after_unsaved_export': controller.active_project_dirty,
        }
    finally:
        controller.close_project()
        controller.abandon_file_jobs()


class RpySingleFileJourneyTests(unittest.TestCase):
    def test_import_edit_save_independent_cold_process_preview_and_export(self):
        cases = (
            ('mixed_dialogue_strings.rpy', '灯笼亮着。', 6, 2),
            ('bom_crlf.rpy', '雪 # 灯笼', 1, 0),
        )
        translated = '雪 "quote" \\ path\nnext\rline\ttab 😀'
        # 独立字节 oracle：不调用 codec 的扫描器或编码器生成预期输出。
        encoded = '雪 \\"quote\\" \\\\ path\\nnext\\rline\\ttab 😀'.encode('utf-8')
        for fixture, original_target, segment_count, empty_count in cases:
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                source_root = root / 'original-game'
                source = source_root / 'tl' / 'zh_Hans' / fixture
                source.parent.mkdir(parents=True)
                raw = (FIXTURES / fixture).read_bytes()
                source.write_bytes(raw)
                work = root / 'translator-work'
                work.mkdir()
                package = work / 'translation.localcat-project'
                outputs = root / 'chosen-output'
                outputs.mkdir()
                controller = compose_project_enabled_editor_controller(
                    ResourceRepository(root / 'first-device-data'))
                try:
                    review = _finish(controller, controller.begin_single_tl_import(source))
                    self.assertFalse(controller.has_active_project)
                    created = _finish(controller, controller.begin_single_tl_publish(
                        review, package, name='单文件旅程',
                        source_locale='en', target_locale='zh-CN'))
                    self.assertTrue(created.receipt.durable)
                    self.assertFalse(controller.active_project_dirty)
                    initial = _segments(controller)
                    self.assertEqual(len(initial), segment_count)
                    self.assertTrue(all(not item['confirmed'] for item in initial))
                    noop = _export(controller, outputs / 'unchanged.rpy')
                    self.assertEqual(noop['status'], 'ready')
                    self.assertEqual(noop['outcome'], 'published')
                    self.assertEqual((noop['modified'], noop['empty'], noop['unconfirmed']),
                                     (0, empty_count, segment_count))
                    self.assertEqual((outputs / 'unchanged.rpy').read_bytes(), raw)
                    self.assertFalse(controller.active_project_dirty)
                    controller.update_workspace_target(translated)
                    controller.confirm_current()
                    self.assertTrue(controller.active_project_dirty)
                    saved = _finish(controller, controller.begin_file_save())
                    self.assertTrue(saved.receipt.durable)
                    self.assertFalse(controller.active_project_dirty)
                    saved_segments = _segments(controller)
                    self.assertEqual(saved_segments[0]['target'], translated)
                    self.assertTrue(saved_segments[0]['confirmed'])
                    self.assertEqual([item['identity'] for item in initial],
                                     [item['identity'] for item in saved_segments])
                finally:
                    controller.close_project()
                    controller.abandon_file_jobs()

                package_bytes = package.read_bytes()
                source_root.rename(root / 'moved-away-game')
                self.assertFalse(source.exists())
                output = outputs / 'cold-export.rpy'
                process = subprocess.run(
                    [sys.executable, '-B', '-c',
                     'import json, sys; from pathlib import Path; '
                     'from tests.test_rpy_single_file_journey import _cold_export; '
                     'print(json.dumps(_cold_export(*(Path(p) for p in sys.argv[1:]))))',
                     str(package), str(root / 'second-device-data'), str(output)],
                    cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=60,
                )
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                cold = json.loads(process.stdout)
                self.assertNotEqual(cold['pid'], os.getpid())
                self.assertEqual(cold['platform'], sys.platform)
                self.assertEqual(cold['segments'], saved_segments)
                self.assertTrue(cold['clean_before'])
                self.assertTrue(cold['clean_after'])
                original = original_target.encode('utf-8')
                self.assertEqual(raw.count(original), 1)
                expected = raw.replace(original, encoded)
                self.assertEqual(output.read_bytes(), expected)
                self.assertEqual(cold['export'], {
                    'status': 'ready', 'modified': 1, 'empty': empty_count,
                    'unconfirmed': segment_count - 1, 'segments': segment_count,
                    'outcome': 'published', 'sha256': hashlib.sha256(expected).hexdigest(),
                    'bytes': len(expected),
                })
                self.assertEqual(cold['unsaved_export']['outcome'], 'published')
                self.assertEqual(cold['unsaved_export']['empty'], empty_count + 1)
                self.assertTrue(cold['dirty_after_unsaved_export'])
                self.assertEqual((outputs / 'unsaved-empty.rpy').read_bytes(),
                                 raw.replace(original, b''))
                self.assertEqual(package.read_bytes(), package_bytes)
                self.assertEqual((root / 'moved-away-game' / 'tl' / 'zh_Hans' / fixture).read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
