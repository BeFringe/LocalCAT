"""Real-process WA-02 worker for Chunk source composition and crash tests."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _write_result(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _wait(path: Path) -> None:
    deadline = time.monotonic() + 30.0
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("worker barrier timeout")
        time.sleep(0.01)


def _runtime(root: Path):
    from tests.test_collaborative_chunks_cluster1_store import _DurableRuntime

    return _DurableRuntime(root)


def _seed(root: Path, result: Path) -> None:
    runtime = _runtime(root)
    chunk_id = runtime.create("old", (runtime.a, runtime.b))
    _write_result(result, {"chunk_id": chunk_id, "outcome": "ok"})


def _race_create(root: Path, ready: Path, barrier: Path, result: Path) -> None:
    from collaborative_chunk_contracts import TopologyAction, canonicalize_chunk_members

    runtime = _runtime(root)
    capability, expected = runtime.capability(TopologyAction.CREATE)
    preview = runtime.topology.preview_create(
        capability,
        runtime.manager,
        workspace_binding=runtime.binding,
        expected_plan_binding=expected,
        name="raced",
        members=canonicalize_chunk_members((runtime.a,)),
    )
    ready.write_bytes(b"ready")
    _wait(barrier)
    try:
        receipt = runtime.apply(preview, capability, expected)
    except Exception as error:
        code = getattr(error, "code", type(error).__name__)
        _write_result(result, {"outcome": code})
    else:
        _write_result(
            result,
            {"operation_id": receipt.operation_id, "outcome": "ok"},
        )


def _fault_rename(root: Path, phase: str, ready: Path) -> None:
    from collaborative_chunk_contracts import TopologyAction

    runtime = _runtime(root)
    runtime.operation_issuer.value = 300
    state = runtime.store().load()
    if state is None or state.active_snapshot is None:
        raise RuntimeError("seeded Chunk state is unavailable")
    chunk_id = state.active_snapshot.chunks[0].chunk_id

    def fault(current: str) -> None:
        if current == phase:
            ready.write_text(current, encoding="utf-8")
            while True:
                time.sleep(60)

    runtime.fault = fault
    capability, expected = runtime.capability(TopologyAction.RENAME)
    preview = runtime.topology.preview_rename(
        capability,
        runtime.manager,
        workspace_binding=runtime.binding,
        expected_plan_binding=expected,
        chunk_id=chunk_id,
        name="new",
    )
    runtime.apply(preview, capability, expected)
    raise RuntimeError("selected fault phase was not reached")


def _load(root: Path, result: Path) -> None:
    try:
        state = _runtime(root).store().load()
    except Exception as error:
        _write_result(result, {"outcome": getattr(error, "code", type(error).__name__)})
    else:
        name = None
        if state is not None and state.active_snapshot is not None:
            name = state.active_snapshot.chunks[0].name
        _write_result(result, {"name": name, "outcome": "ok"})


def _qt_startup(app_data: Path, result: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from qt_editor import _compose_chunk_controller, _compose_editor_controller
    from qt_editor_window import QtEditorWindow
    from resource_repository import ResourceRepository

    app = QApplication.instance() or QApplication([])
    repository = ResourceRepository(app_data)
    controller, _composition = _compose_editor_controller(repository)
    chunks = _compose_chunk_controller(controller, repository)
    window = QtEditorWindow(controller, chunk_controller=chunks)
    window.show()
    app.processEvents()
    window.close()
    app.processEvents()
    if "fcntl" in sys.modules or "platform_fs_posix" in sys.modules:
        raise AssertionError("POSIX-only module was loaded during Windows startup")
    _write_result(result, {"outcome": "ok", "platform": sys.platform})


def _package_adapter(package: Path, app_data: Path):
    from qt_editor import _compose_chunk_controller, _compose_editor_controller
    from resource_repository import ResourceRepository

    repository = ResourceRepository(app_data)
    controller, _composition = _compose_editor_controller(repository)
    adapter = _compose_chunk_controller(controller, repository)
    adapter.open_project_package(package)
    return adapter


def _package_create(
    package: Path,
    app_data: Path,
    result: Path,
) -> None:
    adapter = _package_adapter(package, app_data)
    selected = []
    seen_documents = set()
    for choice in adapter.segment_choices():
        if choice.identity.document_id in seen_documents:
            continue
        selected.append(choice.identity)
        seen_documents.add(choice.identity.document_id)
    if len(selected) < 2:
        raise AssertionError("package acceptance requires two documents")
    receipt = adapter.apply_mutation(
        adapter.preview_create_chunk("Windows package slice", tuple(selected))
    )
    view = adapter.project_view()
    _write_result(
        result,
        {
            "chunk_count": len(view.chunks),
            "chunk_name": view.chunks[0].name,
            "member_count": view.chunks[0].member_count,
            "operation_id": receipt.operation_id,
            "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "project_id": view.project_id,
        },
    )


def _package_open(
    package: Path,
    app_data: Path,
    result: Path,
) -> None:
    adapter = _package_adapter(package, app_data)
    view = adapter.project_view()
    _write_result(
        result,
        {
            "chunk_count": len(view.chunks),
            "chunk_name": None if not view.chunks else view.chunks[0].name,
            "member_count": 0 if not view.chunks else view.chunks[0].member_count,
            "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "project_id": view.project_id,
        },
    )


def main() -> int:
    mode = sys.argv[1]
    if mode == "seed":
        _seed(Path(sys.argv[2]), Path(sys.argv[3]))
    elif mode == "race-create":
        _race_create(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
            Path(sys.argv[5]),
        )
    elif mode == "fault-rename":
        _fault_rename(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
    elif mode == "load":
        _load(Path(sys.argv[2]), Path(sys.argv[3]))
    elif mode == "qt-startup":
        _qt_startup(Path(sys.argv[2]), Path(sys.argv[3]))
    elif mode == "package-create":
        _package_create(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
        )
    elif mode == "package-open":
        _package_open(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
        )
    else:
        raise ValueError("unknown worker mode")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
