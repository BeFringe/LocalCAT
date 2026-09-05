"""Theme changes are presentation-only for auxiliary Qt surfaces."""
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from editor_contracts import BrowseGroupDisplayMode, BrowseGroupPreferences
from qt_browse_group_dialog import QtBrowseGroupDialog, BrowseGroupCard, BrowseGroupPreview, BrowseGroupTurnBar
from qt_termbase_dialog import QtTermbaseDialog
from tests import test_qt_termbase_dialog as termbase_fixture


class AuxiliaryDialogThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_browse_settings_and_painted_card_switch_without_rebuilding(self):
        dialog = QtBrowseGroupDialog(preferences=BrowseGroupPreferences(), document_name="fixture", segment_count=100)
        dialog.group_size_spin.setValue(30)
        card = BrowseGroupCard(BrowseGroupPreview(1, 2, 0, 9, "source", "target", object()), dialog)
        card.resize(370, 92)
        try:
            for scheme, dark in ((Qt.ColorScheme.Dark, True), (Qt.ColorScheme.Light, False)):
                self.app.styleHints().colorSchemeChanged.emit(scheme)
                self.assertEqual(dialog.palette().color(QPalette.Window).lightnessF() < .5, dark)
                self.assertEqual(card.grab().toImage().pixelColor(360, 80).lightnessF() < .5, dark)
                self.assertEqual(dialog.group_size_spin.value(), 30)
                self.assertIsNone(dialog.saved_preferences)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_termbase_table_input_and_selection_keep_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            controller, _composition, _repository, resource_id, _tm_id = termbase_fixture.QtTermbaseDialogTests()._controller(Path(temporary))
            dialog = QtTermbaseDialog(controller, resource_id, "fixture")
            dialog.term_table.setCurrentCell(1, 0)
            dialog.source_input.setText("unsaved input")
            try:
                for scheme, dark in ((Qt.ColorScheme.Dark, True), (Qt.ColorScheme.Light, False)):
                    self.app.styleHints().colorSchemeChanged.emit(scheme)
                    self.assertEqual(dialog.source_input.palette().color(QPalette.Base).lightnessF() < .5, dark)
                    self.assertEqual(dialog.term_table.viewport().palette().color(QPalette.Base).lightnessF() < .5, dark)
                    self.assertEqual(dialog.source_input.text(), "unsaved input")
                    self.assertEqual(dialog.term_table.currentRow(), 1)
            finally:
                dialog.close()
                dialog.deleteLater()

    def test_fixed_turn_bar_repaints_and_binding_dies_with_parent(self):
        from shiboken6 import isValid

        bar = BrowseGroupTurnBar()
        bar.set_display_mode(BrowseGroupDisplayMode.FIXED)
        bar.resize(420, 200)
        binding = bar._theme_binding
        for scheme, dark in ((Qt.ColorScheme.Dark, True), (Qt.ColorScheme.Light, False)):
            self.app.styleHints().colorSchemeChanged.emit(scheme)
            self.assertEqual(bar.grab().toImage().pixelColor(10, 190).lightnessF() < .5, dark)
        QApplication.sendEvent(bar, QEvent(QEvent.PaletteChange))
        bar.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertFalse(isValid(binding))
        self.app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
        self.app.processEvents()
