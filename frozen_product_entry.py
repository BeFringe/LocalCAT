"""Product composition after the platform producer has completed Boot TCB."""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


def _seed_default_resources(backend: Any, data_dir: Path, defaults: dict[str, bytes]) -> None:
    from platform_fs_contracts import PublishMode
    from uuid import uuid4
    import hashlib

    root = backend.bind_root(data_dir)
    try:
        for name, payload in defaults.items():
            if root.inspect_entry(name) is not None:
                continue
            candidate = None
            pending = None
            candidate_name = ".localcat-default-" + uuid4().hex + ".tmp"
            candidate_identity = None
            try:
                candidate = root.create_candidate(candidate_name, private=True)
                candidate_identity = candidate.identity()
                candidate.write_all(payload)
                candidate.flush_content()
                pending = root.begin_publish(candidate, name, mode=PublishMode.CREATE_IF_ABSENT, lease=None)
                candidate = None
                candidate_identity = None
                facts = pending.preliminary_facts()
                if (facts.byte_count != len(payload) or facts.content_sha256 != hashlib.sha256(payload).digest()
                        or pending.retained_destination().read_all() != payload
                        or pending.terminal_reproof() != facts):
                    raise RuntimeError("default resource publication could not be proved")
            finally:
                if candidate is not None:
                    candidate.close()
                if candidate_identity is not None:
                    root.unlink_owned(candidate_name, candidate_identity)
                if pending is not None:
                    pending.close()
    finally:
        root.close()


def run_product(authority: Any) -> int:
    from tm_gate_inputs import _compose_frozen_input_owner, _require_native_frozen_producer

    _require_native_frozen_producer(authority)
    # These imports happen only after the product/worker selection in E10.
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    from platform_fs import compose_platform_file_backend
    from qt_editor import (
        APPLICATION_ICON_FILENAME, APPLICATION_VERSION, _compose_chunk_controller,
        _compose_editor_controller, _compose_tmx_export_service,
        _stabilize_windows_tooltips, _start_capability_validation, default_data_dir,
    )
    from qt_editor_window import QtEditorWindow
    from qt_resource_contracts import QtResourceBytes, SourceQtResources
    from qt_speaker_avatar import SpeakerAvatarCatalog, resource_png_pixmap
    from resource_repository import ResourceRepository

    owner = _compose_frozen_input_owner(authority)
    try:
        with owner.open_session() as session:
            logo = QtResourceBytes(filename=APPLICATION_ICON_FILENAME, content=session.read_bytes(APPLICATION_ICON_FILENAME))
            avatars = tuple(QtResourceBytes(filename=Path(name).name, content=session.read_bytes(name))
                            for name in authority._resource_names("speaker_avatars/") if name.endswith("Half.png"))
            resources = SourceQtResources(logo=logo, speaker_avatars=avatars)
            defaults = {name: session.read_bytes(name) for name in ("tm.jsonl", "terms.csv")}
            session.terminal_reproof()
    finally:
        owner.close()

    QApplication.setApplicationName("LocalCAT")
    QApplication.setApplicationDisplayName("LocalCAT")
    QApplication.setOrganizationName("LocalCAT")
    QApplication.setApplicationVersion(APPLICATION_VERSION)
    QApplication.setLibraryPaths([str(authority._qt_plugin_root())])
    app = QApplication([sys.executable])
    app.setDesktopFileName("localcat")
    _stabilize_windows_tooltips(app)
    pixmap = resource_png_pixmap(resources.logo)
    if pixmap is None:
        raise ValueError("retained Qt logo is invalid")
    app.setWindowIcon(QIcon(pixmap))
    data_dir = default_data_dir().expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    backend = compose_platform_file_backend(data_dir)
    _seed_default_resources(backend, data_dir, defaults)
    repository = ResourceRepository(data_dir, default_tm_path=data_dir / "tm.jsonl",
                                    default_termbase_path=data_dir / "terms.csv", backend=backend)
    controller, composition = _compose_editor_controller(repository, trusted_source_authority=authority)
    chunks = _compose_chunk_controller(controller, repository)
    exports = _compose_tmx_export_service(controller, repository, chunks)
    window = QtEditorWindow(controller, chunk_controller=chunks,
                            speaker_avatar_catalog=SpeakerAvatarCatalog(resources.speaker_avatars))
    window.tmx_export_coordinator = exports
    window.show()
    validation: Any = _start_capability_validation(composition, window)
    app.processEvents()
    try:
        return app.exec()
    finally:
        validation.cancel()
        authority.close()
