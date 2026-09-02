from __future__ import annotations

from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from editor_contracts import SpeakerInventory, SpeakerInventoryItem
from platform_source_authority import compose_rooted_source_authority
from qt_resource_contracts import QtResourceBytes
from qt_source_resources import resolve_source_qt_resources
from qt_speaker_avatar import SpeakerAvatarCatalog, resource_png_pixmap
from qt_speaker_inventory_dialog import QtSpeakerInventoryDialog


class _InventoryController:
    def speaker_inventory(self) -> SpeakerInventory:
        return SpeakerInventory(
            items=(
                SpeakerInventoryItem("ädela", 2, "one", 0),
                SpeakerInventoryItem("Unknown", 1, "two", 1),
            ),
            empty_count=0,
            segment_count=3,
        )


def _png_bytes(color: int = 0xFF336699) -> bytes:
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(color)
    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise AssertionError("could not open synthetic PNG buffer")
    try:
        if not image.save(buffer, "PNG"):
            raise AssertionError("could not encode synthetic PNG")
    finally:
        buffer.close()
    return bytes(data)


class QtSourceResourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _source_root(self, parent: Path) -> Path:
        source_root = parent / "source-root"
        avatars = source_root / "speaker_avatars"
        avatars.mkdir(parents=True)
        (source_root / "LocalCAT-logo-silver.png").write_bytes(_png_bytes())
        return source_root

    def test_rooted_bytes_work_from_non_repository_cwd_and_do_not_touch_business_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = self._source_root(root)
            (source_root / "speaker_avatars" / "ÄDELAHalf.png").write_bytes(
                _png_bytes(0xFF884422)
            )
            business_root = root / "business"
            business_root.mkdir()
            business_paths = (
                business_root / "project.json",
                business_root / "workspace.json",
                business_root / "tm.jsonl",
            )
            for index, path in enumerate(business_paths):
                path.write_bytes(f"sentinel-{index}\n".encode("utf-8"))
            before = {path: path.read_bytes() for path in business_paths}
            launch_cwd = root / "launch-cwd"
            launch_cwd.mkdir()

            authority = compose_rooted_source_authority(source_root.resolve())
            previous_cwd = Path.cwd()
            try:
                os.chdir(launch_cwd)
                resources = resolve_source_qt_resources(
                    authority,
                    logo_filename="LocalCAT-logo-silver.png",
                )
            finally:
                os.chdir(previous_cwd)
                authority.close()

            self.assertEqual(resources.logo.filename, "LocalCAT-logo-silver.png")
            self.assertTrue(resources.logo.content)
            logo_pixmap = resource_png_pixmap(resources.logo)
            self.assertIsNotNone(logo_pixmap)
            assert logo_pixmap is not None
            self.assertFalse(logo_pixmap.isNull())
            self.assertEqual(
                tuple(asset.filename for asset in resources.speaker_avatars),
                ("ÄDELAHalf.png",),
            )
            catalog = SpeakerAvatarCatalog(resources.speaker_avatars)
            dialog = QtSpeakerInventoryDialog(  # type: ignore[arg-type]
                _InventoryController(),
                avatar_catalog=catalog,
            )
            self.addCleanup(dialog.close)
            matched = dialog.table.cellWidget(0, 0)
            missing = dialog.table.cellWidget(1, 0)
            self.assertIsNotNone(matched.pixmap())
            self.assertEqual(matched.accessibleName(), "ädela：内置 speaker 头像")
            self.assertEqual(missing.text(), "—")
            self.assertEqual(missing.accessibleName(), "Unknown：无内置头像")
            self.assertEqual(
                {path: path.read_bytes() for path in business_paths},
                before,
            )
            with self.assertRaises(FrozenInstanceError):
                resources.logo.content = b"replacement"  # type: ignore[misc]

    def test_catalog_uniqueness_casefold_unicode_and_decode_failures_are_stable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = self._source_root(root)
            avatars = source_root / "speaker_avatars"
            valid = _png_bytes()
            (avatars / "ÄDELAHalf.png").write_bytes(valid)
            (avatars / "StraßeHalf.png").write_bytes(valid)
            (avatars / "STRASSEHalf.png").write_bytes(valid)
            (avatars / "BrokenHalf.png").write_bytes(b"not a png")
            (avatars / "Wrong.png").write_bytes(valid)

            authority = compose_rooted_source_authority(source_root.resolve())
            try:
                resources = resolve_source_qt_resources(
                    authority,
                    logo_filename="LocalCAT-logo-silver.png",
                )
            finally:
                authority.close()
            catalog = SpeakerAvatarCatalog(resources.speaker_avatars)

            self.assertIsNotNone(catalog.avatar_pixmap("ädela"))
            self.assertIsNone(catalog.avatar_pixmap("strasse"))
            self.assertIsNone(catalog.avatar_pixmap("Broken"))
            self.assertIsNone(catalog.avatar_pixmap("Wrong"))
            self.assertIsNone(catalog.avatar_pixmap("No match"))
            self.assertNotIn(
                "Wrong.png",
                tuple(asset.filename for asset in resources.speaker_avatars),
            )

    def test_catalog_ignores_wrong_suffix_even_for_direct_immutable_input(self) -> None:
        catalog = SpeakerAvatarCatalog(
            (QtResourceBytes("Alice.png", _png_bytes()),)
        )
        self.assertIsNone(catalog.avatar_pixmap("Alice"))


if __name__ == "__main__":
    unittest.main()
