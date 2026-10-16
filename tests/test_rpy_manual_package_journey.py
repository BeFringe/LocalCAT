"""多章节项目与 JSONL/CSV 资源包通过独立进程完成手工往返。"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from editor_contracts import ResourceKind
from project_codec_settings import CodecProviderSetting, CodecSettings, CodecSettingsRepository
from qt_editor import _compose_editor_controller
from resource_package_contracts import ResourceImportMode
from resource_repository import ResourceRepository
from tm_contracts import TMMatchType
from tests.test_rpy_multi_file_journey import _batch_export
from tests.test_rpy_single_file_journey import _finish, _segments


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/rpy/manual_package'
REFS = ('b/extra/ending.rpy', 'a/intro.rpy', 'b/intro.rpy')
PACKAGE_NAMES = ('project.localcat-project', 'memory.localcat-resource', 'terms.localcat-resource')
TM_TARGET = '记忆译文 灯笼 [name]。'


def _compose(data):
    controller, composition = _compose_editor_controller(ResourceRepository(data))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # 运行真实 matcher 验证以消费术语；不铸造、恢复或运行 Gate D 资格。
    composition.matcher_validation_owner.validate_text_v1(
        generated_at_utc=now, valid_until_utc=now + timedelta(days=1), evaluated_at_utc=now)
    assert not controller.tm_retrieval_status().fuzzy_available
    return controller, composition


def _close(controller, composition):
    controller.close_project()
    controller.abandon_file_jobs()
    composition._close_frozen_inputs()


def _source_finish(controller, job):
    job.run()
    outcome = controller.finish_source_update_job(job)
    assert outcome.accepted, outcome
    return outcome.result


def _select(controller, source_ref, index=0):
    document = next(d for d in controller.workspace_view.documents if d.source_ref == source_ref)
    controller.select_workspace_document(document.identity)
    if index:
        controller.move_workspace(index)


def _facts(controller):
    # issued identity 的会话/代次必须重新签发，仅持久文档身份随包恢复。
    return {'documents': [d.identity.document_id for d in controller.workspace_view.documents],
            'refs': [d.source_ref for d in controller.workspace_view.documents],
            'segments': _segments(controller)}


def _navigation(controller):
    original = _facts(controller)
    assert original['refs'] == list(REFS)
    tree = controller.workspace_document_tree
    assert tuple(group.name for group in tree) == ('b', 'a')
    assert tree[0].children[0].children[0].source_ref == REFS[0]
    assert tree[0].children[1].source_ref == REFS[2]
    assert tree[1].children[0].source_ref == REFS[1]
    _select(controller, REFS[0])
    controller.move_workspace(1)
    assert controller.current_workspace_document_id == controller.workspace_view.documents[1].identity.document_id
    controller.move_workspace(1)
    assert controller.current_workspace_document_id == controller.workspace_view.documents[2].identity.document_id
    assert _facts(controller) == original


def _import_resources(controller, folder, prefix):
    imported = []
    for name, kind, count in (
        ('memory', ResourceKind.TRANSLATION_MEMORY, 2),
        ('terms', ResourceKind.TERMBASE, 1),
    ):
        path = folder / (name + '.localcat-resource')
        report = controller.validate_resource_package(path)
        assert report.resource_kind.value == kind.value
        assert report.record_count >= count
        preview = controller.preview_resource_package_import(
            path, ResourceImportMode.CREATE_NEW, new_resource_name=f'{prefix} {name}')
        result = controller.apply_resource_package_import(preview)
        assert result.receipt.package_artifact_digest == report.artifact_digest
        resource = controller.repository.get(result.destination_resource_id)
        assert resource.path.is_relative_to(controller.repository.config_dir)
        imported.append(resource)
    return imported


def _prepare_sender(root):
    root.mkdir()
    source, updated, data, outbound = (root / name for name in ('old', 'updated', 'data', 'outbound'))
    shutil.copytree(FIXTURES / 'old', source)
    shutil.copytree(FIXTURES / 'updated', updated)
    outbound.mkdir()
    repository = ResourceRepository(data)
    tm = repository.create_resource('发送端记忆', ResourceKind.TRANSLATION_MEMORY)
    tm.path.write_bytes((FIXTURES / 'memory.jsonl').read_bytes())
    terms = repository.create_resource('发送端术语', ResourceKind.TERMBASE)
    terms.path.write_bytes((FIXTURES / 'terms.csv').read_bytes())
    controller, composition = _compose(data)
    try:
        preflight = controller.prepare_tm_activation(tm.id)
        operation = controller.activate_tm_resource(preflight)
        completed = controller.wait_tm_activation(operation.operation_id, timeout=30.0)
        assert completed.succeeded, completed
        # 来源配置有意与接收端不同；只运输逻辑资源，不复制本地选择。
        for resource in (tm, terms):
            controller.update_resource(replace(resource, active=False, lookup=False, update=False))
        review = _finish(controller, controller.begin_directory_open(source))
        assert review.preview.complete
        entries = {entry.source_ref: entry.entry_id for entry in review.preview.entries if entry.selectable}
        assert 'unselected.rpy' in entries
        published = _finish(controller, controller.begin_directory_publish(
            review, tuple(entries[ref] for ref in REFS), outbound / PACKAGE_NAMES[0],
            name='三章手工包旅程', source_locale='en', target_locale='zh-CN'))
        assert published.receipt.durable
        _navigation(controller)
        initial = _facts(controller)
        assert len(initial['segments']) == 4
        assert initial['segments'][-1]['target'] == ''
        assert all(not segment['confirmed'] for segment in initial['segments'])
        _select(controller, REFS[1])
        controller.confirm_current()
        assert controller.workspace_view.segments[1].confirmed
        assert _finish(controller, controller.begin_file_save()).receipt.durable
        controller.close_project()
        _finish(controller, controller.begin_file_open(outbound / PACKAGE_NAMES[0]))
        # 冷开包没有设备路径绑定；在新版根下明确提供各 source_ref。
        assert controller.source_update_root is None
        change = _source_finish(controller, controller.begin_source_update_preview(updated, REFS))
        assert (change.preview.source_changed_count, change.preview.unchanged_count,
                change.preview.new_count, change.preview.removed_count,
                change.preview.ambiguous_count, change.preview.unresolved_count) == (1, 3, 0, 0, 0, 0)
        _source_finish(controller, controller.begin_source_update_apply(change, ()))
        _select(controller, REFS[1])
        assert (controller.current_segment.source, controller.current_segment.target,
                controller.current_segment.confirmed) == (
                    'Welcome lantern [name].', '原译 灯笼 [name]。', False)
        assert _finish(controller, controller.begin_file_save()).receipt.durable
        _navigation(controller)
        for resource, name in ((tm, PACKAGE_NAMES[1]), (terms, PACKAGE_NAMES[2])):
            controller.export_resource_package(resource.id, outbound / name)
        return {'pid': os.getpid(), 'state': _facts(controller), 'resource_ids': [tm.id, terms.id],
                'flags': [[r.active, r.lookup, r.update] for r in controller.list_resources()],
                'fuzzy': controller.tm_retrieval_status().fuzzy_available}
    finally:
        _close(controller, composition)


def _receive(root):
    data, incoming = root / 'data', root / 'incoming'
    # 本机关闭 RPY 的选择先于项目打开；包不能覆盖此选择或恢复执行权限。
    CodecSettingsRepository(data).save(CodecSettings((CodecProviderSetting('localcat.rpy', False),)))
    controller, composition = _compose(data)
    try:
        assert controller.list_resources() == ()
        _finish(controller, controller.begin_file_open(incoming / PACKAGE_NAMES[0]))
        assert '禁用' in controller.tl_export_unavailable_reason
        assert controller.list_resources() == ()
        before = _facts(controller)
        _navigation(controller)
        imported = _import_resources(controller, incoming, '接收端')
        # 包不含发送端 active/lookup/update；本地导入按 owner 默认创建。
        assert all((r.active, r.lookup, r.update) == (True, True, True) for r in imported)
        for resource in imported:
            controller.update_resource(replace(resource, active=False, lookup=False, update=False))
        _select(controller, REFS[1])
        assert controller.tm_suggestion_report().suggestions == ()
        assert controller.term_suggestions() == ()
        for resource in imported:
            controller.update_resource(replace(resource, active=True, lookup=True,
                                                update=resource.kind is ResourceKind.TRANSLATION_MEMORY))
        report = controller.tm_suggestion_report()
        exact = next(s for s in report.suggestions if s.target == TM_TARGET)
        assert exact.matched_source == controller.current_segment.source
        assert exact.match_type is TMMatchType.EXACT
        assert exact.final_similarity == 1.0
        controller.apply_tm_suggestion(exact)
        assert (controller.current_segment.target, controller.current_segment.confirmed) == (TM_TARGET, False)
        controller.confirm_current()
        _select(controller, REFS[2], 1)
        assert controller.current_segment.source == 'Open lantern'
        term = next(s for s in controller.term_suggestions() if s.source_term == 'lantern')
        controller.insert_term_suggestion(term, position=0)
        assert controller.current_segment.target == '灯笼'
        confirmed = controller.confirm_current()
        assert confirmed.write_report.written_resource_ids
        assert not confirmed.write_report.errors
        assert _finish(controller, controller.begin_file_save()).receipt.durable
        _navigation(controller)
        assert not controller.tm_retrieval_status().fuzzy_available
        assert not (data / 'gate-d-qualification').exists()
        assert '禁用' in controller.tl_export_unavailable_reason
        return {'pid': os.getpid(), 'before': before, 'state': _facts(controller),
                'resource_ids': [r.id for r in imported], 'tm_write_ids': list(confirmed.write_report.written_resource_ids),
                'fuzzy': controller.tm_retrieval_status().fuzzy_available}
    finally:
        _close(controller, composition)


def _cold_return(root):
    data, incoming, outgoing = root / 'data', root / 'incoming', root / 'outgoing'
    outgoing.mkdir()
    # 显式在接收设备启用发行中已知 codec，独立新进程才导出 TL。
    CodecSettingsRepository(data).save(CodecSettings((CodecProviderSetting('localcat.rpy', True),)))
    controller, composition = _compose(data)
    try:
        _finish(controller, controller.begin_file_open(incoming / PACKAGE_NAMES[0]))
        _navigation(controller)
        state = _facts(controller)
        assert state['segments'][1]['target'] == TM_TARGET and state['segments'][1]['confirmed']
        assert state['segments'][3]['target'] == '灯笼' and state['segments'][3]['confirmed']
        assert not controller.active_project_dirty
        exported = _batch_export(controller, root / 'tl-export')
        _select(controller, REFS[2], 1)
        assert any(s.target == '灯笼' for s in controller.tm_suggestion_report().suggestions)
        for resource in controller.list_resources():
            name = PACKAGE_NAMES[1] if resource.kind is ResourceKind.TRANSLATION_MEMORY else PACKAGE_NAMES[2]
            controller.export_resource_package(resource.id, outgoing / name)
        controller.close_project()
        shutil.copyfile(incoming / PACKAGE_NAMES[0], outgoing / PACKAGE_NAMES[0])
        return {'pid': os.getpid(), 'state': state, 'files': [asdict(item) for item in exported.files],
                'fuzzy': controller.tm_retrieval_status().fuzzy_available}
    finally:
        _close(controller, composition)


def _receive_return(root):
    controller, composition = _compose(root / 'data')
    try:
        original_resources = controller.list_resources()
        assert len(original_resources) == 2
        assert all(not r.active and not r.lookup and not r.update for r in original_resources)
        _finish(controller, controller.begin_file_open(root / 'incoming' / PACKAGE_NAMES[0]))
        assert controller.list_resources() == original_resources
        imported = _import_resources(controller, root / 'incoming', '返程本地')
        for resource in imported:
            controller.update_resource(replace(resource, active=True, lookup=True, update=False))
        _navigation(controller)
        _select(controller, REFS[2], 1)
        assert any(s.target == '灯笼' for s in controller.tm_suggestion_report().suggestions)
        assert any(s.target_term == '灯笼' for s in controller.term_suggestions())
        result = _batch_export(controller, root / 'tl-export')
        assert not controller.tm_retrieval_status().fuzzy_available
        assert not (root / 'data' / 'gate-d-qualification').exists()
        assert all(controller.repository.get(r.id) == r for r in original_resources)
        return {'pid': os.getpid(), 'state': _facts(controller),
                'resource_ids': [r.id for r in imported],
                'original_resource_ids': [r.id for r in original_resources],
                'files': [asdict(item) for item in result.files]}
    finally:
        _close(controller, composition)


def _run_stage(stage, root):
    process = subprocess.run(
        [sys.executable, '-B', '-c',
         'import json, sys; from pathlib import Path; '
         'from tests import test_rpy_manual_package_journey as journey; '
         'print(json.dumps(getattr(journey, sys.argv[1])(Path(sys.argv[2])), ensure_ascii=False))',
         stage, str(root)], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=180)
    if process.returncode:
        raise AssertionError(f'{stage} exited {process.returncode}\n{process.stdout}\n{process.stderr}')
    return json.loads(process.stdout)


class RpyManualPackageJourneyTests(unittest.TestCase):
    def assert_transport_members(self, folder):
        """验证实际载荷 schema/成员白名单，而非搜索敏感词。"""
        with zipfile.ZipFile(folder / PACKAGE_NAMES[0]) as package:
            manifest = json.loads(package.read('manifest.json'))
            self.assertEqual(set(manifest), {
                'schema', 'carrier_profile', 'limit_profile', 'project_id', 'workspace_content_digest',
                'name', 'source_locale', 'target_locale', 'origin_kind', 'origin_profile',
                'portable_root_ref', 'documents'})
            members = {'manifest.json'}
            for document in manifest['documents']:
                self.assertEqual(set(document), {'codec_private_member', 'display_name', 'document_id',
                                                'document_member', 'order', 'source_member', 'source_ref'})
                for key in ('document_member', 'source_member', 'codec_private_member'):
                    members.add(document[key]['path'])
            self.assertEqual(set(package.namelist()), members)
            self.assertEqual([d['source_ref'] for d in manifest['documents']], list(REFS))
        for name, payload, profile in ((PACKAGE_NAMES[1], 'payload/tm.jsonl', 'localcat-tm-jsonl-v1'),
                                       (PACKAGE_NAMES[2], 'payload/termbase.csv', 'localcat-termbase-csv-v1')):
            with zipfile.ZipFile(folder / name) as package:
                self.assertEqual(set(package.namelist()), {'manifest.json', payload})
                manifest = json.loads(package.read('manifest.json'))
                self.assertEqual(set(manifest), {'schema', 'carrier_profile', 'payload_profile_set', 'resource'})
                self.assertEqual(set(manifest['resource']), {'kind', 'payload_profile', 'payload', 'profile_counts'})
                self.assertEqual(manifest['resource']['payload_profile'], profile)
                descriptor = manifest['resource']['payload']
                self.assertEqual(set(descriptor), {'path', 'sha256', 'byte_count', 'record_count'})
                raw = package.read(payload)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), descriptor['sha256'])
                self.assertEqual(len(raw), descriptor['byte_count'])

    def test_directory_reconcile_resource_packages_and_two_device_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sender = _run_stage('_prepare_sender', root / 'sender')
            outbound = root / 'sender' / 'outbound'
            self.assert_transport_members(outbound)
            receiver = root / 'receiver'
            (receiver / 'incoming').mkdir(parents=True)
            for name in PACKAGE_NAMES:
                shutil.copyfile(outbound / name, receiver / 'incoming' / name)
            self.assertEqual(set(p.name for p in (receiver / 'incoming').iterdir()), set(PACKAGE_NAMES))
            (root / 'sender').rename(root / 'sender-offline')
            received = _run_stage('_receive', receiver)
            self.assertEqual(received['before'], sender['state'])
            self.assertTrue(set(sender['resource_ids']).isdisjoint(received['resource_ids']))
            self.assertEqual(sender['flags'], [[False, False, False], [False, False, False]])
            self.assertFalse(sender['fuzzy'])
            self.assertFalse(received['fuzzy'])
            cold = _run_stage('_cold_return', receiver)
            self.assertEqual(cold['state'], received['state'])
            self.assert_transport_members(receiver / 'outgoing')
            # 返程回到第一设备的原数据目录，保持原本资源选择；原 TL 仍离线。
            returned_root = root / 'sender'
            (returned_root / 'incoming').mkdir(parents=True)
            (root / 'sender-offline' / 'data').rename(returned_root / 'data')
            for name in PACKAGE_NAMES:
                shutil.copyfile(receiver / 'outgoing' / name, returned_root / 'incoming' / name)
            receiver.rename(root / 'receiver-offline')
            returned = _run_stage('_receive_return', returned_root)
            self.assertEqual(returned['state'], received['state'])
            self.assertEqual(returned['original_resource_ids'], sender['resource_ids'])
            self.assertTrue(set(returned['resource_ids']).isdisjoint(received['resource_ids']))
            self.assertEqual(len({s['pid'] for s in (sender, received, cold, returned)}), 4)
            self.assertNotIn(os.getpid(), {s['pid'] for s in (sender, received, cold, returned)})
            expected = {ref: (FIXTURES / 'updated' / ref).read_bytes() for ref in REFS}
            expected[REFS[1]] = expected[REFS[1]].replace('新模板占位 [name]。'.encode(), TM_TARGET.encode())
            expected[REFS[2]] = expected[REFS[2]].replace(b'new ""', 'new "灯笼"'.encode())
            for stage, folder in ((cold, root / 'receiver-offline' / 'tl-export'),
                                  (returned, returned_root / 'tl-export')):
                self.assertEqual([item['source_ref'] for item in stage['files']], list(REFS))
                self.assertEqual({path.relative_to(folder).as_posix() for path in folder.rglob('*.rpy')}, set(REFS))
                for item in stage['files']:
                    raw = expected[item['source_ref']]
                    self.assertEqual((folder / item['source_ref']).read_bytes(), raw)
                    self.assertEqual(item['output_sha256'], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(item['output_byte_count'], len(raw))
            for version in ('old', 'updated'):
                for path in (FIXTURES / version).rglob('*.rpy'):
                    self.assertEqual((root / 'sender-offline' / version / path.relative_to(FIXTURES / version)).read_bytes(),
                                     path.read_bytes())


if __name__ == '__main__':
    unittest.main()
