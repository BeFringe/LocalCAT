from __future__ import annotations

from pathlib import Path
import unittest

from parser_composition import (
    ProviderBinding,
    ProviderConfigurationError,
    compose_registry,
)
from parser_contracts import CodecProvider
from tests.parser_architecture_test_support import (
    PARSER_CODEC_PREFIXES,
    SourceModule,
    build_parser_architecture_policy,
    collect_import_references,
)
from tests.test_parser_registry import _descriptor


class _Provider:
    """Structural provider fake with no CodecProvider/BaseParser inheritance."""

    def __init__(
        self,
        provider_id: str,
        provider_version: str,
        descriptors: tuple[object, ...],
    ) -> None:
        self.provider_id = provider_id
        self.provider_version = provider_version
        self._descriptors = descriptors
        self.private_sidecar = object()

    def descriptors(self) -> tuple[object, ...]:
        return self._descriptors


class _RaisingProvider(_Provider):
    def descriptors(self) -> tuple[object, ...]:
        raise RuntimeError("source and secret must not escape")


class ParserCompositionTests(unittest.TestCase):
    def test_empty_seam_rejects_direct_external_descriptor_injection(self) -> None:
        empty = compose_registry()
        self.assertEqual(empty.descriptors, ())

        descriptor = _descriptor("direct")
        with self.assertRaises(ProviderConfigurationError) as caught:
            compose_registry(descriptors=(descriptor,))
        self.assertEqual(
            caught.exception.code,
            "PARSER.SELECTION.DIRECT_DESCRIPTOR_FORBIDDEN",
        )

    def test_version_rejected_plugin_cannot_bypass_binding_with_direct_descriptor(self) -> None:
        descriptor = _descriptor(
            "gated-plugin",
            provider_id="plugin.gated",
        )
        provider = _Provider("plugin.gated", "2", (descriptor,))
        binding = ProviderBinding(
            provider_id="plugin.gated",
            provider=provider,
            enabled=True,
            compatible_versions=("1",),
        )

        with self.assertRaises(ProviderConfigurationError) as gated:
            compose_registry(providers=(binding,))
        with self.assertRaises(ProviderConfigurationError) as bypass:
            compose_registry(
                providers=(binding,),
                descriptors=(descriptor,),
            )

        self.assertEqual(
            gated.exception.code,
            "PARSER.SELECTION.PROVIDER_VERSION_INCOMPATIBLE",
        )
        self.assertEqual(
            bypass.exception.code,
            "PARSER.SELECTION.DIRECT_DESCRIPTOR_FORBIDDEN",
        )

    def test_structural_provider_registers_opaque_capability_without_interpretation(self) -> None:
        descriptor = _descriptor(
            "opaque-plugin",
            provider_id="plugin.rpy",
            opaque_features=("opaque-round-trip-token",),
        )
        provider = _Provider("plugin.rpy", "7", (descriptor,))
        binding = ProviderBinding(
            provider_id="plugin.rpy",
            provider=provider,
            enabled=True,
            compatible_versions=("7",),
        )

        registry = compose_registry(providers=(binding,))
        reader = registry.create_reader(descriptor)

        self.assertNotIn(CodecProvider, type(provider).__mro__)
        self.assertIsInstance(provider, CodecProvider)
        self.assertIs(reader.descriptor, descriptor)
        self.assertEqual(
            reader.descriptor.capabilities.opaque_features,
            ("opaque-round-trip-token",),
        )
        self.assertIsNotNone(provider.private_sidecar)

    def test_missing_disabled_and_version_incompatible_are_structured(self) -> None:
        provider = _Provider("plugin.test", "2", ())
        cases = (
            (
                ProviderBinding(
                    provider_id="plugin.test",
                    provider=None,
                    enabled=True,
                    compatible_versions=("2",),
                ),
                "PARSER.SELECTION.PROVIDER_MISSING",
            ),
            (
                ProviderBinding(
                    provider_id="plugin.test",
                    provider=provider,
                    enabled=False,
                    compatible_versions=("2",),
                ),
                "PARSER.SELECTION.PROVIDER_DISABLED",
            ),
            (
                ProviderBinding(
                    provider_id="plugin.test",
                    provider=provider,
                    enabled=True,
                    compatible_versions=("1",),
                ),
                "PARSER.SELECTION.PROVIDER_VERSION_INCOMPATIBLE",
            ),
        )
        for binding, expected_code in cases:
            with self.subTest(code=expected_code):
                with self.assertRaises(ProviderConfigurationError) as caught:
                    compose_registry(providers=(binding,))
                self.assertEqual(caught.exception.code, expected_code)

    def test_provider_identity_and_descriptor_authority_must_agree(self) -> None:
        wrong_provider = _Provider("actual.provider", "1", ())
        wrong_descriptor = _descriptor(
            "wrong-provider-descriptor",
            provider_id="another.provider",
        )
        cases = (
            ProviderBinding(
                provider_id="configured.provider",
                provider=wrong_provider,
                enabled=True,
                compatible_versions=("1",),
            ),
            ProviderBinding(
                provider_id="actual.provider",
                provider=_Provider("actual.provider", "1", (wrong_descriptor,)),
                enabled=True,
                compatible_versions=("1",),
            ),
        )
        for binding in cases:
            with self.subTest(binding=binding.provider_id):
                with self.assertRaises(ProviderConfigurationError) as caught:
                    compose_registry(providers=(binding,))
                self.assertEqual(
                    caught.exception.code,
                    "PARSER.SELECTION.PROVIDER_IDENTITY_MISMATCH",
                )

    def test_provider_exception_is_wrapped_without_leaking_message(self) -> None:
        provider = _RaisingProvider("plugin.raise", "1", ())
        binding = ProviderBinding(
            provider_id="plugin.raise",
            provider=provider,
            enabled=True,
            compatible_versions=("1",),
        )

        with self.assertRaises(ProviderConfigurationError) as caught:
            compose_registry(providers=(binding,))

        self.assertEqual(
            caught.exception.code,
            "PARSER.SELECTION.PROVIDER_FAILED",
        )
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("source", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_provider_order_does_not_change_failure(self) -> None:
        first = ProviderBinding(
            provider_id="plugin.same",
            provider=_Provider("plugin.same", "1", ()),
            enabled=True,
            compatible_versions=("1",),
        )
        second = ProviderBinding(
            provider_id="plugin.same",
            provider=_Provider("plugin.same", "1", ()),
            enabled=True,
            compatible_versions=("1",),
        )
        failures: list[ProviderConfigurationError] = []
        for bindings in ((first, second), (second, first)):
            with self.assertRaises(ProviderConfigurationError) as caught:
                compose_registry(providers=bindings)
            failures.append(caught.exception)
        self.assertEqual(
            failures[0].code,
            "PARSER.SELECTION.PROVIDER_DUPLICATE",
        )
        self.assertEqual(failures[0].safe_summary, failures[1].safe_summary)


class ParserCompositionArchitectureTests(unittest.TestCase):
    def test_real_composition_is_the_only_codec_capable_seam(self) -> None:
        root = Path(__file__).parents[1]
        composition = SourceModule(
            "parser_composition",
            (root / "parser_composition.py").read_text(encoding="utf-8"),
        )
        registry = SourceModule(
            "parser_registry",
            (root / "parser_registry.py").read_text(encoding="utf-8"),
        )
        policy = build_parser_architecture_policy()

        self.assertEqual(policy.check_module(composition), ())
        self.assertEqual(policy.check_module(registry), ())
        self.assertNotIn("importlib", composition.source)
        self.assertNotIn("entry_points", composition.source)
        imported = {
            reference.target
            for reference in collect_import_references(
                composition.source,
                module_name="parser_composition",
            )
        }
        self.assertFalse(
            any(
                target == prefix or target.startswith(prefix + ".")
                for target in imported
                for prefix in PARSER_CODEC_PREFIXES
            )
        )


if __name__ == "__main__":
    unittest.main()
