from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from editor_contracts import TermMatchPolicy, TermRowKind
from termbase_store import TermbaseStore, TermbaseValidationError


class TermbaseStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TermbaseStore()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "terms.csv"

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


if __name__ == "__main__":
    unittest.main()
