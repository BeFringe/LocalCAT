"""Product composition after the platform producer has completed Boot TCB."""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


def ordinary_resources():
    from frozen_candidate import running_candidate
    from qt_resource_contracts import QtResourceBytes, SourceQtResources
    from qt_editor import APPLICATION_ICON_FILENAME
    candidate = running_candidate()
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.setLibraryPaths([str(candidate.bundle_root / "PySide6/plugins")])
    return SourceQtResources(logo=QtResourceBytes(filename=APPLICATION_ICON_FILENAME,
                             content=candidate.read_data(APPLICATION_ICON_FILENAME)), speaker_avatars=())


def run_ordinary_core_smoke(composition, work_root: Path) -> dict:
    """Small real consumer chain; never publishes a Gate D qualification."""
    from tm_benchmark import _benchmark_implementation_fingerprint_from_session, _load_benchmark_contract_from_session
    from tm_benchmark_latency import BenchmarkExecutionPath
    from tm_benchmark_oracle import run_oracle_recall_evidence
    from tm_benchmark_process import ProcessEvidenceError, run_process_migration_evidence
    from tm_benchmark_query_process import run_query_process_probe

    inputs = composition._frozen_input_source
    work_root.mkdir()
    report = {"candidate_id": inputs.candidate.candidate_id, "test_mode": True,
              "final_evidence": False, "paths": [], "timeouts": [], "standard_streams_none":
              [sys.stdin is None, sys.stdout is None, sys.stderr is None]}
    with inputs.operation():
        with inputs.input_session() as session:
            report["implementation_fingerprint"] = _benchmark_implementation_fingerprint_from_session(session)
            contract = _load_benchmark_contract_from_session(session)
            for path in (BenchmarkExecutionPath.FTS5_TRIGRAM, BenchmarkExecutionPath.GRAM_FALLBACK):
                migration_root = work_root / (path.value + "-migration")
                migration_root.mkdir()
                oracle_root = work_root / (path.value + "-oracle")
                oracle_root.mkdir()
                evidence = run_process_migration_evidence(
                    contract_path=None, execution_path=path, run_root=migration_root,
                    test_mode=True, test_record_count=40, timeout_seconds=120.0,
                    _input_session=session, _contract_input=session.input("benchmark_tm_contract.json"))
                query = run_query_process_probe(evidence, timeout_seconds=120.0, _input_session=session)
                oracle = run_oracle_recall_evidence(
                    contract=contract, execution_path=path, run_root=oracle_root,
                    test_mode=True, test_record_count=40, test_query_count=4, _input_session=session)
                if evidence.final_evidence or not evidence.test_mode or not oracle.test_mode or not oracle.recall_passed:
                    raise RuntimeError("ordinary smoke must retain test-only evidence")
                report["paths"].append({"path": path.value, "migration_pid": evidence.child_pid,
                    "query_pid": query.query_child_pid, "oracle_queries": oracle.query_count,
                    "process_evidence_digest": evidence.evidence_digest,
                    "query_probe_digest": query.probe.probe_digest,
                    "oracle_evidence_digest": oracle.evidence_digest})
                timeout_root = work_root / (path.value + "-timeout")
                timeout_root.mkdir()
                try:
                    run_process_migration_evidence(
                        contract_path=None, execution_path=path, run_root=timeout_root,
                        test_mode=True, test_record_count=40, timeout_seconds=0.000001,
                        _input_session=session, _contract_input=session.input("benchmark_tm_contract.json"))
                except ProcessEvidenceError as error:
                    if error.error_code != "PROCESS.CHILD_TIMEOUT":
                        raise
                    report["timeouts"].append({"path": path.value, "error_code": error.error_code})
                else:
                    raise RuntimeError("expired child startup deadline was not enforced")
            report["consumed_inputs"] = list(session.consumed_ids)
    return report


def finish_ordinary_smoke(app, composition, validation_worker, marker: Path) -> None:
    """Keep processing Qt events while the same Host's Core consumers run."""
    import json
    import time
    from threading import Thread
    from uuid import uuid4
    outcomes = {}

    def observe():
        try:
            validation_worker.join(timeout=120.0)
            if validation_worker.is_alive():
                raise TimeoutError("ordinary startup validation timeout")
            matcher = composition.host.matcher_snapshot()
            retrieval = composition.host.retrieval_operation_snapshot()
            if matcher.generation == 0 or matcher.matcher is None or retrieval.generation == 0:
                raise RuntimeError("ordinary Host validation did not publish its real Core results")
            outcomes["gate_c"] = composition.host._gate_c_diagnostics()
            if (not outcomes["gate_c"]["context_available"] or not outcomes["gate_c"]["fuzzy_core_available"]
                    or outcomes["gate_c"]["fuzzy_available"]):
                raise RuntimeError("ordinary Gate C correctness or safe projection is unavailable")
            outcomes.update(run_ordinary_core_smoke(composition, marker.parent / ("core-smoke-" + uuid4().hex)))
            outcomes["matcher_generation"] = matcher.generation
            outcomes["retrieval_generation"] = retrieval.generation
        except Exception as error:
            outcomes["error_type"] = type(error).__name__
            outcomes["error_code"] = getattr(error, "error_code", "ORDINARY.SMOKE_FAILED")
            outcomes["generations_after_failure"] = {
                "matcher": composition.host.matcher_snapshot().generation,
                "retrieval": composition.host.retrieval_snapshot().generation,
            }

    observer = Thread(target=observe, name="LocalCAT-ordinary-smoke", daemon=False)
    observer.start()
    deadline = time.monotonic() + 300.0
    while observer.is_alive() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    if observer.is_alive():
        composition._close_frozen_inputs()
        raise TimeoutError("ordinary consumer chain timeout")
    app.processEvents()
    bridge = validation_worker._localcat_capability_completion_bridge
    outcomes["queued_notification"] = {"count": bridge.delivered_count,
                                        "on_owner_thread": bridge.delivered_on_owner}
    if "error_type" not in outcomes and (bridge.delivered_count < 1 or not bridge.delivered_on_owner):
        outcomes["error_type"] = "RuntimeError"
        outcomes["error_code"] = "ORDINARY.QUEUED_NOTIFICATION_MISSING"
    # Existing smoke marker plus its diagnostic facts, never a release verdict.
    payload = json.loads(marker.read_bytes())
    payload["ordinary_core"] = outcomes
    marker.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    if "error_type" in outcomes:
        raise RuntimeError("ordinary consumer chain failed")


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
