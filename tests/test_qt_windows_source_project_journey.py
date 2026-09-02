"""WA-08 Windows source Qt save and fresh-process reopen acceptance."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


WORKER = Path(__file__).with_name("windows_qt_source_project_worker.py")

_QT_OVERRIDE_ENVIRONMENT = (
    "PYSIDE_DESIGNER_PLUGINS",
    "QML2_IMPORT_PATH",
    "QML_IMPORT_PATH",
    "QT_DEBUG_PLUGINS",
    "QT_LOGGING_RULES",
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
)


def _write_project(
    path: Path,
    *,
    name: str,
    source: str,
    segment_id: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": name,
                "source_locale": "en",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": segment_id,
                        "source": source,
                        "target": "",
                        "speaker": "",
                        "confirmed": False,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@unittest.skipUnless(sys.platform == "win32", "WA-08 requires Windows Qt")
class WindowsQtSourceProjectJourneyTests(unittest.TestCase):
    def _run(
        self,
        cwd: Path,
        mode: str,
        *paths: Path,
    ) -> dict[str, object]:
        marker = paths[-1]
        environment = os.environ.copy()
        for name in _QT_OVERRIDE_ENVIRONMENT:
            environment.pop(name, None)
        environment["QT_QPA_PLATFORM"] = "windows"
        environment["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            [sys.executable, "-I", "-B", str(WORKER), mode, *(str(path) for path in paths)],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"worker failed ({mode})\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        self.assertTrue(marker.is_file(), f"worker did not publish {marker}")
        return json.loads(marker.read_text(encoding="utf-8"))

    def test_legacy_json_saves_and_fresh_qt_process_reopens_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa08-legacy-") as directory:
            root = Path(directory).resolve()
            nonrepo_cwd = root / "launch cwd with spaces"
            nonrepo_cwd.mkdir()
            source = root / "输入" / "legacy source.json"
            saved = root / "输出" / "legacy saved.json"
            saved.parent.mkdir()
            appdata = root / "appdata"
            save_marker = root / "legacy-save.json"
            open_marker = root / "legacy-open.json"
            _write_project(
                source,
                name="Windows Qt legacy",
                source="Legacy source",
                segment_id="legacy-one",
            )

            saved_facts = self._run(
                nonrepo_cwd,
                "legacy-save",
                source,
                saved,
                appdata,
                save_marker,
            )
            opened_facts = self._run(
                nonrepo_cwd,
                "legacy-open",
                saved,
                appdata,
                open_marker,
            )

            self.assertNotEqual(saved_facts["pid"], opened_facts["pid"])
            self.assertEqual(saved_facts["qpa"], "windows")
            self.assertEqual(opened_facts["qpa"], "windows")
            self.assertEqual(saved_facts["cwd"], str(nonrepo_cwd))
            self.assertEqual(opened_facts["cwd"], str(nonrepo_cwd))
            self.assertTrue(saved_facts["dirty_before_save"])
            self.assertFalse(saved_facts["dirty_after_save"])
            self.assertFalse(opened_facts["dirty"])
            self.assertEqual(saved_facts["segment_id"], "legacy-one")
            self.assertEqual(opened_facts["segment_id"], "legacy-one")
            self.assertEqual(saved_facts["target"], "Windows Qt legacy target")
            self.assertEqual(opened_facts["target"], "Windows Qt legacy target")
            self.assertEqual(opened_facts["project_name"], "Windows Qt legacy")
            self.assertEqual(opened_facts["segment_count"], 1)
            self.assertNotEqual(
                saved_facts["session_id"],
                opened_facts["session_id"],
            )
            self.assertEqual(Path(saved_facts["saved_path"]), saved)
            self.assertEqual(Path(opened_facts["opened_path"]), saved)
            self.assertTrue(saved_facts["closed"])
            self.assertTrue(opened_facts["closed"])

    def test_project_package_receipt_identity_and_clean_baseline_survive_fresh_qt_process(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa08-package-") as directory:
            root = Path(directory).resolve()
            nonrepo_cwd = root / "非仓库 cwd"
            nonrepo_cwd.mkdir()
            source_root = root / "selected sources"
            first = source_root / "chapter-a.json"
            second = source_root / "chapter-b.json"
            package = root / "published" / "project.localcat-project"
            package.parent.mkdir()
            appdata = root / "appdata"
            save_marker = root / "package-save.json"
            open_marker = root / "package-open.json"
            _write_project(
                first,
                name="Chapter A",
                source="Package source A",
                segment_id="a-one",
            )
            _write_project(
                second,
                name="Chapter B",
                source="Package source B",
                segment_id="b-one",
            )
            first_source_before = first.read_bytes()
            second_source_before = second.read_bytes()

            saved_facts = self._run(
                nonrepo_cwd,
                "package-save",
                source_root,
                first,
                second,
                package,
                appdata,
                save_marker,
            )
            opened_facts = self._run(
                nonrepo_cwd,
                "package-open",
                package,
                appdata,
                open_marker,
            )

            receipt = saved_facts["receipt"]
            identity_before = saved_facts["identity_before_save"]
            identity_after = saved_facts["identity_after_save"]
            reopened_identity = opened_facts["identity"]
            self.assertIsInstance(receipt, dict)
            self.assertIsInstance(identity_before, dict)
            self.assertIsInstance(identity_after, dict)
            self.assertIsInstance(reopened_identity, dict)
            assert isinstance(receipt, dict)
            assert isinstance(identity_before, dict)
            assert isinstance(identity_after, dict)
            assert isinstance(reopened_identity, dict)

            self.assertNotEqual(saved_facts["pid"], opened_facts["pid"])
            self.assertEqual(saved_facts["qpa"], "windows")
            self.assertEqual(opened_facts["qpa"], "windows")
            self.assertEqual(saved_facts["cwd"], str(nonrepo_cwd))
            self.assertEqual(opened_facts["cwd"], str(nonrepo_cwd))
            self.assertTrue(saved_facts["dirty_before_save"])
            self.assertFalse(saved_facts["dirty_after_save"])
            self.assertEqual(saved_facts["dirty_document_ids"], [])
            self.assertFalse(opened_facts["dirty"])
            self.assertEqual(opened_facts["dirty_document_ids"], [])
            self.assertFalse(saved_facts["recovery_pending"])
            self.assertFalse(opened_facts["recovery_pending"])
            self.assertTrue(receipt["durable"])
            self.assertFalse(receipt["recovery_required"])
            self.assertEqual(receipt["document_count"], 2)
            self.assertEqual(receipt["segment_count"], 2)
            self.assertEqual(saved_facts["artifact_digest"], receipt["artifact_digest"])
            self.assertEqual(opened_facts["artifact_digest"], receipt["artifact_digest"])
            self.assertEqual(
                opened_facts["workspace_content_digest"],
                receipt["workspace_content_digest"],
            )
            self.assertEqual(receipt["project_id"], identity_after["project_id"])
            stable_keys = ("project_id", "document_id", "local_segment_id")
            self.assertEqual(
                tuple(identity_before[key] for key in stable_keys),
                tuple(identity_after[key] for key in stable_keys),
            )
            self.assertEqual(
                tuple(identity_after[key] for key in stable_keys),
                tuple(reopened_identity[key] for key in stable_keys),
            )
            self.assertNotEqual(
                identity_after["session_id"],
                reopened_identity["session_id"],
            )
            self.assertEqual(saved_facts["target"], "Windows Qt package target")
            self.assertEqual(opened_facts["target"], "Windows Qt package target")
            self.assertEqual(opened_facts["project_name"], "Windows Qt ProjectPackage")
            self.assertEqual(opened_facts["document_count"], 2)
            self.assertEqual(opened_facts["segment_count"], 2)
            self.assertIn("LocalCAT项目包已保存", saved_facts["feedback"])
            self.assertIn("LocalCAT项目包已打开", opened_facts["feedback"])
            self.assertEqual(first.read_bytes(), first_source_before)
            self.assertEqual(second.read_bytes(), second_source_before)
            self.assertTrue(saved_facts["closed"])
            self.assertTrue(opened_facts["closed"])


if __name__ == "__main__":
    unittest.main()
