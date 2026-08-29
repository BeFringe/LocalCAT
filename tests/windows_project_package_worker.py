"""Subprocess helper for WA-03 lock and restart acceptance."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from pathlib import PureWindowsPath
import sys
import time

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from platform_fs import compose_platform_file_backend
from platform_fs_contracts import LockPolicy, LockWait
import project_package


def _lock_name(target: Path) -> str:
    return project_package._windows_owner_stem(target.name) + ".lock"


def _hold_lock(root: Path, target: Path, ready: Path, release: Path) -> int:
    backend = compose_platform_file_backend(root)
    rooted = backend.bind_root(root)
    parent = backend.bind_parent(rooted, PureWindowsPath(target.name))
    lease = backend.acquire(
        parent,
        _lock_name(target),
        project_package._PROJECT_PACKAGE_LOCK_PAYLOAD,
        LockPolicy(LockWait.FAIL_FAST),
    )
    try:
        ready.write_text("ready", encoding="ascii")
        deadline = time.monotonic() + 30.0
        while not release.exists():
            if time.monotonic() >= deadline:
                return 4
            time.sleep(0.02)
        return 0
    finally:
        lease.close()
        parent.close()
        rooted.close()


def _kill_save(target: Path, phase: str) -> int:
    service = project_package.ProjectPackageService()
    opened = service.open(target)
    save_service = opened.create_save_service(session_id="wa03-worker", revision=3)
    save_service.workspace_service._workspace = replace(
        save_service.workspace_service.workspace,
        name=f"edited-{phase}",
    )
    original = project_package._WindowsProjectPackagePersistencePort._write_journal

    def kill_after_journal(port: object, handle: object) -> None:
        original(port, handle)
        if handle.phase.value == phase:
            os._exit(73)

    project_package._WindowsProjectPackagePersistencePort._write_journal = (
        kill_after_journal
    )
    service.save_workspace(
        save_service,
        target,
        persistence_binding=opened.persistence_binding,
    )
    return 5


def _controller_cold_open(target: Path, appdata: Path, marker: Path) -> int:
    from qt_editor import _compose_editor_controller
    from resource_repository import ResourceRepository

    controller, _composition = _compose_editor_controller(ResourceRepository(appdata))
    view = controller.open_project_package(target)
    marker.write_text(
        json.dumps(
            {
                "document_count": len(view.documents),
                "project_id": view.project.project_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return 0


def main(arguments: list[str]) -> int:
    if arguments[0] == "hold-lock":
        return _hold_lock(*(Path(value) for value in arguments[1:5]))
    if arguments[0] == "kill-save":
        return _kill_save(Path(arguments[1]), arguments[2])
    if arguments[0] == "controller-cold-open":
        return _controller_cold_open(*(Path(value) for value in arguments[1:4]))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
