from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from editor_contracts import TermCommitState
from termbase_store import TermbaseStore, TermbaseValidationError


_MIXED = (
    b"\xef\xbb\xbfsource,target\n"
    b'localcat-term-v1,id-1,"quoted, source","line one\nline two",true,false\n'
)


class TermbasePortableSnapshotTests(unittest.TestCase):
    def test_export_validate_and_full_replace_preserve_mixed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.csv"
            exported = root / "exported.csv"
            destination = root / "destination.csv"
            source.write_bytes(_MIXED)
            destination.write_bytes(b"\xef\xbb\xbfother,value\n")
            store = TermbaseStore()

            facts = store.export_portable_snapshot(source, exported)
            self.assertEqual(store.validate_portable_snapshot(exported), facts)
            self.assertEqual((facts.record_count, facts.legacy_record_count, facts.v1_record_count), (2, 1, 1))

            prepared = store.prepare_snapshot_replace(destination, exported)
            outcome = store.commit(prepared)
            self.assertIs(outcome.state, TermCommitState.COMMITTED)
            self.assertEqual(destination.read_bytes(), exported.read_bytes())
            self.assertEqual(store.list_records(destination), store.list_records(exported))
            cleanup = store.finalize(prepared, outcome)
            self.assertTrue(cleanup.cleaned)

    def test_noncanonical_snapshot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "terms.csv"
            path.write_bytes(b"source,target\r\n")
            with self.assertRaises(TermbaseValidationError) as caught:
                TermbaseStore().validate_portable_snapshot(path)
            self.assertEqual(caught.exception.code, "NON_CANONICAL_SNAPSHOT")


if __name__ == "__main__":
    unittest.main()
