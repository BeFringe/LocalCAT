"""Real canonical owners can transfer from startup worker to the Qt thread."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication

from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import _initial_tm_activation_service
from qt_editor import (
    _compose_chunk_controller,
    _compose_editor_controller,
    _compose_tmx_export_service,
)
from qt_editor_window import QtEditorWindow
from qt_startup_loader import QtStartupLoader
from resource_repository import ResourceRepository
from tests.source_authority_support import current_source_authority
from tm_contracts import MigrationReport
import tm_engine


class QtStartupResourceHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_real_canonical_graph_is_usable_after_gui_thread_handoff(self) -> None:
        # Windows activation quarantine can retain long-path cleanup residues.
        # They are disposable fixture data, not a failure of the handoff contract.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            repository = ResourceRepository(root / "data")
            resource = repository.create_resource(
                "Startup canonical", ResourceKind.TRANSLATION_MEMORY
            )
            resource.path.write_text(
                '{"source":"Hello.","target":"Canonical target"}\n',
                encoding="utf-8",
            )
            outcome = _initial_tm_activation_service(resource).activate_initial(
                resource.path, resource.id
            )
            self.assertIs(type(outcome), MigrationReport, repr(outcome))
            authority = current_source_authority()
            gui_thread = threading.get_ident()
            published: list[object] = []
            failures: list[Exception] = []
            windows: list[QtEditorWindow] = []
            wait_loop = QEventLoop()
            deadline = QTimer()
            deadline.setSingleShot(True)
            deadline.timeout.connect(wait_loop.quit)

            def build() -> object:
                controller, capabilities = _compose_editor_controller(
                    repository, source_authority=authority
                )
                chunks = _compose_chunk_controller(controller, repository)
                exports = _compose_tmx_export_service(
                    controller, repository, chunks
                )
                return (
                    threading.get_ident(),
                    controller,
                    capabilities,
                    chunks,
                    exports,
                )

            def publish(result: object) -> None:
                worker_thread, controller, capabilities, chunks, exports = result
                self.assertNotEqual(worker_thread, gui_thread)
                self.assertEqual(threading.get_ident(), gui_thread)
                self.assertEqual(QThread.currentThread(), self.app.thread())
                self.assertFalse(controller.has_active_project)
                adapter = controller._tm_adapter
                self.assertIsNotNone(adapter)
                runtime = adapter._runtime_host
                store = runtime.snapshot().canonical_ports[0].handle.store
                self.assertIs(
                    controller._tm_engines[resource.id].canonical_store, store
                )
                # No operation/query view may keep its worker-local SQLite
                # connection or refresh gate alive across the ownership handoff.
                self.assertEqual(store.coordinator._active_lease_count, 0)
                self.assertIsNone(store.coordinator._refresh_gate_owner)
                self.assertEqual(store._query_view_tokens, set())

                window = QtEditorWindow(controller, chunk_controller=chunks)
                windows.append(window)
                window.tmx_export_coordinator = exports
                self.assertEqual(window.thread(), self.app.thread())
                self.assertTrue(store.health().healthy)
                self.assertEqual(
                    store.exact_records("Hello.")[0].target_raw,
                    "Canonical target",
                )
                controller.set_project(
                    EditorProject(
                        name="Handoff",
                        segments=(EditorSegment(id="one", source="Hello."),),
                    )
                )
                self.assertEqual(
                    controller.suggestions().tm_matches[0].target,
                    "Canonical target",
                )
                for field in ("lookup", "active", "update"):
                    changed = controller.update_resource(
                        replace(repository.get(resource.id), **{field: False})
                    )
                    self.assertFalse(getattr(changed, field))
                    self.assertIs(
                        runtime.snapshot().canonical_ports[0].handle.store, store
                    )
                    controller.update_resource(
                        replace(repository.get(resource.id), **{field: True})
                    )
                self.assertEqual(
                    controller.suggestions().tm_matches[0].target,
                    "Canonical target",
                )
                self.assertTrue(store.health().healthy)
                self.assertEqual(store.coordinator._active_lease_count, 0)
                self.assertIsNone(store.coordinator._refresh_gate_owner)
                self.assertEqual(store._query_view_tokens, set())
                published.append((controller, capabilities, chunks, exports))
                wait_loop.quit()

            def failed(error: Exception) -> None:
                failures.append(error)
                wait_loop.quit()

            loader = QtStartupLoader(build, publish, failed)
            try:
                with patch.object(
                    tm_engine,
                    "open_canonical_tm_store",
                    wraps=tm_engine.open_canonical_tm_store,
                ) as opened:
                    loader.start()
                    # Use the same Qt event-loop handoff as production. Repeated
                    # QtTest.qWait calls can starve Python worker code of the GIL.
                    deadline.start(60_000)
                    wait_loop.exec()
                    deadline.stop()
                    self.assertEqual(failures, [])
                    self.assertEqual(len(published), 1)
                    self.assertEqual(opened.call_count, 1)
                    self.assertFalse(loader.running)
            finally:
                loader.stop_publication()
                if loader._thread is not None:
                    loader._thread.join(60)
                    self.assertFalse(loader._thread.is_alive())
                for window in windows:
                    window.close()
                self.app.processEvents()
