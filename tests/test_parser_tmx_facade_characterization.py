"""Characterization guard for the pre-rebaseline TMX import facade.

These tests deliberately freeze Application/Store observations rather than
the private XML tokenizer.  The parser rebaseline may replace that tokenizer,
but it must keep the two import lanes, their transaction boundary, and the
``ImportReport`` mapping until the compatibility contract is versioned.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resource_importer import import_tmx
from tm_contracts import CanonicalResourceIdentity
from tm_engine import open_canonical_tm_store
from tests.test_tm_activation_journal import _first_prepared


def _unit(*variants: tuple[str, str]) -> str:
    rendered = "".join(
        f'<tuv xml:lang="{locale}"><seg>{text}</seg></tuv>'
        for locale, text in variants
    )
    return f"<tu>{rendered}</tu>"


def _tmx(*units: str) -> bytes:
    body = "".join(units)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<tmx version="1.4"><header srclang="en-US"/><body>'
        f"{body}</body></tmx>"
    ).encode("utf-8")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _activate(root: Path) -> CanonicalResourceIdentity:
    identity, coordinator, _sealed, prepared, journal = _first_prepared(
        root,
        fts5_available=True,
    )
    with patch("tm_sqlite_store._probe_fts5", return_value=True):
        coordinator.publish_activation(prepared, journal)
    return identity


class TMXFacadeCharacterizationTests(unittest.TestCase):
    def test_legacy_lane_prefers_exact_locale_then_unambiguous_base_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "locale-and-variants.tmx"
            target = root / "legacy.jsonl"
            target.write_text(
                "".join(
                    (
                        json.dumps(
                            {"source": "Stable", "target": "保留"},
                            ensure_ascii=False,
                        )
                        + "\n",
                        json.dumps(
                            {"source": "Exact", "target": "旧译"},
                            ensure_ascii=False,
                        )
                        + "\n",
                    )
                ),
                encoding="utf-8",
            )
            source.write_bytes(
                _tmx(
                    _unit(
                        ("en-GB", "Wrong exact source"),
                        ("en_US", "Exact"),
                        ("zh-Hans", "错误的精确译文"),
                        ("zh_CN", "精确译文"),
                    ),
                    _unit(("en-GB", "Fallback"), ("zh-Hans", "回退译文")),
                    _unit(("en-US", "Variant"), ("zh-CN", "变体一")),
                    _unit(("en-US", "Variant"), ("zh-CN", "变体二")),
                )
            )

            report = import_tmx(source, target, " EN_us ", "zh_cn")
            records = _read_jsonl(target)

        self.assertTrue(report.succeeded)
        self.assertEqual(
            (report.imported, report.skipped, report.overwritten, report.errors),
            (3, 0, 2, ()),
        )
        self.assertEqual(
            [(record["source"], record["target"]) for record in records],
            [
                ("Stable", "保留"),
                ("Exact", "精确译文"),
                ("Fallback", "回退译文"),
                ("Variant", "变体二"),
            ],
        )

    def test_canonical_lane_commits_ordered_variants_and_records_origin_digest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _activate(root)
            target = identity.configured_jsonl_path
            original_target = target.read_bytes()
            source = root / "canonical-batch.tmx"
            source.write_bytes(
                _tmx(
                    _unit(("en-US", "Alpha"), ("zh-CN", "甲")),
                    _unit(("en-US", "Beta"), ("zh-CN", "乙")),
                    _unit(("en-US", "Alpha"), ("zh-CN", "甲二")),
                    (
                        '<tu><tuv xml:lang="en-US"><seg>Tagged <ph id="1"/>'
                        '</seg></tuv><tuv xml:lang="zh-CN"><seg>标签</seg>'
                        "</tuv></tu>"
                    ),
                )
            )
            source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

            report = import_tmx(source, target, "en-US", "zh-CN")
            store = open_canonical_tm_store(target)
            self.assertIsNotNone(store)
            assert store is not None
            imported_records = store.capture_export_snapshot().records[3:]
            with sqlite3.connect(identity.canonical_sidecar_path) as connection:
                origin_rows = connection.execute(
                    "SELECT status, source_digest, source_path, valid_count, "
                    "invalid_count, duplicate_source_count, completed_revision "
                    "FROM tm_origin_batch WHERE kind = 'import'"
                ).fetchall()

            self.assertEqual(target.read_bytes(), original_target)

        # Record warnings are errors in the compatibility receipt even though
        # verified valid units have committed atomically to canonical storage.
        self.assertFalse(report.succeeded)
        self.assertEqual(report.imported, 3)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.overwritten, 0)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("inline XML", report.errors[0])
        self.assertEqual(
            [
                (item.record.source_raw, item.record.target_raw)
                for item in imported_records
            ],
            [("Alpha", "甲"), ("Beta", "乙"), ("Alpha", "甲二")],
        )
        self.assertEqual(
            origin_rows,
            [
                (
                    "completed",
                    source_digest,
                    str(source.resolve()),
                    3,
                    1,
                    1,
                    2,
                )
            ],
        )

    def test_no_valid_pair_returns_error_only_without_committing_either_lane(
        self,
    ) -> None:
        payload = _tmx(_unit(("fr-FR", "Bonjour"), ("de-DE", "Hallo")))
        self._assert_failure_is_non_committing(payload)

    def test_malformed_input_returns_error_only_without_committing_either_lane(
        self,
    ) -> None:
        self._assert_failure_is_non_committing(b"<tmx><body><tu>")

    def _assert_failure_is_non_committing(self, payload: bytes) -> None:
        for canonical in (False, True):
            with self.subTest(canonical=canonical), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "failed.tmx"
                source.write_bytes(payload)
                if canonical:
                    identity = _activate(root)
                    target = identity.configured_jsonl_path
                    sidecar = identity.canonical_sidecar_path
                    with sqlite3.connect(sidecar) as connection:
                        prior_count = connection.execute(
                            "SELECT COUNT(*) FROM tm_record"
                        ).fetchone()
                else:
                    target = root / "legacy.jsonl"
                    target.write_text(
                        '{"source":"Keep","target":"保留"}\n',
                        encoding="utf-8",
                    )
                    prior_count = None
                    sidecar = None
                original_target = target.read_bytes()

                report = import_tmx(source, target, "en-US", "zh-CN")

                self.assertFalse(report.succeeded)
                self.assertEqual(
                    (report.imported, report.skipped, report.overwritten),
                    (0, 0, 0),
                )
                self.assertTrue(report.errors)
                self.assertEqual(target.read_bytes(), original_target)
                if canonical:
                    assert sidecar is not None
                    with sqlite3.connect(sidecar) as connection:
                        record_count = connection.execute(
                            "SELECT COUNT(*) FROM tm_record"
                        ).fetchone()
                        import_count = connection.execute(
                            "SELECT COUNT(*) FROM tm_origin_batch "
                            "WHERE kind = 'import'"
                        ).fetchone()
                    self.assertEqual(record_count, prior_count)
                    self.assertEqual(import_count, (0,))


if __name__ == "__main__":
    unittest.main()
