from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ImportRequest,
    ResourceKind,
)
from editor_controller import EditorController
from platform_fs import compose_platform_file_backend
from platform_fs_contracts import LockPolicy, LockWait
from resource_artifact_save import (
    ResourceArtifactSaveService,
    _lock_name,
    _lock_payload,
)
from resource_package_contracts import (
    ResourceImportMode,
    ResourceRecoveryAction,
    ResourceRecoveryDisposition,
)
from resource_platform_io import platform_relative_path
from resource_portability import ResourcePortabilityService
from resource_receipt_ledger import ResourceReceiptLedger
from resource_repository import ResourceRepository
from termbase_store import TermbaseStore
from tm_engine import open_canonical_tm_store


_MIXED_TERMS = (
    b"\xef\xbb\xbfsource,target\n"
    b"localcat-term-v1,id-1,Case,Target,true,false\n"
)
_PRIOR_TERMS = (
    b"\xef\xbb\xbfprior,translation\n"
    b"localcat-term-v1,id-prior,Prior,Translation,true,false\n"
)
_C5_SEED_SOURCE = "C5 seed sentence."
_C5_SEED_TARGET = "C5 种子译文。"
_C5_TMX_SOURCE = "C5 imported sentence."
_C5_TMX_TARGET = "C5 导入译文。"
_C5_TMX = f'''<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4">
  <header creationtool="LocalCAT" creationtoolversion="1" segtype="sentence"
          adminlang="en-US" srclang="en-US" datatype="PlainText"/>
  <body>
    <tu tuid="c5-imported">
      <tuv xml:lang="en-US"><seg>{_C5_TMX_SOURCE}</seg></tuv>
      <tuv xml:lang="zh-CN"><seg>{_C5_TMX_TARGET}</seg></tuv>
    </tu>
  </body>
</tmx>
'''.encode("utf-8")


def _prepare_source(root: Path) -> tuple[ResourceRepository, str]:
    repository = ResourceRepository(root / "source-app")
    resources = repository.list_resources()
    if resources:
        source = resources[0]
    else:
        source = repository.create_resource("Terms", ResourceKind.TERMBASE)
        source.path.write_bytes(_MIXED_TERMS)
    return repository, source.id


def _produce(root: Path) -> dict[str, object]:
    source_repository, source_id = _prepare_source(root)
    source_service = ResourcePortabilityService(source_repository)
    direct = source_service.export_direct(source_id, root / "terms.csv")
    package = source_service.export_package(
        source_id,
        root / "terms.localcat-resource",
    )

    destination_repository = ResourceRepository(root / "destination-app")
    destination_service = ResourcePortabilityService(destination_repository)
    preview = destination_service.preview_resource_package_import(
        root / "terms.localcat-resource",
        ResourceImportMode.CREATE_NEW,
        new_resource_name="Imported Terms",
    )
    imported = destination_service.apply_resource_package_import(preview)
    return {
        "direct_digest": direct.receipt.payload_digest,
        "package_digest": package.receipt.package_artifact_digest,
        "payload_digest": package.receipt.payload_digest,
        "imported_resource_id": imported.destination_resource_id,
    }


def _reopen(root: Path) -> dict[str, object]:
    source_repository = ResourceRepository(root / "source-app")
    destination_repository = ResourceRepository(root / "destination-app")
    source_resource = source_repository.list_resources()[0]
    destination_resource = destination_repository.list_resources()[0]
    source_service = ResourcePortabilityService(source_repository)
    package = source_service.validate_resource_package(
        root / "terms.localcat-resource"
    )
    direct = TermbaseStore(source_repository.platform_backend).validate_portable_snapshot(
        root / "terms.csv"
    )
    imported = TermbaseStore(
        destination_repository.platform_backend
    ).validate_portable_snapshot(destination_resource.path)
    source_ledger = ResourceReceiptLedger(
        source_repository.config_dir,
        source_repository.platform_backend,
    )
    destination_ledger = ResourceReceiptLedger(
        destination_repository.config_dir,
        destination_repository.platform_backend,
    )
    return {
        "source_resource_id": source_resource.id,
        "imported_resource_id": destination_resource.id,
        "direct_digest": direct.payload_digest,
        "package_digest": package.artifact_digest,
        "payload_digest": package.payload_digest,
        "imported_digest": imported.payload_digest,
        "source_receipts": len(source_ledger.list_receipts()),
        "source_pending": len(source_ledger.list_pending()),
        "destination_receipts": len(destination_ledger.list_receipts()),
        "destination_pending": len(destination_ledger.list_pending()),
    }


