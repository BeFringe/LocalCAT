"""WA-08 native Windows Qt source-runtime acceptance."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


WORKER = Path(__file__).with_name("windows_qt_source_ui_worker.py")
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


@unittest.skipUnless(sys.platform == "win32", "WA-08 requires native Windows Qt")
class WindowsQtSourceRuntimeTests(unittest.TestCase):
    def test_native_window_dialog_icons_and_keyboard_use_production_qt(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="localcat-wa08-native-qt-"
        ) as temporary:
            root = Path(temporary).resolve()
            nonrepo_cwd = root / "non-repository cwd"
            nonrepo_cwd.mkdir()
            project = root / "project" / "speaker project.json"
            project.parent.mkdir()
            project.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "Windows native Qt",
                        "source_locale": "en",
                        "target_locale": "zh-CN",
                        "segments": [
                            {
                                "id": "native-one",
                                "source": "Native Qt source",
                                "target": "",
                                "speaker": "Adela",
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
            appdata = root / "application data"
            marker = root / "native-qt-result.json"
            environment = os.environ.copy()
            for name in _QT_OVERRIDE_ENVIRONMENT:
                environment.pop(name, None)
            environment["QT_QPA_PLATFORM"] = "windows"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(WORKER),
                    str(project),
                    str(appdata),
                    str(marker),
                ],
                cwd=nonrepo_cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                "native Qt worker failed\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}",
            )
            self.assertTrue(marker.is_file())
            payload = json.loads(marker.read_text(encoding="utf-8"))

            self.assertNotEqual(payload["pid"], os.getpid())
            self.assertEqual(payload["platform_name"], "windows")
            self.assertEqual(Path(payload["cwd"]).resolve(), nonrepo_cwd)
            self.assertTrue(payload["window_exposed"])
            self.assertTrue(payload["window_visible"])
            self.assertTrue(payload["application_icon_matches_logo"])
            self.assertTrue(payload["window_icon_matches_logo"])
            self.assertTrue(payload["dialog_icon_matches_logo"])
            self.assertTrue(payload["dialog_visible_in_nested_loop"])
            self.assertTrue(payload["dialog_modal"])
            self.assertTrue(payload["dialog_closed_by_escape"])
            self.assertEqual(payload["dialog_accessible_name"], "Raw speaker 盘点")
            self.assertEqual(payload["dialog_rows"], 1)
            self.assertFalse(payload["keyboard_search_visible_before"])
            self.assertTrue(payload["keyboard_search_visible_after"])
            self.assertTrue(payload["keyboard_search_focused"])
            self.assertFalse(payload["xlwings_imported"])


if __name__ == "__main__":
    unittest.main()
