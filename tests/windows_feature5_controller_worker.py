"""Fresh-process Windows Feature5 Controller surface worker."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceConfig,
    ResourceKind,
)
from resource_repository import ResourceRepository
import tm_activation_journal
from tm_contracts import CanonicalResourceIdentity, MigrationFailure, MigrationReport
import tm_migration
from tm_migration import MigrationPreflightError, TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator


_QUERY_SOURCE = "Hello"
_CANONICAL_TARGET = "canonical-primary"
_HEALTHY_TARGET = "healthy-peer"
_PRIVATE_BODY = "PRIVATE_BODY_MUST_NOT_LEAK"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _resource_by_name(
    repository: ResourceRepository,
    name: str,
) -> ResourceConfig:
    matches = tuple(
        resource
        for resource in repository.list_resources()
        if resource.name == name
    )
    if len(matches) != 1:
        raise AssertionError(f"expected one {name!r} resource")
    return matches[0]


def _prepare_repository(
    root: Path,
) -> tuple[ResourceRepository, ResourceConfig, ResourceConfig]:
    repository = ResourceRepository(root / "app-data")
    primary = repository.create_resource("Primary TM", ResourceKind.TRANSLATION_MEMORY)
    healthy = repository.create_resource("Healthy TM", ResourceKind.TRANSLATION_MEMORY)
    primary.path.write_text(
        _canonical_json({"source": _QUERY_SOURCE, "target": _CANONICAL_TARGET})
        + "\n",
        encoding="utf-8",
    )
    healthy.path.write_text(
        _canonical_json({"source": _QUERY_SOURCE, "target": _HEALTHY_TARGET})
        + "\n",
        encoding="utf-8",
    )
    return repository, primary, healthy


def _activation_owner(
    primary: ResourceConfig,
) -> tuple[ResourceStoreCoordinator, TMMigrationService]:
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        primary.id,
        primary.path,
    )
    coordinator = ResourceStoreCoordinator(
        canonical_store_id=f"store.{primary.id}",
        resource_identity=identity,
    )
    return coordinator, TMMigrationService(
        resource_identity=identity,
        canonical_store_id=f"store.{primary.id}",
        coordinator=coordinator,
    )


def _outcome_projection(outcome: object) -> dict[str, object]:
    if type(outcome) is MigrationReport:
        report = outcome
        assert isinstance(report, MigrationReport)
        return {
            "kind": "MigrationReport",
            "generation": report.activated_generation,
        }
    if type(outcome) is MigrationFailure:
        failure = outcome
        assert isinstance(failure, MigrationFailure)
        return {
            "kind": "MigrationFailure",
            "error_code": failure.error_code,
            "published": failure.canonical_authority_published,
            "ambiguous": failure.canonical_authority_ambiguous,
        }
    raise AssertionError("activation returned an unsupported outcome")


def _prepare(root: Path, scenario: str) -> dict[str, object]:
    _repository, primary, _healthy = _prepare_repository(root)
    _coordinator, service = _activation_owner(primary)
    if scenario == "success":
        outcome = service.activate_initial(primary.path, primary.id)
    elif scenario == "rollback":
        with mock.patch.object(
            TMMigrationService,
            "_build_stage",
            autospec=True,
            side_effect=OSError(_PRIVATE_BODY),
        ):
            outcome = service.activate_initial(primary.path, primary.id)
    else:
        raise AssertionError("prepare scenario is unsupported")
    return {
        "mode": "prepare",
        "pid": os.getpid(),
        "scenario": scenario,
        "outcome": _outcome_projection(outcome),
    }


def _park(root: Path, phase: str, marker: Path) -> dict[str, object]:
    _repository, primary, _healthy = _prepare_repository(root)
    _coordinator, service = _activation_owner(primary)
    real_publish = tm_activation_journal._WindowsPortablePublicationPhaseOwner.publish

    def park_now() -> None:
        marker.write_text(
            _canonical_json(
                {
                    "phase": phase,
                    "pid": os.getpid(),
                    "schema": "feature5-phase-v1",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        while True:
            time.sleep(1.0)

    def publish_and_park(**kwargs: object) -> object:
        handle = real_publish(**kwargs)
        unsigned = kwargs.get("unsigned")
        if (
            isinstance(
                unsigned,
                tm_activation_journal._PortablePublicationPhaseUnsigned,
            )
            and unsigned.phase == phase
        ):
            park_now()
        return handle

    if phase in {"DB_REPLACED", "GENERATION_PUBLISHED"}:
        with mock.patch.object(
            tm_activation_journal._WindowsPortablePublicationPhaseOwner,
            "publish",
            new=publish_and_park,
        ):
            outcome = service.activate_initial(primary.path, primary.id)
    elif phase == "STAGE_RETIRED":
        real_cleanup = tm_migration._cleanup_initial_unpublished_stage

        def cleanup_and_park(attempt: object) -> None:
            real_cleanup(attempt)
            park_now()

        with mock.patch.object(
            tm_migration,
            "_cleanup_initial_unpublished_stage",
            new=cleanup_and_park,
        ):
            outcome = service.activate_initial(primary.path, primary.id)
    elif phase == "LINEAGE_MARKER_PUBLISHED":
        real_marker = ResourceStoreCoordinator._ensure_portable_activation_lineage_marker

        def marker_and_park(
            coordinator: ResourceStoreCoordinator,
            *args: object,
            **kwargs: object,
        ) -> None:
            real_marker(coordinator, *args, **kwargs)
            park_now()

        with mock.patch.object(
            ResourceStoreCoordinator,
            "_ensure_portable_activation_lineage_marker",
            new=marker_and_park,
        ):
            outcome = service.activate_initial(primary.path, primary.id)
    else:
        raise AssertionError("park phase is unsupported")
    return {
        "mode": "park",
        "pid": os.getpid(),
        "phase": phase,
        "outcome": _outcome_projection(outcome),
    }


def _poison_source(root: Path) -> dict[str, object]:
    repository = ResourceRepository(root / "app-data")
    primary = _resource_by_name(repository, "Primary TM")
    primary.path.write_text(
        _canonical_json({"source": _QUERY_SOURCE, "target": _PRIVATE_BODY}) + "\n",
        encoding="utf-8",
    )
    return {"mode": "poison-source", "pid": os.getpid()}


def _corrupt_private_owner(root: Path) -> dict[str, object]:
    repository = ResourceRepository(root / "app-data")
    primary = _resource_by_name(repository, "Primary TM")
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        primary.id,
        primary.path,
    )
    private_root = (
        primary.path.parent
        / tm_activation_journal._portable_activation_private_directory_name(
            identity
        )
    )
    generation = (
        private_root
        / tm_activation_journal._portable_publication_phase_name(
            "GENERATION_PUBLISHED"
        )
    )
    if not generation.is_file():
        raise AssertionError("completed generation private record is missing")
    replacement = primary.path.parent / "foreign-private-generation"
    replacement.write_bytes(generation.read_bytes())
    os.replace(replacement, generation)
    return {"mode": "corrupt-private", "pid": os.getpid()}


def _setup_arbitrary_sidecar(root: Path) -> dict[str, object]:
    _repository, primary, _healthy = _prepare_repository(root)
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        primary.id,
        primary.path,
    )
    identity.canonical_sidecar_path.write_bytes(b"arbitrary-unowned-sidecar\n")
    lock = primary.path.with_name(
        f".{primary.path.name}.localcat-initial-activation.lock"
    )
    private_root = (
        primary.path.parent
        / tm_activation_journal._portable_activation_private_directory_name(
            identity
        )
    )
    if lock.exists() or private_root.exists():
        raise AssertionError("arbitrary sidecar setup unexpectedly owns portable facts")
    return {
        "mode": "setup-arbitrary-sidecar",
        "pid": os.getpid(),
        "sidecar_name": identity.canonical_sidecar_path.name,
    }


def _preflight_pending(root: Path) -> dict[str, object]:
    repository = ResourceRepository(root / "app-data")
    primary = _resource_by_name(repository, "Primary TM")
    _coordinator, service = _activation_owner(primary)
    try:
        _ = service.preflight_pending_recovery(primary.path)
    except MigrationPreflightError as error:
        error_code = error.error_code
        issued = False
    else:
        error_code = None
        issued = True
    return {
        "mode": "preflight-pending",
        "pid": os.getpid(),
        "error_code": error_code,
        "issued": issued,
    }


def _report_projection(report: object) -> dict[str, object]:
    suggestions = getattr(report, "suggestions")
    statuses = getattr(report, "resource_statuses")
    return {
        "epoch": getattr(getattr(report, "query_identity"), "query_epoch"),
        "statuses": [
            {
                "resource_id": status.resource_id,
                "resource_name": status.resource_name,
                "mode": status.mode.value,
                "safe_codes": list(status.safe_codes),
            }
            for status in statuses
        ],
        "suggestions": [
            {
                "resource_id": suggestion.resource_id,
                "record_id": suggestion.record_id,
                "target": suggestion.target,
                "resource_mode": suggestion.provenance.resource_mode.value,
            }
            for suggestion in suggestions
        ],
    }


def _status_projection(statuses: object) -> list[dict[str, object]]:
    return [
        {
            "resource_id": status.resource_id,
            "resource_name": status.resource_name,
            "mode": status.mode.value,
            "safe_codes": list(status.safe_codes),
        }
        for status in statuses
    ]


def _probe(
    root: Path,
    *,
    prepare_primary: bool,
    qt_activate_primary: bool,
) -> dict[str, object]:
    from qt_editor import _compose_editor_controller

    repository = ResourceRepository(root / "app-data")
    controller, _capability = _compose_editor_controller(repository)
    controller.set_project(
        EditorProject(
            name="Windows Feature5 fresh process",
            segments=(EditorSegment(id="segment-1", source=_QUERY_SOURCE),),
        )
    )
    before = controller.tm_suggestion_report()
    lifecycle_before = _status_projection(controller.tm_resource_statuses())
    prepare_projection: dict[str, object] | None = None
    operation_projection: dict[str, object] | None = None
    qt_projection: dict[str, object] | None = None
    if prepare_primary:
        from editor_controller import EditorControllerError

        primary = _resource_by_name(repository, "Primary TM")
        try:
            preflight = controller.prepare_tm_activation(primary.id)
        except EditorControllerError as error:
            prepare_projection = {
                "error_code": str(error),
                "issued": False,
                "prepared_action": None,
            }
        else:
            prepared = controller._prepared_tm_activation
            prepare_projection = {
                "error_code": None,
                "issued": True,
                "prepared_action": None if prepared is None else prepared.action,
            }
            controller.cancel_tm_activation(preflight)
    if qt_activate_primary:
        from PySide6.QtCore import QCoreApplication, QEventLoop
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QApplication, QMessageBox
        from qt_settings_dialog import QtSettingsDialog

        primary = _resource_by_name(repository, "Primary TM")
        app = QApplication.instance() or QApplication([])
        dialog = QtSettingsDialog(controller)
        dialog.show()
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
        action = dialog.findChild(QAction, f"tmLifecycleAction_{primary.id}")
        if action is None:
            raise AssertionError("Qt TM lifecycle action is missing")
        action_enabled = action.isEnabled()
        action_text = action.text()
        prompt_count = 0

        def confirm(*_args: object, **_kwargs: object) -> QMessageBox.StandardButton:
            nonlocal prompt_count
            prompt_count += 1
            return QMessageBox.StandardButton.Yes

        with mock.patch(
            "qt_settings_dialog._ask_localized_question",
            side_effect=confirm,
        ):
            action.trigger()
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
            deadline = time.monotonic() + 120.0
            while dialog._tm_operation_id is not None:
                dialog._poll_tm_operation()
                QCoreApplication.processEvents(
                    QEventLoop.ProcessEventsFlag.AllEvents
                )
                if time.monotonic() >= deadline:
                    raise AssertionError("Qt TM operation poll timed out")
                time.sleep(0.01)
        completed = controller.tm_activation_operation()
        if completed is not None:
            operation_projection = {
                "phase": completed.phase,
                "safe_code": completed.safe_code,
                "succeeded": completed.succeeded,
            }
        qt_projection = {
            "action_enabled": action_enabled,
            "action_text": action_text,
            "prompt_count": prompt_count,
            "status_text": dialog.status_label.text(),
        }
        dialog.close()
        app.processEvents()
    first = controller.tm_suggestion_report()
    second = controller.tm_suggestion_report()
    membership = controller.issued_tm_suggestions

    projection = _report_projection(first)
    serialized = _canonical_json(projection)
    return {
        "activation": operation_projection,
        "before": _report_projection(before),
        "lifecycle_before": lifecycle_before,
        "mode": (
            "probe-qt"
            if qt_activate_primary
            else "probe-prepare"
            if prepare_primary
            else "probe"
        ),
        "pid": os.getpid(),
        "prepare": prepare_projection,
        "qt": qt_projection,
        "report": projection,
        "repeat_equal": second == first,
        "membership_equal": membership == first.suggestions,
        "private_body_visible": _PRIVATE_BODY in serialized,
    }


def main(arguments: list[str]) -> int:
    if Path.cwd().resolve() == SOURCE_ROOT or (Path.cwd() / ".git").exists():
        raise AssertionError("fresh worker requires a non-repository CWD")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=(
            "prepare",
            "park",
            "setup-arbitrary-sidecar",
            "preflight-pending",
            "poison-source",
            "corrupt-private",
            "probe",
            "probe-prepare",
            "probe-qt",
        ),
    )
    parser.add_argument("root", type=Path)
    parser.add_argument("--scenario", choices=("success", "rollback"))
    parser.add_argument(
        "--phase",
        choices=(
            "DB_REPLACED",
            "GENERATION_PUBLISHED",
            "STAGE_RETIRED",
            "LINEAGE_MARKER_PUBLISHED",
        ),
    )
    parser.add_argument("--marker", type=Path)
    parsed = parser.parse_args(arguments)
    root = parsed.root.resolve()
    if parsed.mode in {"prepare", "park", "setup-arbitrary-sidecar"}:
        root.mkdir(parents=True, exist_ok=False)
    if parsed.mode == "prepare":
        if parsed.scenario is None:
            parser.error("prepare requires --scenario")
        result = _prepare(root, parsed.scenario)
    elif parsed.mode == "park":
        if parsed.phase is None or parsed.marker is None:
            parser.error("park requires --phase and --marker")
        result = _park(root, parsed.phase, parsed.marker.resolve())
    elif parsed.mode == "setup-arbitrary-sidecar":
        result = _setup_arbitrary_sidecar(root)
    elif parsed.mode == "preflight-pending":
        result = _preflight_pending(root)
    elif parsed.mode == "poison-source":
        result = _poison_source(root)
    elif parsed.mode == "corrupt-private":
        result = _corrupt_private_owner(root)
    else:
        result = _probe(
            root,
            prepare_primary=parsed.mode == "probe-prepare",
            qt_activate_primary=parsed.mode == "probe-qt",
        )
    print(_canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