def _query_targets(controller: EditorController, source: str) -> tuple[str, ...]:
    controller.set_project(
        EditorProject(
            name="C5 reboot TM query",
            segments=(EditorSegment(id="query", source=source),),
        )
    )
    return tuple(match.target for match in controller.suggestions().tm_matches)


def _receipt_facts(receipt: object) -> dict[str, object]:
    return {
        "operation_id": receipt.operation_id,
        "operation_kind": receipt.operation_kind.value,
        "resource_kind": receipt.resource_kind.value,
        "payload_profile": receipt.payload_profile.value,
        "source_resource_id": receipt.source_resource_id,
        "destination_resource_id": receipt.destination_resource_id,
        "package_artifact_digest": receipt.package_artifact_digest,
        "payload_digest": receipt.payload_digest,
        "record_count": receipt.record_count,
        "owner_generation": receipt.owner_generation,
        "owner_revision": receipt.owner_revision,
        "durable_state": receipt.durable_state.value,
    }


def _c5_observe(root: Path) -> dict[str, object]:
    source_repository = ResourceRepository(root / "source-app")
    destination_repository = ResourceRepository(root / "destination-app")
    source_resources = source_repository.list_resources()
    destination_resources = destination_repository.list_resources()
    if len(source_resources) != 1 or len(destination_resources) != 1:
        raise AssertionError("C5 TM journey requires one source and destination resource")
    source_resource = source_resources[0]
    resource = destination_resources[0]
    if (
        source_resource.kind is not ResourceKind.TRANSLATION_MEMORY
        or resource.kind is not ResourceKind.TRANSLATION_MEMORY
    ):
        raise AssertionError("C5 TM journey did not retain translation-memory kinds")

    package_path = root / "tm.localcat-resource"
    controller = EditorController(destination_repository)
    package = controller.validate_resource_package(package_path)
    store = open_canonical_tm_store(
        resource.path,
        expected_resource_id=resource.id,
    )
    if store is None:
        raise AssertionError("C5 imported TM did not reopen its canonical owner")
    revision = store.canonical_revision()
    seed_targets = _query_targets(controller, _C5_SEED_SOURCE)
    tmx_targets = _query_targets(controller, _C5_TMX_SOURCE)
    if _C5_SEED_TARGET not in seed_targets or _C5_TMX_TARGET not in tmx_targets:
        raise AssertionError("C5 canonical TM business queries did not reopen")
    if revision.resource_id != resource.id or revision.record_count != 2:
        raise AssertionError("C5 canonical TM revision changed")

    source_ledger = ResourceReceiptLedger(
        source_repository.config_dir,
        source_repository.platform_backend,
    )
    destination_ledger = ResourceReceiptLedger(
        destination_repository.config_dir,
        destination_repository.platform_backend,
    )
    source_receipts = source_ledger.list_receipts()
    destination_receipts = destination_ledger.list_receipts()
    if len(source_receipts) != 1 or len(destination_receipts) != 1:
        raise AssertionError("C5 TM journey requires exact package receipts")
    if source_ledger.list_pending() or destination_ledger.list_pending():
        raise AssertionError("C5 TM journey retained a pending resource receipt")

    return {
        "resource": {
            "id": resource.id,
            "kind": resource.kind.value,
            "lookup": resource.lookup,
        },
        "package": {
            "artifact_digest": package.artifact_digest,
            "payload_digest": package.payload_digest,
            "record_count": package.record_count,
        },
        "receipts": {
            "source": _receipt_facts(source_receipts[0]),
            "destination": _receipt_facts(destination_receipts[0]),
            "source_pending": 0,
            "destination_pending": 0,
        },
        "canonical": {
            "generation": revision.generation,
            "head_revision": revision.head_revision,
            "record_count": revision.record_count,
        },
        "queries": {
            "seed": seed_targets,
            "tmx": tmx_targets,
        },
    }


