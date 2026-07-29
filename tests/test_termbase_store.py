from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from editor_contracts import (
    LegacyTermRow,
    PreparedTermMutation,
    TermDraft,
    TermMatchPolicy,
    TermRowKind,
)
from termbase_store import TermbaseStore, TermbaseValidationError


class TermbaseStoreTests(unittest.TestCase):
    store: TermbaseStore = TermbaseStore()
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    path: Path = Path()

    def setUp(self) -> None:
        self.store = TermbaseStore()
        temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir = temp_dir
        self.addCleanup(temp_dir.cleanup)
        self.path = Path(temp_dir.name) / "terms.csv"

    def assert_prepared_artifacts(
        self,
        prepared: PreparedTermMutation,
        original: bytes,
    ) -> None:
        recovery_path = prepared.recovery_path
        self.assertIsNotNone(recovery_path)
        assert recovery_path is not None
        self.assertEqual(prepared.resource_path, self.path)
        self.assertEqual(prepared.base_digest, hashlib.sha256(original).hexdigest())
        self.assertEqual(prepared.staged_path.parent, self.path.parent)
        self.assertEqual(recovery_path.parent, self.path.parent)
        self.assertNotEqual(prepared.staged_path, self.path)
        self.assertNotEqual(recovery_path, self.path)
        self.assertNotEqual(prepared.staged_path, recovery_path)
        self.assertEqual(recovery_path.read_bytes(), original)
        self.assertEqual(
            self.store.list_records(prepared.staged_path),
            prepared.candidate_records,
        )
        self.assertEqual(self.path.read_bytes(), original)

    def staged_rows(self, prepared: PreparedTermMutation) -> list[list[str]]:
        text = prepared.staged_path.read_bytes().decode("utf-8-sig")
        return list(csv.reader(io.StringIO(text, newline=""), strict=True))

    @staticmethod
    def source_stat(path: Path) -> tuple[int, int, int, int]:
        stat = path.stat()
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_mode)

    def test_lists_utf8_sig_mixed_rows_in_file_order_without_changing_bytes(
        self,
    ) -> None:
        legacy_row = 'Legacy source,"旧,译"\r\n'
        v1_row = (
            'localcat-term-v1,term-1,"Configured, source",新译,false,true\r\n'
        )
        original = b"\xef\xbb\xbf" + (legacy_row + v1_row).encode("utf-8")
        self.path.write_bytes(original)

        records = self.store.list_records(self.path)

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.list_records(self.path), records)
        self.assertEqual(
            tuple(record.source for record in records),
            ("Legacy source", "Configured, source"),
        )
        self.assertEqual(tuple(record.target for record in records), ("旧,译", "新译"))
        self.assertEqual(
            tuple(record.locator.row_ordinal for record in records),
            (0, 1),
        )
        file_digest = hashlib.sha256(original).hexdigest()
        self.assertEqual(
            tuple(record.locator.file_digest for record in records),
            (file_digest, file_digest),
        )
        self.assertEqual(
            tuple(record.locator.row_digest for record in records),
            (
                hashlib.sha256(legacy_row.encode("utf-8")).hexdigest(),
                hashlib.sha256(v1_row.encode("utf-8")).hexdigest(),
            ),
        )

        legacy, configured = records
        self.assertIs(legacy.locator.row_kind, TermRowKind.LEGACY)
        self.assertIsNone(legacy.locator.record_id)
        self.assertIsNone(legacy.record_id)
        self.assertIs(legacy.policy, TermMatchPolicy.LEGACY)
        self.assertIsNone(legacy.match_case)
        self.assertIsNone(legacy.whole_word)

        self.assertIs(configured.locator.row_kind, TermRowKind.V1)
        self.assertEqual(configured.locator.record_id, "term-1")
        self.assertEqual(configured.record_id, "term-1")
        self.assertIs(configured.policy, TermMatchPolicy.CONFIGURED)
        self.assertIs(configured.match_case, False)
        self.assertIs(configured.whole_word, True)

    def test_rejects_invalid_mixed_files_atomically_with_structured_errors(
        self,
    ) -> None:
        cases = (
            ("EMPTY_ROW", b"source,target\n\n", 1),
            (
                "UNKNOWN_MARKER",
                b"localcat-term-v2,id,source,target,false,true\n",
                0,
            ),
            (
                "DUPLICATE_SOURCE",
                b"same,one\nlocalcat-term-v1,id-1,same,two,false,true\n",
                1,
            ),
            (
                "DUPLICATE_ID",
                b"localcat-term-v1,id-1,one,first,false,true\n"
                b"localcat-term-v1,id-1,two,second,true,false\n",
                1,
            ),
            (
                "INVALID_BOOLEAN",
                b"localcat-term-v1,id-1,source,target,TRUE,false\n",
                0,
            ),
            (
                "EMPTY_SOURCE",
                b"  ,target\n",
                0,
            ),
            (
                "EMPTY_TARGET",
                b"source,\n",
                0,
            ),
            (
                "EMPTY_RECORD_ID",
                b"localcat-term-v1,,source,target,false,true\n",
                0,
            ),
            (
                "INVALID_COLUMN_COUNT",
                b"source,target,extra\n",
                0,
            ),
            (
                "MALFORMED_CSV",
                b'"unterminated,target\n',
                0,
            ),
            (
                "INVALID_UTF8",
                b"\xffsource,target\n",
                None,
            ),
        )

        for expected_code, original, expected_ordinal in cases:
            with self.subTest(code=expected_code):
                self.path.write_bytes(original)

                with self.assertRaises(TermbaseValidationError) as raised:
                    self.store.list_records(self.path)

                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(raised.exception.row_ordinal, expected_ordinal)
                self.assertEqual(self.path.read_bytes(), original)

    def test_rejects_bare_quotes_in_unquoted_legacy_and_v1_fields(self) -> None:
        cases = (
            (b'source,target"\n', "target"),
            (
                b'localcat-term-v1,id-1,so"urce,target,false,true\n',
                "source",
            ),
        )

        for original, sensitive_text in cases:
            with self.subTest(original=original):
                self.path.write_bytes(original)

                with self.assertRaises(TermbaseValidationError) as raised:
                    self.store.list_records(self.path)

                self.assertEqual(raised.exception.code, "MALFORMED_CSV")
                self.assertEqual(raised.exception.row_ordinal, 0)
                self.assertNotIn(sensitive_text, str(raised.exception))
                self.assertEqual(self.path.read_bytes(), original)

    def test_accepts_quoted_multiline_and_escaped_quote_fields(self) -> None:
        original = (
            b'"legacy\nsource","target ""quoted"""\r\n'
            b'localcat-term-v1,id-2,"v1\nsource","v1 ""target""",true,false\r\n'
        )
        self.path.write_bytes(original)

        records = self.store.list_records(self.path)

        self.assertEqual(
            tuple((record.source, record.target) for record in records),
            (
                ("legacy\nsource", 'target "quoted"'),
                ("v1\nsource", 'v1 "target"'),
            ),
        )
        self.assertEqual(
            tuple(record.locator.row_ordinal for record in records),
            (0, 1),
        )
        self.assertEqual(self.path.read_bytes(), original)

    def test_prepare_create_builds_durable_v1_candidate_without_replacing_source(
        self,
    ) -> None:
        original = b"\xef\xbb\xbfLegacy,\xe6\x97\xa7\xe8\xaf\x91\r\n"
        self.path.write_bytes(original)

        with patch("termbase_store.os.fsync") as fsync:
            prepared = self.store.prepare_create(
                self.path,
                TermDraft(
                    source="New source",
                    target="新译",
                    match_case=True,
                    whole_word=False,
                ),
            )

        self.assertEqual(prepared.action, "create")
        self.assert_prepared_artifacts(prepared, original)
        self.assertEqual(fsync.call_count, 3)
        self.assertEqual(len(prepared.candidate_records), 2)
        created = prepared.candidate_records[-1]
        self.assertIs(created.locator.row_kind, TermRowKind.V1)
        self.assertEqual(uuid.UUID(created.record_id).version, 4)
        self.assertIs(created.match_case, False)
        self.assertIs(created.whole_word, True)
        self.assertEqual(
            self.staged_rows(prepared)[-1],
            [
                "localcat-term-v1",
                created.record_id,
                "New source",
                "新译",
                "false",
                "true",
            ],
        )

    def test_prepare_update_preserves_legacy_shape_and_v1_identity(self) -> None:
        original = (
            b"Legacy,old\n"
            b"localcat-term-v1,term-1,Configured,old,true,false\n"
        )
        self.path.write_bytes(original)
        legacy, configured = self.store.list_records(self.path)

        legacy_prepared = self.store.prepare_update(
            self.path,
            legacy.locator,
            TermDraft(
                source="Legacy renamed",
                target="legacy new",
                match_case=False,
                whole_word=True,
            ),
        )
        self.assert_prepared_artifacts(legacy_prepared, original)
        self.assertEqual(
            self.staged_rows(legacy_prepared)[0],
            ["Legacy renamed", "legacy new"],
        )
        updated_legacy = legacy_prepared.candidate_records[0]
        self.assertIs(updated_legacy.locator.row_kind, TermRowKind.LEGACY)
        self.assertIsNone(updated_legacy.match_case)
        self.assertIsNone(updated_legacy.whole_word)

        configured_prepared = self.store.prepare_update(
            self.path,
            configured.locator,
            TermDraft(
                source="Configured renamed",
                target="configured new",
                match_case=False,
                whole_word=True,
            ),
        )
        self.assert_prepared_artifacts(configured_prepared, original)
        updated_configured = configured_prepared.candidate_records[1]
        self.assertEqual(updated_configured.record_id, "term-1")
        self.assertIs(updated_configured.match_case, False)
        self.assertIs(updated_configured.whole_word, True)
        self.assertEqual(
            self.staged_rows(configured_prepared)[1],
            [
                "localcat-term-v1",
                "term-1",
                "Configured renamed",
                "configured new",
                "false",
                "true",
            ],
        )

    def test_prepare_delete_builds_complete_candidate_without_replacing_source(
        self,
    ) -> None:
        original = (
            b"Keep,one\n"
            b"localcat-term-v1,term-1,Delete,two,false,true\n"
        )
        self.path.write_bytes(original)
        locator = self.store.list_records(self.path)[1].locator

        prepared = self.store.prepare_delete(self.path, locator)

        self.assertEqual(prepared.action, "delete")
        self.assert_prepared_artifacts(prepared, original)
        self.assertEqual(
            tuple(record.source for record in prepared.candidate_records),
            ("Keep",),
        )
        self.assertEqual(self.staged_rows(prepared), [["Keep", "one"]])

    def test_prepare_merge_is_source_lww_and_preserves_existing_identity_and_order(
        self,
    ) -> None:
        original = (
            b"Legacy,old legacy\n"
            b"localcat-term-v1,term-1,Configured,old configured,true,false\n"
            b"Untouched,keep\n"
        )
        self.path.write_bytes(original)
        incoming = (
            LegacyTermRow("New first", "first value", 10),
            LegacyTermRow("Configured", "configured replacement", 11),
            LegacyTermRow("New second", "second value", 12),
            LegacyTermRow("New first", "last value", 13),
            LegacyTermRow("Legacy", "legacy replacement", 14),
        )

        prepared = self.store.prepare_merge_legacy(self.path, incoming)

        self.assertEqual(prepared.action, "merge_legacy")
        self.assert_prepared_artifacts(prepared, original)
        self.assertEqual(
            tuple(
                (
                    record.source,
                    record.target,
                    record.locator.row_kind,
                    record.record_id,
                    record.match_case,
                    record.whole_word,
                )
                for record in prepared.candidate_records
            ),
            (
                ("Legacy", "legacy replacement", TermRowKind.LEGACY, None, None, None),
                (
                    "Configured",
                    "configured replacement",
                    TermRowKind.V1,
                    "term-1",
                    True,
                    False,
                ),
                ("Untouched", "keep", TermRowKind.LEGACY, None, None, None),
                ("New first", "last value", TermRowKind.LEGACY, None, None, None),
                ("New second", "second value", TermRowKind.LEGACY, None, None, None),
            ),
        )

    def test_prepare_rejects_duplicate_create_update_conflict_and_empty_merge(
        self,
    ) -> None:
        original = b"First,one\nSecond,two\n"
        self.path.write_bytes(original)
        first = self.store.list_records(self.path)[0]
        cases = (
            (
                "DUPLICATE_SOURCE",
                lambda: self.store.prepare_create(
                    self.path,
                    TermDraft("First", "replacement"),
                ),
            ),
            (
                "CONFLICTING_SOURCE",
                lambda: self.store.prepare_update(
                    self.path,
                    first.locator,
                    TermDraft("Second", "replacement"),
                ),
            ),
            (
                "EMPTY_IMPORT",
                lambda: self.store.prepare_merge_legacy(self.path, ()),
            ),
        )

        for expected_code, operation in cases:
            with self.subTest(code=expected_code):
                before_names = tuple(sorted(path.name for path in self.path.parent.iterdir()))
                with self.assertRaises(TermbaseValidationError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(
                    tuple(sorted(path.name for path in self.path.parent.iterdir())),
                    before_names,
                )
                self.assertNotIn("First", str(raised.exception))
                self.assertNotIn("Second", str(raised.exception))

    def test_prepare_rejects_stale_locator_without_artifacts(self) -> None:
        original = b"Source,old\n"
        self.path.write_bytes(original)
        stale = self.store.list_records(self.path)[0].locator
        changed = b"Source,new\n"
        self.path.write_bytes(changed)

        for operation in (
            lambda: self.store.prepare_update(
                self.path,
                stale,
                TermDraft("Source", "replacement"),
            ),
            lambda: self.store.prepare_delete(self.path, stale),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(TermbaseValidationError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, "STALE_LOCATOR")
                self.assertEqual(self.path.read_bytes(), changed)
                self.assertEqual(
                    tuple(self.path.parent.iterdir()),
                    (self.path,),
                )

    def test_prepare_failure_cleans_temporary_artifacts_and_preserves_source(
        self,
    ) -> None:
        original = b"Source,target\n"
        self.path.write_bytes(original)

        with patch(
            "termbase_store.os.fsync",
            side_effect=(None, None, OSError("injected"), None),
        ):
            with self.assertRaises(OSError):
                self.store.prepare_create(
                    self.path,
                    TermDraft("New", "value"),
                )

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(tuple(self.path.parent.iterdir()), (self.path,))

    def test_prepare_accepts_relative_resource_path_without_leaking_artifacts(
        self,
    ) -> None:
        original = b"Source,target\n"
        self.path.write_bytes(original)
        before_stat = self.source_stat(self.path)
        relative_path = Path(os.path.relpath(self.path, Path.cwd()))

        prepared = self.store.prepare_create(
            relative_path,
            TermDraft("New", "value"),
        )

        recovery_path = prepared.recovery_path
        self.assertIsNotNone(recovery_path)
        assert recovery_path is not None
        self.assertTrue(prepared.resource_path.is_absolute())
        self.assertTrue(prepared.staged_path.is_absolute())
        self.assertTrue(recovery_path.is_absolute())
        self.assertEqual(prepared.resource_path, self.path)
        self.assert_prepared_artifacts(prepared, original)
        self.assertEqual(self.source_stat(self.path), before_stat)

    def test_prepare_contract_construction_failure_cleans_durable_artifacts(
        self,
    ) -> None:
        original = b"Source,target\n"
        self.path.write_bytes(original)
        before_stat = self.source_stat(self.path)

        with patch(
            "termbase_store.PreparedTermMutation",
            side_effect=RuntimeError("injected contract failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected contract failure"):
                self.store.prepare_create(
                    self.path,
                    TermDraft("New", "value"),
                )

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.source_stat(self.path), before_stat)
        self.assertEqual(tuple(self.path.parent.iterdir()), (self.path,))


if __name__ == "__main__":
    unittest.main()
