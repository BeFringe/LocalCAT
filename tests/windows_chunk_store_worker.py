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


def _selected_documents(adapter: object) -> tuple[object, ...]:
    selected = []
    seen_documents: set[str] = set()
    for choice in adapter.segment_choices():
        document_id = choice.identity.document_id
        if document_id in seen_documents:
            continue
        selected.append(choice.identity)
        seen_documents.add(document_id)
    if len(selected) < 2:
        raise AssertionError("package acceptance requires two documents")
    return tuple(selected)


def _workspace_facts(controller: object) -> dict[str, object]:
    owner = controller._workspace_owner_for_chunk_controller()
    view = controller.workspace_view
    save = controller.workspace_save_state
    return {
        "project_id": view.project.project_id,
        "workspace_digest": owner.workspace_digest,
        "workspace_content_digest": owner.workspace_content_digest,
        "segments": [
            {
                "document_id": item.identity.document.document_id,
                "local_segment_id": item.identity.local_segment_id,
                "document_local_index": item.document_local_index,
                "project_global_index": item.project_global_index,
                "source": item.source,
                "target": item.target,
                "confirmed": item.confirmed,
            }
            for item in view.segments
        ],
        "navigation": {
            "document_id": view.current_segment.document.document_id,
            "local_segment_id": view.current_segment.local_segment_id,
            "global_index": controller.workspace_global_index,
        },
        "dirty": {
            "dirty_document_ids": list(save.dirty_document_ids),
            "manifest_dirty": save.manifest_dirty,
            "project_dirty": save.project_dirty,
        },
    }


def _metadata_facts(state: object | None) -> dict[str, object]:
    from collaborative_chunk_contracts import chunk_plan_digest_v1

    if state is None:
        return {
            "active_snapshot": None,
            "active_name": None,
            "audit_head": None,
            "audit_records": [],
            "chunk_count": 0,
        }
    active = state.active_snapshot
    return {
        "active_snapshot": (
            None
            if active is None
            else {
                "schema_version": active.schema_version,
                "namespace": active.namespace,
                "chunk_plan_id": active.chunk_plan_id,
                "project_id": active.project_id,
                "revision": active.revision,
                "segment_universe_digest": active.segment_universe_digest,
                "audit_head_digest": active.audit_head_digest,
                "plan_digest": chunk_plan_digest_v1(active),
                "chunks": [
                    {
                        "chunk_id": chunk.chunk_id,
                        "name": chunk.name,
                        "order": chunk.order,
                        "members": [
                            {
                                "project_id": member.project_id,
                                "document_id": member.identity.document_id,
                                "local_segment_id": member.identity.local_segment_id,
                            }
                            for member in chunk.members
                        ],
                        "assignee": (
                            None
                            if chunk.assignee is None
                            else {
                                "authority_id": chunk.assignee.authority_id,
                                "subject_id": chunk.assignee.subject_id,
                            }
                        ),
                    }
                    for chunk in active.chunks
                ],
            }
        ),
        "active_name": None if active is None else active.chunks[0].name,
        "audit_head": None if active is None else active.audit_head_digest,
        "audit_records": [
            {
                "record_digest": record.record_digest,
                "previous_audit_head_digest": record.previous_audit_head_digest,
                "outcome": record.outcome,
                "receipt": {
                    "operation_id": record.receipt.operation_id,
                    "action": record.receipt.action.value,
                    "project_id": record.receipt.project_id,
                    "chunk_plan_id": record.receipt.chunk_plan_id,
                    "base_revision": record.receipt.base_revision,
                    "published_revision": record.receipt.published_revision,
                    "before_plan_digest": record.receipt.before_plan_digest,
                    "after_plan_digest": record.receipt.after_plan_digest,
                    "affected_chunk_ids": list(record.receipt.affected_chunk_ids),
                    "created_chunk_ids": list(record.receipt.created_chunk_ids),
                    "retired_chunk_ids": list(record.receipt.retired_chunk_ids),
                    "affected_chunk_count": record.receipt.affected_chunk_count,
                    "created_chunk_count": record.receipt.created_chunk_count,
                    "retired_chunk_count": record.receipt.retired_chunk_count,
                    "affected_member_count": record.receipt.affected_member_count,
                    "assignment_count": record.receipt.assignment_count,
                    "safe_issues": list(record.receipt.safe_issues),
                    "truncated": record.receipt.truncated,
                    "audit_record_digest": record.receipt.audit_record_digest,
                    "actor_ref": {
                        "authority_id": record.receipt.actor_ref.authority_id,
                        "subject_id": record.receipt.actor_ref.subject_id,
                    },
                },
            }
            for record in state.audit_records
        ],
        "chunk_count": 0 if active is None else len(active.chunks),
    }


