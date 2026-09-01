"""Prepare and resume the C5 persistence matrix across one normal OS reboot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from project_package import ProjectPackageService
from project_save import ProjectSaveService, RecoveryAction, RecoveryPhase
from project_workspace import ProjectWorkspaceService
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    stage_selected_project_documents,
)
SCHEMA = "localcat.windows-c5-persistence-reboot-ticket.v2"
_PROJECT_WORKER = WORKSPACE_ROOT / "tests" / "windows_project_package_worker.py"
_RESOURCE_WORKER = WORKSPACE_ROOT / "tests" / "windows_resource_source_worker.py"
_SOURCE_FILES = (
    "platform_fs.py",
    "platform_fs_contracts.py",
    "platform_fs_windows.py",
    "windows_file_api.py",
    "project_package.py",
    "project_save.py",
    "project_workspace.py",
    "project_workspace_intake.py",
    "resource_artifact_save.py",
    "resource_importer.py",
    "resource_package.py",
    "resource_package_contracts.py",
    "resource_payload_port.py",
    "resource_platform_io.py",
    "resource_portability.py",
    "resource_receipt_ledger.py",
    "resource_repository.py",
    "termbase_store.py",
    "tm_activation_journal.py",
    "tm_activation_recovery.py",
    "tm_content_attestation.py",
    "tm_contracts.py",
    "tm_engine.py",
    "tm_migration.py",
    "tm_resource_port.py",
    "tm_snapshot_artifacts.py",
    "tm_snapshot_recovery.py",
    "tm_sqlite_store.py",
    "tm_stage_sealer.py",
    "parser_composition.py",
    "parser_contracts.py",
    "parser_registry.py",
    "parser_source.py",
    "parser_tmx_codec.py",
    "tm_application_composition.py",
    "tmx_application.py",
    "tmx_context_contracts.py",
    "tmx_context_interchange.py",
    "tmx_platform_io.py",
    "tmx_resource_package_handler.py",
    "editor_contracts.py",
    "editor_controller.py",
    "tests/windows_c5_persistence_reboot_worker.py",
    "tests/windows_project_package_worker.py",
    "tests/windows_resource_source_worker.py",
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _source_snapshot() -> list[dict[str, str]]:
    return [
        {
            "path": relative,
            "sha256": _sha256((WORKSPACE_ROOT / relative).read_bytes()),
        }
        for relative in _SOURCE_FILES
    ]


def _inventory(root: Path) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path.read_bytes()),
        }
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    ]


def _write_document(root: Path, name: str, source: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _canonical(
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
            }
        )
    )
    return path


def _prepare_project(root: Path) -> dict[str, object]:
    root.mkdir()
    first = _write_document(root, "chapters/a.json", "A")
    second = _write_document(root, "chapters/b.json", "B")
    staged = stage_selected_project_documents(
        root,
        (first, second),
        SelectedProjectDocumentsRequest("C5 reboot", "en", "zh-CN"),
    )
    workspace = ProjectWorkspaceService(
        staged.workspace,
        staged.origin_binding,
        session_id="c5-reboot",
        revision=1,
    )
    target = root / "project.localcat-project"
    exported = ProjectPackageService().save_workspace(
        ProjectSaveService(workspace, baseline=None),
        target,
    )
    if exported.receipt is None:
        raise AssertionError("project prepare did not issue a receipt")
    killed = subprocess.run(
        [
            sys.executable,
            str(_PROJECT_WORKER),
            "kill-save",
            str(target),
            RecoveryPhase.COMMIT_UNCERTAIN.value,
        ],
        check=False,
        cwd=root,
    )
    if killed.returncode != 73:
        raise AssertionError(f"project recovery worker exited {killed.returncode}")
    preview = ProjectPackageService().inspect_recovery(target)
    if preview is None or preview.phase is not RecoveryPhase.COMMIT_UNCERTAIN:
        raise AssertionError("project recovery journal was not retained")
    return {
        "operation_id": preview.operation_id,
        "project_id": staged.workspace.project_id,
        "target": target.name,
        "workspace_name": f"edited-{RecoveryPhase.COMMIT_UNCERTAIN.value}",
    }


def _run_resource(root: Path, mode: str, *arguments: object, expected: int = 0) -> Any:
    root.mkdir(exist_ok=True)
    cwd = root / f"non-repository-{mode}"
    cwd.mkdir(exist_ok=True)
    completed = subprocess.run(
        [
            sys.executable,
            str(_RESOURCE_WORKER),
            mode,
            str(root),
            *(str(value) for value in arguments),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if completed.returncode != expected:
        raise AssertionError(
            f"resource {mode} exited {completed.returncode}: {completed.stderr}"
        )
    if expected:
        return None
    return json.loads(completed.stdout)


def _ticket_path(root: Path) -> Path:
    return root / "reboot-ticket.json"


def _current_boot_session() -> str:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime."
                "ToUniversalTime().Ticks.ToString()"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    boot_session = completed.stdout.strip()
    if completed.returncode != 0 or not boot_session.isascii() or not boot_session.isdigit():
        raise RuntimeError("Windows boot session is unavailable")
    return boot_session


def prepare(root: Path) -> dict[str, object]:
    if not root.is_absolute() or root.exists():
        raise ValueError("prepare root must be a new absolute path")
    boot_session = _current_boot_session()
    root.mkdir(parents=True)
    project_root = root / "project"
    resource_root = root / "resource"
    resource_recovery_root = root / "resource-recovery"
    project = _prepare_project(project_root)
    resource = _run_resource(resource_root, "c5-prepare")
    _run_resource(
        resource_recovery_root,
        "crash-direct-ledger",
        3,
        expected=91,
    )
    unsigned = {
        "schema": SCHEMA,
        "boot_session": boot_session,
        "source_snapshot": _source_snapshot(),
        "expected": {
            "project": project,
            "resource": resource,
        },
        "inventories": {
            "project": _inventory(project_root),
            "resource": _inventory(resource_root),
            "resource_recovery": _inventory(resource_recovery_root),
        },
    }
    ticket = {**unsigned, "ticket_sha256": _sha256(_canonical(unsigned))}
    _ticket_path(root).write_bytes(_canonical(ticket))
    return {
        "reboot": "PREPARED",
        "schema": SCHEMA,
        "ticket_sha256": ticket["ticket_sha256"],
    }


def _load_ticket(root: Path) -> dict[str, object]:
    raw = _ticket_path(root).read_bytes()
    ticket = json.loads(raw.decode("utf-8"))
    if type(ticket) is not dict or _canonical(ticket) != raw:
        raise RuntimeError("C5 reboot ticket is not canonical")
    expected_keys = {
        "schema",
        "boot_session",
        "source_snapshot",
        "expected",
        "inventories",
        "ticket_sha256",
    }
    if set(ticket) != expected_keys or ticket["schema"] != SCHEMA:
        raise RuntimeError("C5 reboot ticket shape mismatch")
    unsigned = dict(ticket)
    digest = unsigned.pop("ticket_sha256")
    if digest != _sha256(_canonical(unsigned)):
        raise RuntimeError("C5 reboot ticket digest mismatch")
    return ticket


def resume(root: Path) -> dict[str, object]:
    ticket = _load_ticket(root)
    boot_session = _current_boot_session()
    if ticket["boot_session"] == boot_session:
        return {
            "reboot": "NOT_RUN",
            "schema": SCHEMA,
            "ticket_sha256": ticket["ticket_sha256"],
        }
    if ticket["source_snapshot"] != _source_snapshot():
        raise RuntimeError("C5 reboot source snapshot changed")
    expected = ticket["expected"]
    inventories = ticket["inventories"]
    if type(expected) is not dict or type(inventories) is not dict:
        raise RuntimeError("C5 reboot ticket bodies are invalid")
    roots = {
        "project": root / "project",
        "resource": root / "resource",
        "resource_recovery": root / "resource-recovery",
    }
    for role, role_root in roots.items():
        if inventories.get(role) != _inventory(role_root):
            raise RuntimeError(f"C5 reboot {role} bytes changed before owner reopen")

    project_expected = expected["project"]
    if type(project_expected) is not dict:
        raise RuntimeError("C5 reboot project expectation is invalid")
    project_service = ProjectPackageService()
    project_target = roots["project"] / str(project_expected["target"])
    preview = project_service.inspect_recovery(project_target)
    if (
        preview is None
        or preview.operation_id != project_expected["operation_id"]
        or preview.phase is not RecoveryPhase.COMMIT_UNCERTAIN
    ):
        raise RuntimeError("C5 reboot project recovery fact changed")
    report = project_service.recover(
        project_target,
        preview.operation_id,
        RecoveryAction.COMPLETE_COMMIT,
    )
    reopened_project = project_service.open(project_target)
    if (
        report.recovery_required
        or reopened_project.workspace.project_id != project_expected["project_id"]
        or reopened_project.workspace.name != project_expected["workspace_name"]
        or project_service.inspect_recovery(project_target) is not None
    ):
        raise RuntimeError("C5 reboot project recovery did not close")

    resource_expected = expected["resource"]
    reopened_resource = _run_resource(roots["resource"], "c5-reopen")
    if type(resource_expected) is not dict:
        raise RuntimeError("C5 reboot resource expectation is invalid")
    expected_tmx_import = resource_expected.get("tmx_import")
    expected_resource_reopen = dict(resource_expected)
    expected_resource_reopen.pop("tmx_import", None)
    if expected_tmx_import != {
        "imported": 1,
        "skipped": 0,
        "overwritten": 0,
        "errors": [],
    } or reopened_resource != expected_resource_reopen:
        raise RuntimeError("C5 reboot resource cold reopen changed")
    recovered_resource = _run_resource(
        roots["resource_recovery"],
        "inspect-crash",
    )
    if (
        recovered_resource["target_state"] != "new"
        or recovered_resource["recovery"] != "completed"
        or recovered_resource["pending_after"] != 0
        or recovered_resource["receipts_after"] != 1
    ):
        raise RuntimeError("C5 reboot resource recovery did not close")

    return {
        "project": "PASS",
        "reboot": "PASS",
        "resource": "PASS",
        "schema": SCHEMA,
        "ticket_sha256": ticket["ticket_sha256"],
        "tmx": "PASS",
    }


def main(arguments: list[str]) -> int:
    if len(arguments) != 2 or arguments[0] not in {"prepare", "resume"}:
        raise SystemExit("usage: worker prepare|resume ROOT")
    action, root_text = arguments
    root = Path(root_text)
    result = prepare(root) if action == "prepare" else resume(root)
    print(_canonical(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
