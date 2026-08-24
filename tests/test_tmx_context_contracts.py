from __future__ import annotations

import hashlib
from pathlib import Path
import pickle
import unittest

from tmx_context_contracts import (
    TMX_CONTEXT_PROFILE_ID,
    TmxCarrierKind,
    TmxDestinationBeforeKind,
    TmxDirectPlan,
    TmxEffectiveLocales,
    TmxExportPreview,
    TmxExportUnit,
    TmxLossCount,
    TmxLossDisposition,
    TmxOrderedProp,
    TmxPreparedPayload,
    TmxPropScope,
    TmxScopeBinding,
    TmxScopeKind,
)


class TmxContextContractTests(unittest.TestCase):
    def test_three_axes_are_independent_and_und_is_not_effective(self) -> None:
        self.assertEqual(TmxScopeKind.SELECTED_CHUNK.value, "selected_chunk")
        self.assertEqual(TmxCarrierKind.DIRECT.value, "direct")
        self.assertEqual(TMX_CONTEXT_PROFILE_ID, "localcat-tmx-level1-context-v1")
        self.assertEqual(TmxEffectiveLocales("en-US", "zh-CN").target_locale, "zh-CN")
        for locales in (("und", "zh-CN"), ("en", "en"), (" en", "zh-CN")):
            with self.subTest(locales=locales), self.assertRaises(ValueError):
                TmxEffectiveLocales(*locales)

    def test_selected_chunk_binding_requires_exact_plan_facts(self) -> None:
        digest = hashlib.sha256(b"binding").hexdigest()
        with self.assertRaises(ValueError):
            TmxScopeBinding(TmxScopeKind.SELECTED_CHUNK, "chunk", digest, 1)
        binding = TmxScopeBinding(
            TmxScopeKind.SELECTED_CHUNK,
            "chunk",
            digest,
            1,
            project_id="project",
            chunk_plan_id="plan",
            chunk_plan_revision=3,
            chunk_id="chunk",
            document_count=1,
            attached_count=1,
        )
        self.assertEqual(binding.chunk_plan_revision, 3)

    def test_ordered_duplicate_props_keep_scope_language_and_value(self) -> None:
        props = (
            TmxOrderedProp("vendor", "one", "en-US", TmxPropScope.SOURCE_TUV),
            TmxOrderedProp("vendor", "two", "zh-CN", TmxPropScope.TARGET_TUV),
        )
        unit = TmxExportUnit("doc:1", "source", "target", True, imported_props=props)
        self.assertEqual(unit.imported_props, props)
        self.assertEqual([prop.type for prop in unit.imported_props], ["vendor", "vendor"])

    def test_public_preview_is_body_safe_and_private_plan_is_not_serializable(self) -> None:
        from tmx_context_interchange import prepare_tmx_payload

        digest = hashlib.sha256(b"binding").hexdigest()
        binding = TmxScopeBinding(
            TmxScopeKind.MANAGED_RESOURCE,
            "resource-id",
            digest,
            1,
            attached_count=1,
        )
        payload = prepare_tmx_payload(
            binding,
            TmxEffectiveLocales("en", "zh-CN"),
            (TmxExportUnit("record:1", "secret source", "secret target", True),),
        )
        preview = TmxExportPreview(
            operation_id="op",
            scope_kind=binding.scope_kind,
            scope_id=binding.scope_id,
            project_id=None,
            chunk_plan_id=None,
            chunk_plan_revision=None,
            chunk_id=None,
            document_count=0,
            attached_count=1,
            included_count=1,
            excluded_count=0,
            warning_count=0,
            loss_counts=(),
            safe_issues=(),
            effective_locales=payload.proof.effective_locales,
            profile_id=payload.proof.profile_id,
            destination=Path("/tmp/export.tmx"),
            destination_before=TmxDestinationBeforeKind.ABSENT,
            destination_before_digest=None,
        )
        self.assertNotIn("secret", repr(preview))
        plan = TmxDirectPlan(
            operation_id="op",
            payload=payload,
            binding=binding,
            preview=preview,
            destination_fact=object(),
        )
        self.assertNotIn("secret", repr(plan))
        with self.assertRaises(TypeError):
            pickle.dumps(plan)

    def test_loss_count_is_stable_and_positive(self) -> None:
        count = TmxLossCount("empty_target", TmxLossDisposition.EXCLUDED, 2)
        self.assertEqual(count.count, 2)
        with self.assertRaises(ValueError):
            TmxLossCount("EMPTY TARGET", TmxLossDisposition.EXCLUDED, 1)


if __name__ == "__main__":
    unittest.main()
