"""Whole-wheel proof and source-only selection for the complete W3 producer."""
import base64
import csv
import hashlib
import io
import unittest
import zipfile

from tools.windows_frozen_producer_inputs import _source_wheel_members


def wheel(members, *, corrupt_record=False):
    rows = []
    for name, content in members:
        digest = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        rows.append([name, digest, str(len(content))])
    if corrupt_record:
        rows[-1][1] = "sha256=untrusted"
    rows.append(["package.dist-info/RECORD", "", ""])
    record = io.StringIO()
    csv.writer(record, lineterminator="\n").writerows(rows)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in members:
            archive.writestr(name, content)
        archive.writestr("package.dist-info/RECORD", record.getvalue())
    return output.getvalue()


class ProducerWheelInputTests(unittest.TestCase):
    def test_exact_sources_remain_and_every_uncollected_bytecode_has_provenance(self):
        members = [("PySide6/__init__.py", b"source"),
                   ("PySide6/support/__pycache__/__init__.cpython-310.pyc", b"not executable"),
                   ("PySide6/other.PYO", b"also not executable")]
        content = wheel(members)
        provenance = {}
        selected = _source_wheel_members(content, "vendor.whl", provenance=provenance)
        self.assertEqual(selected["PySide6/__init__.py"], b"source")
        self.assertNotIn(members[1][0], selected)
        self.assertNotIn(members[2][0], selected)
        self.assertEqual(set(provenance["vendor.whl"]["excluded_members"]), {name for name, _ in members[1:]})
        self.assertEqual(provenance["vendor.whl"]["verified_record_members"], 4)
        self.assertEqual(provenance["vendor.whl"]["wheel"]["sha256"], hashlib.sha256(content).hexdigest())

    def test_even_uncollected_bytecode_must_match_original_record(self):
        content = wheel([("PySide6/__init__.py", b"source"), ("PySide6/cached.pyc", b"untrusted")], corrupt_record=True)
        with self.assertRaisesRegex(ValueError, "RECORD mismatch"):
            _source_wheel_members(content, "vendor.whl")

    def test_case_alias_traversal_and_absolute_members_are_rejected(self):
        for members in ([("package.py", b"a"), ("PACKAGE.py", b"a")],
                        [("../escape.pyc", b"a")], [("/absolute.py", b"a")], [("C:/absolute.py", b"a")]):
            with self.subTest(members=members), self.assertRaises(ValueError):
                _source_wheel_members(wheel(members), "vendor.whl")


if __name__ == "__main__":
    unittest.main()
