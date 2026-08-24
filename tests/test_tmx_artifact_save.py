from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from tmx_artifact_save import TmxDirectArtifactSaver
from tmx_context_contracts import (
    TmxContextError,
    TmxDestinationBeforeKind,
    TmxEffectiveLocales,
    TmxExportUnit,
    TmxScopeBinding,
    TmxScopeKind,
)
from tmx_context_interchange import ParserTmxColdValidator, prepare_tmx_payload


class TmxArtifactSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.binding = TmxScopeBinding(
            TmxScopeKind.MANAGED_RESOURCE,
            "resource-1",
            hashlib.sha256(b"resource-generation-revision").hexdigest(),
            1,
            attached_count=1,
        )
        self.payload = prepare_tmx_payload(
            self.binding,
            TmxEffectiveLocales("en-US", "zh-CN"),
            (TmxExportUnit("record-1", "source", "target", True),),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def saver(self, validator=None, scope=None) -> TmxDirectArtifactSaver:
        return TmxDirectArtifactSaver(
            validator or ParserTmxColdValidator(),
            scope or (lambda binding: self.assertEqual(binding, self.binding)),
        )

    def test_preview_cancel_is_zero_mutation_and_plan_is_single_use(self) -> None:
        destination = self.root / "cancel.tmx"
        saver = self.saver()
        preview, plan = saver.preview(self.binding, self.payload, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(preview.destination_before, TmxDestinationBeforeKind.ABSENT)
        self.assertEqual(tuple(self.root.iterdir()), ())
        saver.cancel(plan)
        self.assertEqual(tuple(self.root.iterdir()), ())
        with self.assertRaises(TmxContextError) as captured:
            saver.apply(plan)
        self.assertEqual(captured.exception.code, "TMX.PLAN_INVALID")

    def test_absent_and_existing_destination_publish_durable_receipts(self) -> None:
        destination = self.root / "resource.tmx"
        saver = self.saver()
        preview, plan = saver.preview(self.binding, self.payload, destination)
        receipt = saver.apply(plan)
        self.assertEqual(preview.destination_before, TmxDestinationBeforeKind.ABSENT)
        self.assertTrue(receipt.durable)
        self.assertEqual(destination.read_bytes(), self.payload.data)
        self.assertEqual(receipt.after_digest, hashlib.sha256(destination.read_bytes()).hexdigest())
        self.assertEqual(tuple(path.name for path in self.root.iterdir()), ("resource.tmx",))

        prior = destination.read_bytes()
        replacement = prepare_tmx_payload(
            self.binding,
            TmxEffectiveLocales("en-US", "zh-CN"),
            (TmxExportUnit("record-1", "new source", "new target", False),),
        )
        preview2, plan2 = saver.preview(self.binding, replacement, destination)
        receipt2 = saver.apply(plan2)
        self.assertEqual(preview2.destination_before, TmxDestinationBeforeKind.REGULAR)
        self.assertEqual(receipt2.before_digest, hashlib.sha256(prior).hexdigest())
        self.assertEqual(destination.read_bytes(), replacement.data)
        self.assertEqual(tuple(path.name for path in self.root.iterdir()), ("resource.tmx",))

    def test_stale_destination_and_scope_fail_before_candidate(self) -> None:
        destination = self.root / "stale.tmx"
        destination.write_bytes(b"prior")
        saver = self.saver()
        _preview, plan = saver.preview(self.binding, self.payload, destination)
        destination.write_bytes(b"external")
        before = destination.read_bytes()
        with self.assertRaises(TmxContextError) as captured:
            saver.apply(plan)
        self.assertEqual(captured.exception.code, "TMX.DESTINATION_STALE")
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(tuple(path.name for path in self.root.iterdir()), ("stale.tmx",))

        destination2 = self.root / "scope.tmx"
        error = TmxContextError("TMX.SCOPE_STALE", "scope changed")
        saver2 = self.saver(scope=lambda _binding: (_ for _ in ()).throw(error))
        _preview2, plan2 = saver2.preview(self.binding, self.payload, destination2)
        with self.assertRaises(TmxContextError) as captured2:
            saver2.apply(plan2)
        self.assertEqual(captured2.exception.code, "TMX.SCOPE_STALE")
        self.assertFalse(destination2.exists())
        self.assertEqual(set(path.name for path in self.root.iterdir()), {"stale.tmx"})

        destination3 = self.root / "scope-false.tmx"
        saver3 = self.saver(scope=lambda _binding: False)
        _preview3, plan3 = saver3.preview(self.binding, self.payload, destination3)
        with self.assertRaises(TmxContextError) as captured3:
            saver3.apply(plan3)
        self.assertEqual(captured3.exception.code, "TMX.SCOPE_STALE")
        self.assertFalse(destination3.exists())

    def test_candidate_validation_failure_preserves_prior_exact_bytes(self) -> None:
        destination = self.root / "prior.tmx"
        destination.write_bytes(b"exact prior bytes")
        before = destination.read_bytes()

        def reject(_path, _proof):
            raise TmxContextError("TMX.COLD_VALIDATION_FAILED", "rejected")

        saver = self.saver(validator=reject)
        _preview, plan = saver.preview(self.binding, self.payload, destination)
        with self.assertRaises(TmxContextError) as captured:
            saver.apply(plan)
        self.assertEqual(captured.exception.code, "TMX.COLD_VALIDATION_FAILED")
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(tuple(path.name for path in self.root.iterdir()), ("prior.tmx",))

    def test_post_publication_readback_failure_rolls_back_only_our_candidate(self) -> None:
        destination = self.root / "rollback.tmx"
        destination.write_bytes(b"prior")
        calls = 0

        def fail_readback(path, proof):
            nonlocal calls
            calls += 1
            ParserTmxColdValidator()(path, proof)
            if calls == 2:
                raise TmxContextError("TMX.TEST_READBACK", "readback rejected")

        saver = self.saver(validator=fail_readback)
        _preview, plan = saver.preview(self.binding, self.payload, destination)
        with self.assertRaises(TmxContextError) as captured:
            saver.apply(plan)
        self.assertEqual(captured.exception.code, "TMX.POST_PUBLICATION_ROLLED_BACK")
        self.assertEqual(destination.read_bytes(), b"prior")
        self.assertEqual(tuple(path.name for path in self.root.iterdir()), ("rollback.tmx",))

    def test_symlink_hardlink_and_special_destinations_are_rejected_at_preview(self) -> None:
        original = self.root / "original.tmx"
        original.write_bytes(b"prior")
        symlink = self.root / "link.tmx"
        symlink.symlink_to(original)
        hardlink = self.root / "hard.tmx"
        os.link(original, hardlink)
        directory = self.root / "directory.tmx"
        directory.mkdir()
        saver = self.saver()
        for destination in (symlink, hardlink, directory):
            with self.subTest(destination=destination.name), self.assertRaises(TmxContextError) as captured:
                saver.preview(self.binding, self.payload, destination)
            self.assertEqual(captured.exception.code, "TMX.DESTINATION_UNSAFE")
        self.assertEqual(original.read_bytes(), b"prior")


if __name__ == "__main__":
    unittest.main()