def _c5_prepare(root: Path) -> dict[str, object]:
    source_repository = ResourceRepository(root / "source-app")
    source_controller = EditorController(source_repository)
    source = source_controller.create_resource(
        "C5 active TM",
        ResourceKind.TRANSLATION_MEMORY,
    )
    source.path.write_bytes(
        json.dumps(
            {"source": _C5_SEED_SOURCE, "target": _C5_SEED_TARGET},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    preflight = source_controller.prepare_tm_activation(source.id)
    operation = source_controller.activate_tm_resource(preflight)
    completed = source_controller.wait_tm_activation(
        operation.operation_id,
        timeout=120.0,
    )
    if not completed.completed or not completed.succeeded:
        raise AssertionError("C5 source TM activation did not complete")
    if _C5_SEED_TARGET not in _query_targets(source_controller, _C5_SEED_SOURCE):
        raise AssertionError("C5 source TM activation is not queryable")

    package_path = root / "tm.localcat-resource"
    source_controller.export_resource_package(source.id, package_path)

    destination_repository = ResourceRepository(root / "destination-app")
    destination_controller = EditorController(destination_repository)
    preview = destination_controller.preview_resource_package_import(
        package_path,
        ResourceImportMode.CREATE_NEW,
        new_resource_name="C5 imported TM",
    )
    imported = destination_controller.apply_resource_package_import(preview)
    resource = destination_repository.get(imported.destination_resource_id)
    if _C5_SEED_TARGET not in _query_targets(destination_controller, _C5_SEED_SOURCE):
        raise AssertionError("C5 imported package is not queryable")

    tmx_path = (root / "import.tmx").resolve()
    tmx_path.write_bytes(_C5_TMX)
    report = destination_controller.import_resource(
        ImportRequest(
            resource_id=resource.id,
            input_path=tmx_path,
            source_locale="en-US",
            target_locale="zh-CN",
        )
    )
    if (
        report.imported != 1
        or report.skipped != 0
        or report.overwritten != 0
        or report.errors
    ):
        raise AssertionError("C5 rooted TMX import did not complete exactly once")
    if _C5_TMX_TARGET not in _query_targets(destination_controller, _C5_TMX_SOURCE):
        raise AssertionError("C5 rooted TMX import is not queryable")

    observed = _c5_observe(root)
    observed["tmx_import"] = {
        "imported": report.imported,
        "skipped": report.skipped,
        "overwritten": report.overwritten,
        "errors": report.errors,
    }
    return observed


def _c5_reopen(root: Path) -> dict[str, object]:
    return _c5_observe(root)


def _hold_lock(root: Path, destination_name: str) -> None:
    backend = compose_platform_file_backend(root)
    bound_root = backend.bind_root(root)
    parent = backend.bind_parent(
        bound_root,
        platform_relative_path(destination_name),
    )
    lease = backend.acquire(
        parent,
        _lock_name(destination_name),
        _lock_payload(destination_name),
        LockPolicy(LockWait.BLOCK),
    )
    try:
        print("READY", flush=True)
        sys.stdin.buffer.read(1)
    finally:
        lease.close()
        parent.close()
        bound_root.close()


def _publish(candidate: Path, destination: Path) -> dict[str, object]:
    publication, _validation = ResourceArtifactSaveService().publish(
        candidate,
        destination,
        lambda path: path.read_bytes(),
        owner_commit=lambda _publication, _validation: None,
    )
    return {
        "before": publication.destination_before_digest,
        "after": publication.destination_after_digest,
    }


def _crash_direct_platform(
    root: Path,
    phase: str,
    *,
    replace_existing: bool = False,
) -> None:
    from platform_fs_windows import WindowsPlatformAdapter

    repository, source_id = _prepare_source(root)
    destination = root / "crash-terms.csv"
    if replace_existing:
        prior = repository.create_resource("Prior terms", ResourceKind.TERMBASE)
        prior.path.write_bytes(_PRIOR_TERMS)
        ResourcePortabilityService(repository).export_direct(
            prior.id,
            destination,
        )

    observed = 0

    def terminate(current: str) -> None:
        nonlocal observed
        if current == phase:
            observed += 1
        if observed == (2 if replace_existing else 1) and current == phase:
            import os

            os._exit(91)

    artifact_save = ResourceArtifactSaveService(
        WindowsPlatformAdapter(_fault_injector=terminate)
    )
    ResourcePortabilityService(
        repository,
        artifact_save=artifact_save,
    ).export_direct(source_id, destination)


def _crash_direct_ledger(root: Path, occurrence: int) -> None:
    from platform_fs_windows import WindowsPlatformAdapter

    repository, source_id = _prepare_source(root)
    observed = 0

    def terminate(current: str) -> None:
        nonlocal observed
        if current != "publish_after_destination_reopen":
            return
        observed += 1
        if observed == occurrence:
            import os

            os._exit(91)

    ledger = ResourceReceiptLedger(
        repository.config_dir,
        WindowsPlatformAdapter(_fault_injector=terminate),
    )
    ResourcePortabilityService(
        repository,
        ledger=ledger,
    ).export_direct(source_id, root / "crash-terms.csv")


def _inspect_crash(root: Path) -> dict[str, object]:
    repository = ResourceRepository(root / "source-app")
    ledger = ResourceReceiptLedger(
        repository.config_dir,
        repository.platform_backend,
    )
    service = ResourcePortabilityService(repository, ledger=ledger)
    destination = root / "crash-terms.csv"
    pending_before = ledger.list_pending()
    previews = service.inspect_resource_portability_recovery()
    if len(pending_before) != 1 or len(previews) != 1:
        raise AssertionError("crash inspection requires one exact pending operation")
    pending = pending_before[0]
    preview = previews[0]
    target_digest = None
    target_state = "old"
    if destination.exists():
        target = TermbaseStore(
            repository.platform_backend
        ).validate_portable_snapshot(destination)
        target_digest = target.payload_digest
        if target_digest == pending.receipt.destination_after_digest:
            target_state = "new"
        elif target_digest == pending.receipt.destination_before_digest:
            target_state = "old"
        else:
            raise AssertionError("destination is neither prior nor complete owner output")
    elif pending.receipt.destination_before_digest is not None:
        raise AssertionError("existing prior destination disappeared")
    recovery = "retained"
    if preview.disposition is ResourceRecoveryDisposition.COMPLETE_AVAILABLE:
        service.recover_resource_portability(
            preview,
            ResourceRecoveryAction.COMPLETE,
        )
        recovery = "completed"
    return {
        "disposition": preview.disposition.value,
        "pending_phase": pending.phase.value,
        "target_state": target_state,
        "target_digest": target_digest,
        "before_digest": pending.receipt.destination_before_digest,
        "expected_digest": pending.receipt.destination_after_digest,
        "recovery": recovery,
        "pending_after": len(ledger.list_pending()),
        "receipts_after": len(ledger.list_receipts()),
        "lkg_after": len(tuple(root.glob(".resource-lkg-*.tmp"))),
        "candidate_after": len(tuple(root.glob(".resource-candidate-*.tmp"))),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        raise SystemExit("usage: worker MODE ROOT [ARGS]")
    mode = argv[1]
    root = Path(argv[2])
    if not root.is_absolute():
        raise SystemExit("worker root must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    if mode == "produce":
        result = _produce(root)
    elif mode == "reopen":
        result = _reopen(root)
    elif mode == "hold-lock":
        if len(argv) != 4:
            raise SystemExit("hold-lock requires destination name")
        _hold_lock(root, argv[3])
        return 0
    elif mode == "publish":
        if len(argv) != 5:
            raise SystemExit("publish requires candidate and destination names")
        result = _publish(root / argv[3], root / argv[4])
    elif mode == "crash-direct-platform":
        if len(argv) != 4:
            raise SystemExit("crash-direct-platform requires phase")
        _crash_direct_platform(root, argv[3])
        raise AssertionError("platform crash phase was not reached")
    elif mode == "crash-direct-platform-replace":
        if len(argv) != 4:
            raise SystemExit("crash-direct-platform-replace requires phase")
        _crash_direct_platform(root, argv[3], replace_existing=True)
        raise AssertionError("platform crash phase was not reached")
    elif mode == "crash-direct-ledger":
        if len(argv) != 4:
            raise SystemExit("crash-direct-ledger requires occurrence")
        _crash_direct_ledger(root, int(argv[3]))
        raise AssertionError("ledger crash phase was not reached")
    elif mode == "inspect-crash":
        result = _inspect_crash(root)
    elif mode == "c5-prepare":
        result = _c5_prepare(root)
    elif mode == "c5-reopen":
        result = _c5_reopen(root)
    else:
        raise SystemExit(f"unknown worker mode: {mode}")
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
