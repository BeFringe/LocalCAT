"""Mechanical guard for the synthetic CSV/XLSX termbase golden corpus.

Wave 0 freezes distributable bytes, recipes, selections, and expected contract
observations.  It deliberately does not import or exercise a production codec;
later waves consume this same corpus when the Parser runtime exists.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
import xml.parsers.expat
import zipfile


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "parser" / "termbase"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"

FORMAT_IDS = {"termbase-csv-v1", "termbase-xlsx-v1"}
REQUIRED_AXES = {
    "valid",
    "format_boundary",
    "record_warning",
    "encoding",
    "limit",
    "cancel",
}
TERMINALS = {"success", "success_with_warnings", "fatal", "cancelled"}
_XML_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XML_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _zip_info(name: str, compression: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _xml_text(value: object) -> str:
    from xml.sax.saxutils import escape

    return escape(str(value), {'"': "&quot;"})


def _column_name(index: int) -> str:
    result = ""
    current = index + 1
    while current:
        current, remainder = divmod(current - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _worksheet_xml(rows: list[list[object | None]]) -> bytes:
    xml_rows: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column_number, value in enumerate(row):
            if value is None:
                continue
            reference = f"{_column_name(column_number)}{row_number}"
            cells.append(
                f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">'
                f"{_xml_text(value)}</t></is></c>"
            )
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_XML_MAIN}"><sheetData>'
        f'{"".join(xml_rows)}</sheetData></worksheet>'
    ).encode("utf-8")


def _xlsx_bytes(recipe: dict[str, object]) -> bytes:
    sheets = recipe["sheets"]
    if not isinstance(sheets, list) or not sheets:
        raise AssertionError("xlsx recipe requires at least one sheet")
    active_sheet = recipe["active_sheet_index"]
    if type(active_sheet) is not int or not 0 <= active_sheet < len(sheets):
        raise AssertionError("active_sheet_index is outside the sheet list")

    overrides: dict[str, bytes] = {}
    extra_members: list[tuple[str, bytes, int]] = []
    for member in recipe.get("extra_members", []):
        populated = {"utf8", "hex", "repeat_utf8"}.intersection(member)
        if len(populated) != 1:
            raise AssertionError("extra member requires one payload encoding")
        kind = next(iter(populated))
        if kind == "utf8":
            data = member["utf8"].encode("utf-8")
        elif kind == "hex":
            data = bytes.fromhex(member["hex"])
        else:
            count = member["count"]
            if type(count) is not int or count < 0:
                raise AssertionError("repeat member count must be non-negative")
            data = member["repeat_utf8"].encode("utf-8") * count
        compression = (
            zipfile.ZIP_DEFLATED
            if member.get("compression") == "deflated"
            else zipfile.ZIP_STORED
        )
        if member.get("override"):
            overrides[member["path"]] = data
        else:
            extra_members.append((member["path"], data, compression))

    content_types = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
    ]
    workbook_sheets: list[str] = []
    workbook_relations: list[str] = []
    sheet_members: list[tuple[str, bytes, int]] = []
    for index, sheet in enumerate(sheets, start=1):
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        workbook_sheets.append(
            f'<sheet name="{_xml_text(sheet["name"])}" sheetId="{index}" r:id="rId{index}"/>'
        )
        workbook_relations.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
        path = f"xl/worksheets/sheet{index}.xml"
        payload = overrides.pop(path, _worksheet_xml(sheet["rows"]))
        sheet_members.append((path, payload, zipfile.ZIP_STORED))
    content_types.append("</Types>")

    members = [
        (
            "[Content_Types].xml",
            "".join(content_types).encode("utf-8"),
            zipfile.ZIP_STORED,
        ),
        (
            "_rels/.rels",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="xl/workbook.xml"/></Relationships>'
            ).encode("utf-8"),
            zipfile.ZIP_STORED,
        ),
        (
            "xl/workbook.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<workbook xmlns="{_XML_MAIN}" xmlns:r="{_XML_REL}">'
                f'<bookViews><workbookView activeTab="{active_sheet}"/></bookViews>'
                f'<sheets>{"".join(workbook_sheets)}</sheets></workbook>'
            ).encode("utf-8"),
            zipfile.ZIP_STORED,
        ),
        (
            "xl/_rels/workbook.xml.rels",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'{"".join(workbook_relations)}</Relationships>'
            ).encode("utf-8"),
            zipfile.ZIP_STORED,
        ),
        *sheet_members,
        *extra_members,
    ]
    members.extend(
        (path, payload, zipfile.ZIP_STORED) for path, payload in overrides.items()
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload, compression in members:
            archive.writestr(_zip_info(name, compression), payload)
    data = output.getvalue()
    truncate = recipe.get("truncate_tail_bytes", 0)
    if type(truncate) is not int or truncate < 0 or truncate >= len(data):
        raise AssertionError("truncate_tail_bytes must retain a non-empty archive")
    return data[:-truncate] if truncate else data


def _case_bytes(case: dict[str, object]) -> bytes:
    source = case["source"]
    kind = source["kind"]
    if kind in {"file", "hex"}:
        relative = Path(source["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise AssertionError(f"unsafe fixture path: {relative}")
        path = (FIXTURE_ROOT / relative).resolve()
        candidate = FIXTURE_ROOT / relative
        if candidate.is_symlink():
            raise AssertionError(f"fixture must not be a symlink: {relative}")
        if not path.is_relative_to(FIXTURE_ROOT.resolve()) or not path.is_file():
            raise AssertionError(f"fixture is not a checked-in regular file: {relative}")
        payload = path.read_bytes()
        return payload if kind == "file" else bytes.fromhex(payload.decode("ascii"))
    if kind == "csv-recipe-v1":
        return source["sample_utf8"].encode("utf-8")
    if kind == "xlsx-recipe-v1":
        return _xlsx_bytes(source)
    raise AssertionError(f"unsupported fixture kind: {kind}")


def _case_map(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        case["case_id"]: case
        for format_entry in manifest["formats"]
        for case in format_entry["cases"]
    }


def _xlsx_sheet_rows(payload: bytes, sheet_index: int) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = ET.fromstring(archive.read(f"xl/worksheets/sheet{sheet_index + 1}.xml"))
    rows: list[list[str]] = []
    for row in root.findall(f".//{{{_XML_MAIN}}}row"):
        values: list[str] = []
        for cell in row.findall(f"{{{_XML_MAIN}}}c"):
            text = cell.find(f".//{{{_XML_MAIN}}}t")
            values.append("" if text is None or text.text is None else text.text)
        rows.append(values)
    return rows


def _xml_declaration_callbacks(payload: bytes) -> tuple[str, ...]:
    observed: list[str] = []
    parser = xml.parsers.expat.ParserCreate()
    parser.StartDoctypeDeclHandler = lambda *_args: observed.append("doctype")
    parser.EntityDeclHandler = lambda *_args: observed.append("entity")
    parser.Parse(payload, True)
    return tuple(observed)


class TermbaseGoldenFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _load_manifest()
        cls.cases = _case_map(cls.manifest)

    def test_manifest_is_distributable_and_only_claims_wave_zero_corpus(self) -> None:
        raw = MANIFEST_PATH.read_text(encoding="utf-8")
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(self.manifest["purpose"], "language_resource.termbase")
        self.assertEqual(self.manifest["evidence_scope"], "synthetic-corpus-only")
        self.assertIs(self.manifest["production_codec_exercised"], False)
        self.assertEqual(
            self.manifest["provenance"],
            {"external_inputs": [], "kind": "synthetic", "redistributable": True},
        )
        for forbidden in ("/Users/", "Downloads/", "CAT_Working_File"):
            self.assertNotIn(forbidden, raw)

    def test_both_formats_cover_the_complete_contract_matrix(self) -> None:
        entries = {entry["format_id"]: entry for entry in self.manifest["formats"]}
        self.assertEqual(set(entries), FORMAT_IDS)
        all_case_ids: list[str] = []
        for format_id, entry in entries.items():
            with self.subTest(format_id=format_id):
                self.assertEqual(entry["effective_purpose"], self.manifest["purpose"])
                self.assertEqual(entry["limit_profile"]["input_bytes"], 100 * 1024 * 1024)
                axes = {axis for case in entry["cases"] for axis in case["axes"]}
                self.assertTrue(REQUIRED_AXES.issubset(axes))
                encoding_terminals = {
                    case["expect"]["terminal"]
                    for case in entry["cases"]
                    if "encoding" in case["axes"]
                }
                self.assertIn("success", encoding_terminals)
                self.assertIn("fatal", encoding_terminals)
                for case in entry["cases"]:
                    all_case_ids.append(case["case_id"])
                    self.assertEqual(case["format_id"], format_id)
                    self.assertIn(case["expect"]["terminal"], TERMINALS)
                    self.assertIs(
                        case["expect"]["commit_authorized"],
                        case["expect"]["terminal"] in {"success", "success_with_warnings"},
                    )
        self.assertEqual(len(all_case_ids), len(set(all_case_ids)))

    def test_payloads_and_recipes_are_reproducible_and_digest_bound(self) -> None:
        referenced_paths: set[Path] = set()
        for case_id, case in self.cases.items():
            with self.subTest(case=case_id):
                source = case["source"]
                if source["kind"] in {"file", "hex"}:
                    referenced_paths.add(Path(source["path"]))
                first = _case_bytes(case)
                second = _case_bytes(case)
                self.assertEqual(first, second)
                self.assertEqual(len(first), case["byte_length"])
                self.assertEqual(hashlib.sha256(first).hexdigest(), case["sha256"])
        actual_paths = {
            path.relative_to(FIXTURE_ROOT)
            for path in (FIXTURE_ROOT / "payloads").glob("**/*")
            if path.is_file()
        }
        self.assertEqual(referenced_paths, actual_paths)

    def test_selection_matrix_has_no_implicit_or_ambiguous_fallback(self) -> None:
        for format_id in FORMAT_IDS:
            prefix = "csv" if format_id.endswith("csv-v1") else "xlsx"
            header = self.cases[f"{prefix}-header-name-valid"]["selection"]
            self.assertEqual(
                header,
                {
                    "header_policy": "first_row",
                    "kind": "header_name",
                    "source": "Source",
                    "target": "Target",
                },
            )
            index = self.cases[f"{prefix}-index-headerless-valid"]["selection"]
            self.assertEqual(
                index,
                {
                    "header_policy": "no_header",
                    "kind": "zero_based_index",
                    "source": 1,
                    "target": 0,
                },
            )
            legacy = self.cases[f"{prefix}-legacy-preset-valid"]["selection"]
            self.assertEqual(
                legacy,
                {
                    "header_policy": "legacy_allowlist",
                    "kind": "zero_based_index",
                    "preset": "legacy_0_1",
                    "source": 0,
                    "target": 1,
                },
            )
            for boundary in ("missing-header", "duplicate-header", "same-column"):
                case = self.cases[f"{prefix}-{boundary}"]
                self.assertEqual(case["expect"]["terminal"], "fatal")
                self.assertEqual(case["expect"]["accepted_ids"], [])
                self.assertTrue(case["expect"]["failure_before_first_record"])
            required = self.cases[f"{prefix}-selection-required"]
            self.assertIsNone(required["selection"])
            self.assertEqual(
                required["expect"]["issue_code"],
                "PARSER.TERMBASE.COLUMN_SELECTION_REQUIRED",
            )

    def test_record_warning_fixtures_preserve_physical_row_ordinals(self) -> None:
        csv_case = self.cases["csv-record-warnings"]
        csv_rows = list(csv.reader(io.StringIO(_case_bytes(csv_case).decode("utf-8"))))
        self.assertEqual(len(csv_rows), 7)
        self.assertEqual(csv_case["expect"]["accepted_ids"], ["row-2", "row-7"])
        self.assertEqual(csv_case["expect"]["warning_ordinals"], [3, 4, 5, 6])

        xlsx_case = self.cases["xlsx-record-warnings"]
        xlsx_rows = _xlsx_sheet_rows(_case_bytes(xlsx_case), 0)
        self.assertEqual(len(xlsx_rows), 7)
        self.assertEqual(xlsx_case["expect"]["accepted_ids"], ["row-2", "row-7"])
        self.assertEqual(xlsx_case["expect"]["warning_ordinals"], [3, 4, 5, 6])
        for case in (csv_case, xlsx_case):
            self.assertEqual(
                case["expect"]["warning_reasons"],
                ["empty_row", "missing_selected_column", "empty_source", "empty_target"],
            )
            self.assertEqual(case["expect"]["terminal"], "success_with_warnings")

    def test_xlsx_active_sheet_is_explicit_and_multi_sheet_is_not_aggregated(self) -> None:
        case = self.cases["xlsx-header-name-valid"]
        payload = _case_bytes(case)
        source = case["source"]
        self.assertEqual([sheet["name"] for sheet in source["sheets"]], ["Ignored", "Active", "AlsoIgnored"])
        self.assertEqual(source["active_sheet_index"], 1)
        self.assertEqual(case["expect"]["worksheet_mode"], "active_sheet_only")
        self.assertEqual(case["expect"]["accepted_ids"], ["row-2", "row-3"])
        self.assertEqual(_xlsx_sheet_rows(payload, 1)[1:], [["Alpha", "甲"], ["Beta", "乙"]])
        self.assertEqual(case["expect"]["non_active_markers"], ["DO_NOT_AGGREGATE_1", "DO_NOT_AGGREGATE_2"])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        active_tab = workbook.find(f".//{{{_XML_MAIN}}}workbookView").attrib["activeTab"]
        self.assertEqual(active_tab, "1")

    def test_xlsx_archive_xml_and_dependency_boundaries_are_named(self) -> None:
        archive_dimensions = {
            "xlsx-archive-expanded-byte-limit": ("expanded_bytes", 256 * 1024 * 1024, 1),
            "xlsx-archive-member-limit": ("archive_members", 4096, 1),
            "xlsx-archive-ratio-limit": ("compression_ratio", 100, 1),
        }
        for case_id, (dimension, limit, offset) in archive_dimensions.items():
            with self.subTest(case=case_id):
                runtime = self.cases[case_id]["source"]["runtime"]
                self.assertEqual(runtime["dimension"], dimension)
                self.assertEqual(runtime["profile_limit"], limit)
                self.assertEqual(runtime["offset"], offset)
                self.assertEqual(self.cases[case_id]["expect"]["terminal"], "fatal")

        ratio_payload = _case_bytes(self.cases["xlsx-archive-ratio-limit"])
        with zipfile.ZipFile(io.BytesIO(ratio_payload)) as archive:
            ratios = [
                info.file_size / max(1, info.compress_size)
                for info in archive.infolist()
            ]
        self.assertGreater(max(ratios), 100)

        dtd_case = self.cases["xlsx-dtd-member"]
        entity_case = self.cases["xlsx-entity-member"]
        dtd = _case_bytes(dtd_case)
        entity = _case_bytes(entity_case)
        invalid = _case_bytes(self.cases["xlsx-invalid-xml-encoding"])
        with zipfile.ZipFile(io.BytesIO(dtd)) as archive:
            dtd_member = archive.read("customXml/item1.xml")
        with zipfile.ZipFile(io.BytesIO(entity)) as archive:
            entity_member = archive.read("customXml/item1.xml")
        self.assertEqual(_xml_declaration_callbacks(dtd_member), ("doctype",))
        self.assertEqual(
            _xml_declaration_callbacks(entity_member),
            ("doctype", "entity"),
        )
        self.assertEqual(
            dtd_case["expect"]["issue_code"],
            "PARSER.TERMBASE.XML_DECLARATION_FORBIDDEN",
        )
        self.assertEqual(
            entity_case["expect"]["issue_code"],
            "PARSER.TERMBASE.XML_DECLARATION_FORBIDDEN",
        )
        self.assertEqual(dtd_case["expect"]["forbidden_constructs"], ["doctype"])
        self.assertEqual(
            entity_case["expect"]["forbidden_constructs"],
            ["doctype", "entity"],
        )
        with self.assertRaises(UnicodeDecodeError):
            with zipfile.ZipFile(io.BytesIO(invalid)) as archive:
                archive.read("xl/worksheets/sheet1.xml").decode("utf-8")

        dependency = self.cases["xlsx-openpyxl-missing"]
        self.assertEqual(dependency["environment"], {"openpyxl": "missing"})
        self.assertEqual(
            dependency["expect"]["issue_code"],
            "PARSER.CAPABILITY.CONDITIONAL_DEPENDENCY_MISSING",
        )
        self.assertTrue(dependency["expect"]["failure_before_first_record"])

    def test_fatal_tail_limit_and_cancel_recipes_freeze_later_checkpoints(self) -> None:
        with self.assertRaises(zipfile.BadZipFile):
            with zipfile.ZipFile(io.BytesIO(_case_bytes(self.cases["xlsx-fatal-tail"]))):
                pass
        malformed_csv = _case_bytes(self.cases["csv-fatal-tail"]).decode("utf-8")
        self.assertTrue(malformed_csv.rstrip("\n").endswith('"unterminated'))
        with self.assertRaises(csv.Error):
            list(csv.reader(io.StringIO(malformed_csv), strict=True))

        limits = [
            case
            for case in self.cases.values()
            if "limit" in case["axes"]
        ]
        self.assertGreaterEqual(len(limits), 6)
        for case in limits:
            with self.subTest(case=case["case_id"]):
                runtime = case["source"]["runtime"]
                self.assertGreater(runtime["profile_limit"], 0)
                self.assertGreater(runtime["offset"], 0)
                self.assertEqual(case["expect"]["terminal"], "fatal")
                self.assertFalse(case["expect"]["commit_authorized"])

        for case_id, checkpoint in (
            ("csv-cancel-after-row", "after_row"),
            ("xlsx-cancel-after-row", "after_row"),
        ):
            case = self.cases[case_id]
            self.assertEqual(
                case["source"]["cancellation"],
                {"cancel_after": 1, "checkpoint": checkpoint, "physical_rows": 3},
            )
            self.assertEqual(case["expect"]["terminal"], "cancelled")
            self.assertFalse(case["expect"]["commit_authorized"])


if __name__ == "__main__":
    unittest.main()
