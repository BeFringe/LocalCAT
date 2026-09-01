from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from tests import windows_c5_persistence_reboot_worker as worker


def _cleanup_test_root(root: Path) -> None:
    if root.exists():
        shutil.rmtree("\\\\?\\" + str(root))


@unittest.skipUnless(sys.platform == "win32", "C5 reboot worker requires Windows")
class C5PersistenceRebootWorkerTests(unittest.TestCase):
    def test_ticket_requires_a_changed_boot_session_before_owner_recovery(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="localcat-c5-reboot-worker-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve() / "evidence"
        self.addCleanup(_cleanup_test_root, root)
        with mock.patch.object(
            worker,
            "_current_boot_session",
            return_value="same-session",
        ):
            prepared = worker.prepare(root)
        self.assertEqual(prepared["reboot"], "PREPARED")
        with mock.patch.object(
            worker,
            "_current_boot_session",
            return_value="same-session",
        ):
            not_run = worker.resume(root)
        self.assertEqual(not_run["reboot"], "NOT_RUN")
        with mock.patch.object(
            worker,
            "_current_boot_session",
            return_value="changed-session",
        ):
            resumed = worker.resume(root)
        self.assertEqual(
            resumed,
            {
                "project": "PASS",
                "reboot": "PASS",
                "resource": "PASS",
                "schema": "localcat.windows-c5-persistence-reboot-ticket.v2",
                "ticket_sha256": prepared["ticket_sha256"],
                "tmx": "PASS",
            },
        )


if __name__ == "__main__":
    unittest.main()