def _package_facts_from_adapter(
    adapter: object,
    package: Path,
    app_data: Path,
) -> dict[str, object]:
    authority = adapter._authority
    if authority is None:
        raise AssertionError("package adapter did not bind metadata authority")
    store = authority._ChunkTopologyPublicationAuthority__metadata_store
    if store is None:
        raise AssertionError("package adapter did not bind metadata store")
    controller = adapter._controller
    project_id = controller.workspace_view.project.project_id
    metadata_root = app_data / "collaborative-chunks" / project_id
    return {
        "metadata": _metadata_facts(store.load()),
        "metadata_digest": store.current_digest(),
        "workspace": _workspace_facts(controller),
        "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "metadata_files": sorted(path.name for path in metadata_root.iterdir()),
    }


def _package_create(
    package: Path,
    app_data: Path,
    result: Path,
) -> None:
    adapter = _package_adapter(package, app_data)
    selected = _selected_documents(adapter)
    receipt = adapter.apply_mutation(
        adapter.preview_create_chunk("Windows package slice", selected)
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
            "facts": _package_facts_from_adapter(adapter, package, app_data),
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
            "facts": _package_facts_from_adapter(adapter, package, app_data),
        },
    )


def _install_blocking_fault(
    adapter: object,
    phase: str,
    ready: Path,
    marker: dict[str, object],
) -> None:
    authority = adapter._authority
    if authority is None:
        raise AssertionError("package adapter did not bind metadata authority")
    store = authority._ChunkTopologyPublicationAuthority__metadata_store
    if store is None:
        raise AssertionError("package adapter did not bind metadata store")

    def fault(current: str) -> None:
        if current != phase:
            return
        ready.write_text(
            json.dumps(marker, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        while True:
            time.sleep(60)

    store._CollaborativeChunkStore__fault_injector = fault


def _package_fault(
    package: Path,
    app_data: Path,
    phase: str,
    ready: Path,
    *,
    initial: bool,
) -> None:
    adapter = _package_adapter(package, app_data)
    if initial:
        selected = _selected_documents(adapter)
        preview = adapter.preview_create_chunk(
            "Windows initial creator", selected
        )
    else:
        selected = ()
        view = adapter.project_view()
        if len(view.chunks) != 1:
            raise AssertionError("package fault acceptance requires one seed chunk")
        preview = adapter.preview_rename_chunk(view.chunks[0].chunk_id, "Windows renamed")
    prepared = adapter._pending_application[id(preview)]
    private = prepared.private_preview
    live = adapter._live_universe()
    marker = {
        "phase": phase,
        "segment_universe_digest": live.binding.segment_universe_digest,
        "selected_members": [
            {
                "project_id": preview.project_id,
                "document_id": identity.document_id,
                "local_segment_id": identity.local_segment_id,
            }
            for identity in selected
        ],
        "preview": {
            "operation_id": private.operation_id,
            "action": private.action.value,
            "project_id": private.project_id,
            "chunk_plan_id": private.chunk_plan_id,
            "base_revision": private.base_revision,
            "published_revision": private.published_revision,
            "before_plan_digest": private.before_plan_digest,
            "after_plan_digest": private.after_plan_digest,
            "affected_chunk_ids": list(private.affected_chunk_ids),
            "created_chunk_ids": list(private.created_chunk_ids),
            "retired_chunk_ids": list(private.retired_chunk_ids),
            "affected_chunk_count": private.affected_chunk_count,
            "created_chunk_count": private.created_chunk_count,
            "retired_chunk_count": private.retired_chunk_count,
            "affected_member_count": private.affected_member_count,
            "assignment_count": private.assignment_count,
            "safe_issues": list(private.warnings),
            "truncated": private.truncated,
        },
    }
    _install_blocking_fault(adapter, phase, ready, marker)
    adapter.apply_mutation(preview)
    raise RuntimeError("selected fault phase was not reached")


def _package_recover(package: Path, app_data: Path, result: Path) -> None:
    from collaborative_chunk_store import CollaborativeChunkStore
    from platform_fs import compose_platform_file_backend
    from qt_editor import _compose_editor_controller
    from resource_repository import ResourceRepository

    repository = ResourceRepository(app_data)
    controller, _composition = _compose_editor_controller(repository)
    controller.open_project_package(package)
    project_id = controller.workspace_view.project.project_id
    metadata_root = app_data / "collaborative-chunks" / project_id
    store = CollaborativeChunkStore(
        metadata_root,
        "chunks.json",
        project_id=project_id,
        platform_backend=compose_platform_file_backend(metadata_root),
    )
    report = store.recover()
    # Re-open through the public composition after recovery; direct store
    # recovery alone is not the package-coupled proof.
    adapter = _package_adapter(package, app_data)
    facts = _package_facts_from_adapter(adapter, package, app_data)
    if facts["metadata_digest"] != report.metadata_digest:
        raise AssertionError("public composition reopened a different metadata state")
    _write_result(
        result,
        {
            "outcome": report.outcome,
            "metadata_digest": report.metadata_digest,
            "facts": facts,
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
    elif mode == "package-fault":
        _package_fault(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            sys.argv[4],
            Path(sys.argv[5]),
            initial=False,
        )
    elif mode == "package-initial-fault":
        _package_fault(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            sys.argv[4],
            Path(sys.argv[5]),
            initial=True,
        )
    elif mode == "package-recover":
        _package_recover(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
        )
    else:
        raise ValueError("unknown worker mode")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
