from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from editor_contracts import ResourceKind
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


_MIXED_TERMS = (
    b"\xef\xbb\xbfsource,target\n"
    b"localcat-term-v1,id-1,Case,Target,true,false\n"
)
_PRIOR_TERMS = (
    b"\xef\xbb\xbfprior,translation\n"
    b"localcat-term-v1,id-prior,Prior,Translation,true,false\n"
)


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
    else:
        raise SystemExit(f"unknown worker mode: {mode}")
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
