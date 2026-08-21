"""Task 6.1 canonical import seam tests.

The same TMX batch must behave differently by activation state: an
activated resource receives validated ordered units directly in canonical
storage (same-source variants retained, no JSONL mutation, no divergence
triggered), while a not-yet-activated resource keeps the existing atomic
JSONL last-write-wins merge unchanged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from parser_composition import OpenedParserInput
from resource_importer import import_tmx
from tm_contracts import SourceBindingState
from tm_engine import open_canonical_tm_store
from tm_sqlite_store import SQLiteStoreLifecycleError, SQLiteTMStore
from tests.test_tm_activation_journal import _first_prepared


def _activate_resource(root: Path) -> Path:
    """Fully activate one first-generation canonical resource and return
    its configured JSONL path (bytes stay equal to the seeded SOURCE_BYTES).
    """

    identity, coordinator, _sealed, prepared, journal = _first_prepared(
        root,
        fts5_available=True,
    )
    with patch("tm_sqlite_store._probe_fts5", return_value=True):
        coordinator.publish_activation(prepared, journal)
    return identity.configured_jsonl_path


def _tmx_units(*units: tuple[str, str]) -> str:
    return "".join(
        f'<tu><tuv xml:lang="en-US"><seg>{source}</seg></tuv>'
        f'<tuv xml:lang="zh-CN"><seg>{target}</seg></tuv></tu>'
        for source, target in units
    )


def _store_for(target: Path):
    store = open_canonical_tm_store(target)
    assert store is not None
    return store


class CanonicalImportSeamTests(unittest.TestCase):
    def _write_tmx(self, path: Path, body: str) -> None:
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<tmx version="1.4"><header srclang="en-US"/><body>'
            f"{body}</body></tmx>",
            encoding="utf-8",
        )

    def test_activated_import_keeps_jsonl_and_input_order_with_variants(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = _activate_resource(root)
            original = target.read_bytes()
            source = root / "batch.tmx"
            self._write_tmx(
                source,
                _tmx_units(
                    ("Alpha", "甲"),
                    ("Beta", "乙"),
                    ("Alpha", "甲2"),
                ),
            )
            store = _store_for(target)
            self.assertEqual(store.canonical_revision().record_count, 3)

            report = import_tmx(source, target, "en-US", "zh-CN")

            self.assertTrue(report.succeeded)
            self.assertEqual(report.imported, 3)
            self.assertEqual(report.skipped, 0)
            self.assertEqual(report.overwritten, 0)
            self.assertEqual(target.read_bytes(), original)

            # The live generation lease observes the import in input
            # order; a fresh process rehydrates the same canonical
            # lineage and the completed snapshot binding is historical
            # (VERIFIED_HISTORY), never activation damage.
            snapshot = store.capture_export_snapshot()
            self.assertEqual(snapshot.revision.record_count, 6)
            self.assertEqual(
                [
                    (item.record.source_raw, item.record.target_raw)
                    for item in snapshot.records[3:]
                ],
                [("Alpha", "甲"), ("Beta", "乙"), ("Alpha", "甲2")],
            )
            self.assertEqual(
                [record.target_raw for record in store.exact_records("Alpha")],
                ["甲2", "甲"],
            )
            self.assertEqual(store.exact_records("Beta")[0].target_raw, "乙")

            reopened = open_canonical_tm_store(target)
            assert reopened is not None
            self.assertEqual(
                reopened.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_HISTORY,
            )
            self.assertEqual(reopened.canonical_revision().record_count, 6)
            self.assertEqual(
                [
                    (item.record.source_raw, item.record.target_raw)
                    for item in reopened.capture_export_snapshot().records[3:]
                ],
                [("Alpha", "甲"), ("Beta", "乙"), ("Alpha", "甲2")],
            )
            self.assertEqual(
                [record.target_raw for record in reopened.exact_records("Alpha")],
                ["甲2", "甲"],
            )

    def test_activated_import_does_not_touch_binding_or_trigger_divergence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = _activate_resource(root)
            source = root / "batch.tmx"
            self._write_tmx(source, _tmx_units(("Alpha", "甲")))
            store = _store_for(target)
            original = target.read_bytes()

            report = import_tmx(source, target, "en-US", "zh-CN")
            observation = store.source_binding_monitor.observe()

            self.assertTrue(report.succeeded)
            self.assertEqual(
                observation.state,
                SourceBindingState.VERIFIED_HISTORY,
            )
            self.assertEqual(target.read_bytes(), original)

    def test_identical_digest_reimport_fails_closed_without_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = _activate_resource(root)
            original = target.read_bytes()
            source = root / "batch.tmx"
            self._write_tmx(source, _tmx_units(("Alpha", "甲")))
            store = _store_for(target)

            first = import_tmx(source, target, "en-US", "zh-CN")
            count_after_first = store.canonical_revision().record_count
            second = import_tmx(source, target, "en-US", "zh-CN")

            self.assertTrue(first.succeeded)
            self.assertEqual(count_after_first, 4)
            self.assertFalse(second.succeeded)
            self.assertEqual(second.imported, 0)
            self.assertTrue(
                any("already applied" in error for error in second.errors)
            )
            self.assertEqual(target.read_bytes(), original)
            # The repeat attempt is deterministic and adds no duplicates;
            # a fresh cold open still rehydrates the same canonical
            # lineage with the completed binding as VERIFIED_HISTORY.
            reopened = open_canonical_tm_store(target)
            assert reopened is not None
            self.assertEqual(
                reopened.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_HISTORY,
            )
            self.assertEqual(reopened.canonical_revision().record_count, 4)
            self.assertEqual(
                len(reopened.exact_records("Alpha")),
                1,
            )

    def test_import_records_digest_and_receipt_share_one_input_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = _activate_resource(root)
            source = root / "origin-batch.tmx"
            self._write_tmx(source, _tmx_units(("Alpha", "甲")))
            original = source.read_bytes()
            expected_digest = hashlib.sha256(original).hexdigest()
            expected_receipt_source = source.resolve()
            replacement = root / "replacement-name.tmx"
            self._write_tmx(replacement, _tmx_units(("Beta", "乙")))

            original_stream = OpenedParserInput.stream

            def stream_then_replace(opened: OpenedParserInput):
                source.unlink()
                source.symlink_to(replacement)
                return original_stream(opened)

            with patch(
                "parser_composition.OpenedParserInput.stream",
                autospec=True,
                side_effect=stream_then_replace,
            ):
                first = import_tmx(source, target, "en-US", "zh-CN")

            self.assertTrue(first.succeeded)
            store = _store_for(target)
            self.assertEqual(store.exact_records("Alpha")[0].target_raw, "甲")
            self.assertFalse(store.exact_records("Beta"))
            imported = store.capture_export_snapshot().records[3].record
            self.assertEqual(imported.file_source, "origin-batch.tmx")
            self.assertEqual(
                imported.provenance,
                (
                    ("source", "tmx-import"),
                    ("file", "origin-batch.tmx"),
                ),
            )
            with sqlite3.connect(
                target.with_name(f"{target.name}.sqlite3")
            ) as connection:
                origin = connection.execute(
                    "SELECT source_digest, source_path "
                    "FROM tm_origin_batch WHERE kind = 'import'"
                ).fetchone()
            self.assertEqual(
                origin,
                (expected_digest, str(expected_receipt_source)),
            )
            self.assertTrue(source.is_symlink())
            self.assertEqual(source.resolve(), replacement.resolve())

            # Restoring the exact captured bytes must hit the same origin
            # digest.  If digesting or receipt provenance had re-read the
            # replaced path, this second call would incorrectly create a
            # duplicate Alpha row or bind the receipt to the replacement.
            source.unlink()
            source.write_bytes(original)
            second = import_tmx(source, target, "en-US", "zh-CN")
            self.assertFalse(second.succeeded)
            self.assertTrue(
                any("already applied" in error for error in second.errors)
            )
            reopened = _store_for(target)
            self.assertEqual(reopened.canonical_revision().record_count, 4)
            self.assertEqual(len(reopened.exact_records("Alpha")), 1)

    def test_import_sql_failure_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = _activate_resource(root)
            original = target.read_bytes()
            source = root / "batch.tmx"
            self._write_tmx(source, _tmx_units(("Alpha", "甲")))
            store = _store_for(target)
            failure = SQLiteStoreLifecycleError(
                "STORE.TEST_BATCH_FAILURE",
                resource_id="tm.primary",
                generation=0,
                retryable=False,
            )

            with patch.object(
                SQLiteTMStore,
                "append_batch",
                side_effect=failure,
            ):
                report = import_tmx(source, target, "en-US", "zh-CN")

            self.assertFalse(report.succeeded)
            self.assertTrue(any(report.errors))
            self.assertEqual(
                store.canonical_revision().record_count,
                3,
            )
            self.assertEqual(target.read_bytes(), original)

    def test_non_duplicate_integrity_failure_is_not_misreported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = _activate_resource(root)
            original = target.read_bytes()
            source = root / "batch.tmx"
            self._write_tmx(source, _tmx_units(("Alpha", "甲")))
            store = _store_for(target)

            with patch.object(
                SQLiteTMStore,
                "append_batch",
                side_effect=sqlite3.IntegrityError(
                    "CHECK constraint failed: valid_count"
                ),
            ):
                report = import_tmx(source, target, "en-US", "zh-CN")

            self.assertFalse(report.succeeded)
            self.assertEqual(
                report.errors,
                ("canonical import transaction constraint failed",),
            )
            self.assertEqual(store.canonical_revision().record_count, 3)
            self.assertEqual(target.read_bytes(), original)

    def test_generic_sqlite_failure_is_body_safe_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = _activate_resource(root)
            original = target.read_bytes()
            source = root / "batch.tmx"
            self._write_tmx(source, _tmx_units(("Alpha", "甲")))
            store = _store_for(target)

            with patch.object(
                SQLiteTMStore,
                "append_batch",
                side_effect=sqlite3.OperationalError(
                    "SECRET database body"
                ),
            ):
                report = import_tmx(source, target, "en-US", "zh-CN")

            self.assertFalse(report.succeeded)
            self.assertEqual(report.imported, 0)
            self.assertEqual(report.skipped, 0)
            self.assertEqual(report.overwritten, 0)
            self.assertEqual(
                report.errors,
                ("canonical import transaction failed",),
            )
            self.assertNotIn("SECRET", " ".join(report.errors))
            self.assertEqual(store.canonical_revision().record_count, 3)
            self.assertEqual(target.read_bytes(), original)

    def test_unactivated_import_keeps_legacy_last_write_wins_folding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "legacy.jsonl"
            target.write_text("", encoding="utf-8")
            source = root / "batch.tmx"
            self._write_tmx(
                source,
                _tmx_units(
                    ("Alpha", "甲"),
                    ("Beta", "乙"),
                    ("Alpha", "甲2"),
                ),
            )

            report = import_tmx(source, target, "en-US", "zh-CN")

            self.assertTrue(report.succeeded)
            self.assertEqual(report.imported, 2)
            self.assertEqual(report.overwritten, 1)
            records = {
                json.loads(line)["source"]: json.loads(line)["target"]
                for line in target.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            self.assertEqual(
                records,
                {"Alpha": "甲2", "Beta": "乙"},
            )
            self.assertIsNone(open_canonical_tm_store(target))


if __name__ == "__main__":
    unittest.main()
