"""WA-02 package-coupled Windows source acceptance on real ProjectPackage APIs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from project_workspace_intake import SelectedProjectDocumentsRequest
from qt_editor import _compose_editor_controller
from resource_repository import ResourceRepository


WORKER = Path(__file__).with_name("windows_chunk_store_worker.py")


def _write_document(path: Path, name: str, source: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": name,
                "source_locale": "en",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": "shared",
                        "source": source,
                        "target": "",
                        "speaker": "",
                        "confirmed": False,
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


@unittest.skipUnless(sys.platform == "win32", "WA-02 package acceptance requires Windows")
class WindowsChunkProjectPackageAcceptanceTests(unittest.TestCase):
    def _run(self, cwd: Path, *args: Path | str) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(WORKER), *(str(value) for value in args)],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
        self.assertEqual(
            completed.returncode,
            0,
            (completed.stdout + completed.stderr).decode("utf-8", errors="replace"),
        )
        result = Path(args[-1])
        return json.loads(result.read_text(encoding="utf-8"))

    def test_two_process_chunk_journey_uses_real_movable_project_package(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-wa02-package-") as directory:
            root = Path(directory)
            sources = root / "sources"
            sources.mkdir()
            first = sources / "a.json"
            second = sources / "b.json"
            _write_document(first, "A", "alpha")
            _write_document(second, "B", "beta")
            package = root / "project.localcat-project"
            controller, _composition = _compose_editor_controller(
                ResourceRepository(root / "creator-app-data")
            )
            created = controller.create_workspace_project_from_selected_files(
                sources,
                (first, second),
                SelectedProjectDocumentsRequest("WA-02", "en", "zh-CN"),
                package,
            )
            before = package.read_bytes()
            before_digest = hashlib.sha256(before).hexdigest()
            app_data = root / "chunk-app-data"
            non_repository_cwd = root / "outside-cwd"
            non_repository_cwd.mkdir()

            created_result = self._run(
                non_repository_cwd,
                "package-create",
                package,
                app_data,
                root / "created.json",
            )
            self.assertEqual(created_result["project_id"], created.session.project.project_id)
            self.assertEqual(created_result["chunk_count"], 1)
            self.assertEqual(created_result["member_count"], 2)
            self.assertEqual(created_result["package_sha256"], before_digest)
            self.assertEqual(package.read_bytes(), before)

            moved = root / "moved-project.localcat-project"
            os.replace(package, moved)
            reopened_result = self._run(
                non_repository_cwd,
                "package-open",
                moved,
                app_data,
                root / "reopened.json",
            )
            self.assertEqual(reopened_result["project_id"], created.session.project.project_id)
            self.assertEqual(reopened_result["chunk_count"], 1)
            self.assertEqual(reopened_result["chunk_name"], "Windows package slice")
            self.assertEqual(reopened_result["member_count"], 2)
            self.assertEqual(reopened_result["package_sha256"], before_digest)
            self.assertEqual(moved.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
