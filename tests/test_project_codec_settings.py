from dataclasses import replace
import builtins
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import parser_contracts as contracts
import project_codec_settings as settings
from editor_controller import EditorController, compose_project_enabled_editor_controller
from resource_repository import ResourceRepository


class ProjectCodecSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = settings.CodecSettingsRepository(self.root / 'config')

    def assert_builtins_read(self, runtime):
        inputs = (
            ('input.json', contracts.LOCALCAT_JSON_V1, b'{"name":"fixture","segments":[{"id":"one","source":"Source","target":"Target"}]}'),
            ('input.txt', contracts.LINE_TEXT_V1, b'Source\n'),
            ('input.po', contracts.GETTEXT_PO_V1, b'msgid "Source"\nmsgstr "Target"\n'),
            ('input.pot', contracts.GETTEXT_POT_V1, b'msgid "Source"\nmsgstr ""\n'),
        )
        for name, format_id, raw in inputs:
            with self.subTest(format=format_id):
                path = self.root / name
                path.write_bytes(raw)
                opened = runtime.surface.open_input(
                    contracts.SourceReference(str(self.root), str(path), name),
                    contracts.SelectionRequest(contracts.EffectivePurpose.PROJECT_DOCUMENT, format_id),
                    contracts.ReadRequest(contracts.EffectivePurpose.PROJECT_DOCUMENT, format_id),
                )
                self.addCleanup(opened.close)
                document = opened.materialize()
                self.assertIsNotNone(document.terminal)
                self.assertEqual(document.records[0].source, 'Source')

    def assert_rpy_unavailable(self, runtime):
        for request in (
            contracts.SelectionRequest(contracts.EffectivePurpose.PROJECT_DOCUMENT,
                                       contracts.FormatId('renpy-tl-v1')),
            contracts.SelectionRequest(contracts.EffectivePurpose.PROJECT_DOCUMENT,
                                       hints=contracts.SelectionHints(extensions=('.rpy',))),
        ):
            self.assertIsInstance(runtime.surface.select(request), contracts.SelectionFailure)

    def test_default_product_enables_rpy_and_builtin_composition_stays_separate(self):
        runtime = settings.compose_project_codec_runtime(self.store.config_dir)
        self.assertEqual(runtime.settings, settings.CodecSettings())
        self.assertTrue(runtime.availability[0].available)
        descriptor = runtime.surface.select(contracts.SelectionRequest(
            contracts.EffectivePurpose.PROJECT_DOCUMENT, contracts.FormatId('renpy-tl-v1')))
        self.assertEqual(descriptor.identity, contracts.CodecIdentity('localcat.rpy', 'renpy-tl', '1'))
        self.assert_builtins_read(runtime)
        self.assertFalse(self.store.settings_path.exists())

    def test_disabled_is_excluded_before_surface_and_reload_has_stable_facts(self):
        initial = settings.compose_project_codec_runtime(self.store.config_dir)
        disabled = settings.CodecSettings((settings.CodecProviderSetting('localcat.rpy', False),))
        self.store.save(disabled)
        with mock.patch.object(settings, '_bundled_rpy_provider', side_effect=AssertionError('disabled loaded')):
            runtime = settings.compose_project_codec_runtime(self.store.config_dir)
        self.assertEqual(runtime.settings, disabled)
        self.assertEqual(self.store.load(), disabled)
        self.assertNotEqual(initial.settings, runtime.settings)
        self.assertEqual(runtime.availability[0].code, 'PARSER.SELECTION.PROVIDER_DISABLED')
        self.assert_rpy_unavailable(runtime)
        self.assert_builtins_read(runtime)
        payload = json.loads(self.store.settings_path.read_text())
        self.assertEqual(payload, {'providers': [{'provider_id': 'localcat.rpy', 'enabled': False}]})
        self.store.save(settings.CodecSettings())
        self.assertEqual(settings.compose_project_codec_runtime(self.store.config_dir).settings, initial.settings)

    def test_missing_incompatible_provider_or_codec_never_breaks_builtins(self):
        real = settings._bundled_rpy_provider()
        for fault, provider in (
            ('PROVIDER_MISSING', None),
            ('PROVIDER_INCOMPATIBLE', mock.Mock(provider_id='localcat.rpy', provider_version='2')),
            ('PROVIDER_INCOMPATIBLE', mock.Mock(provider_id='localcat.rpy', provider_version='1',
             descriptors=lambda: (replace(real.descriptors()[0], identity=contracts.CodecIdentity('localcat.rpy', 'renpy-tl', '2')),))),
        ):
            with self.subTest(fault=fault), mock.patch.object(settings, '_bundled_rpy_provider', return_value=provider):
                runtime = settings.compose_project_codec_runtime(self.store.config_dir)
                self.assertFalse(runtime.availability[0].available)
                self.assertEqual(runtime.availability[0].code, 'PARSER.SELECTION.' + fault)
                self.assert_builtins_read(runtime)
                self.assert_rpy_unavailable(runtime)

    def test_configuration_closed_schema_rejects_paths_unknown_ids_versions_and_duplicate_keys(self):
        self.store.config_dir.mkdir()
        for payload in (
            '{"providers":[{"provider_id":"/private/game/plugin.py","enabled":true}]}',
            '{"providers":[{"provider_id":"https://example.com/plugin","enabled":true}]}',
            '{"providers":[{"provider_id":"localcat.rpy","enabled":true,"version":"2"}]}',
            '{"providers":[{"provider_id":"localcat.rpy","enabled":1}]}',
            '{"providers":[{"provider_id":"localcat.rpy","enabled":false,"enabled":true}]}',
            '{"providers":[],"module":"arbitrary.plugin"}',
            '{"providers":[]}',
            'x' * (16 * 1024 + 1),
        ):
            with self.subTest(payload=payload[:120]):
                self.store.settings_path.write_text(payload)
                with self.assertRaises(settings.CodecSettingsError):
                    self.store.load()
                runtime = settings.compose_project_codec_runtime(self.store.config_dir)
                self.assertIsNone(runtime.settings)
                self.assertEqual(runtime.availability[0].code, 'PARSER.SELECTION.PROVIDER_CONFIGURATION_INVALID')
                self.assert_builtins_read(runtime)

    def test_broken_provider_contract_is_unavailable_and_preserves_builtins(self):
        provider = mock.Mock(provider_id='localcat.rpy', provider_version='1', descriptors=lambda: None)
        with mock.patch.object(settings, '_bundled_rpy_provider', return_value=provider):
            runtime = settings.compose_project_codec_runtime(self.store.config_dir)
        self.assertEqual(runtime.availability[0].code, 'PARSER.SELECTION.PROVIDER_INCOMPATIBLE')
        self.assert_builtins_read(runtime)

    def test_same_identity_with_wrong_purpose_or_profile_contract_is_incompatible(self):
        base = settings._bundled_rpy_provider().descriptors()[0]
        for descriptor in (
            replace(base, purpose=contracts.EffectivePurpose.TRANSLATION_MEMORY),
            replace(base, capabilities=replace(base.capabilities, streaming_input=True)),
            replace(base, limit_profile=replace(base.limit_profile, profile_version=2)),
            replace(base, extensions=('.txt',)),
            replace(base, source_state_factory=None),
            replace(base, round_trip_serializer_factory=None),
            replace(base, input_consumption_policy=contracts.InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET),
        ):
            with self.subTest(descriptor=descriptor):
                provider = mock.Mock(provider_id='localcat.rpy', provider_version='1',
                                     descriptors=lambda: (descriptor,))
                with mock.patch.object(settings, '_bundled_rpy_provider', return_value=provider):
                    runtime = settings.compose_project_codec_runtime(self.store.config_dir)
                self.assertEqual(runtime.availability[0].code, 'PARSER.SELECTION.PROVIDER_INCOMPATIBLE')
                self.assert_rpy_unavailable(runtime)
                self.assert_builtins_read(runtime)

    def test_missing_known_provider_dependency_preserves_builtin_startup(self):
        original = builtins.__import__
        def missing(name, *args, **kwargs):
            if name == 'parser_rpy_codec':
                raise ModuleNotFoundError('missing optional format dependency', name='rpy_text_rules')
            return original(name, *args, **kwargs)
        with mock.patch.object(builtins, '__import__', side_effect=missing):
            runtime = settings.compose_project_codec_runtime(self.store.config_dir)
        self.assertEqual(runtime.availability[0].code, 'PARSER.SELECTION.PROVIDER_MISSING')
        self.assert_rpy_unavailable(runtime)
        self.assert_builtins_read(runtime)
        with mock.patch.object(settings, 'create_parser_application_surface',
                               side_effect=ModuleNotFoundError(name='parser_localcat_codec')):
            with self.assertRaises(ModuleNotFoundError):
                settings.compose_project_codec_runtime(self.store.config_dir)

    def test_invalid_settings_cannot_be_written_and_save_failure_preserves_previous(self):
        self.store.save(settings.CodecSettings())
        before = self.store.settings_path.read_bytes()
        for provider_id, enabled in (('unknown', True), ('localcat.rpy', 1)):
            with self.assertRaises((TypeError, ValueError)):
                settings.CodecProviderSetting(provider_id, enabled)
        with mock.patch.object(settings.os, 'replace', side_effect=OSError('private path')):
            with self.assertRaises(settings.CodecSettingsError) as rejected:
                self.store.save(settings.CodecSettings((settings.CodecProviderSetting('localcat.rpy', False),)))
        self.assertNotIn('private path', str(rejected.exception))
        self.assertEqual(self.store.settings_path.read_bytes(), before)
        self.assertEqual(tuple(self.store.config_dir.iterdir()), (self.store.settings_path,))

    def test_normal_controller_composition_injects_runtime_without_changing_default(self):
        repository = ResourceRepository(self.root / 'data')
        default = EditorController(repository)
        self.assertIsNone(default.project_codec_runtime)
        configured = compose_project_enabled_editor_controller(repository)
        self.assertTrue(configured.project_codec_runtime.availability[0].available)
        local = settings.CodecSettingsRepository(repository.config_dir)
        local.save(settings.CodecSettings((settings.CodecProviderSetting('localcat.rpy', False),)))
        restarted = compose_project_enabled_editor_controller(repository)
        self.assertFalse(restarted.project_codec_runtime.availability[0].available)
        self.assertTrue(configured.project_codec_runtime.availability[0].available)
        self.assert_builtins_read(restarted.project_codec_runtime)


if __name__ == '__main__':
    unittest.main()
