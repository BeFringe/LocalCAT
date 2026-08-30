"""Independent-process worker for Windows portable PREPARED cancellation."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePath

import platform_fs_windows
import tm_activation_journal
from platform_fs_contracts import (
    CandidateContentFacts,
    PlatformFileError,
    PrivateProofContext,
    PrivateProofObjectRole,
    PublishMode,
)
from tm_contracts import CanonicalResourceIdentity
from tm_migration import TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator
from tests.test_tm_initial_activation_windows import SOURCE_BYTES, _portable_sealed_stage


def _identity(root: Path, *, create_source: bool) -> CanonicalResourceIdentity:
    source = (root / "tm.primary.jsonl").resolve()
    if create_source:
        source.write_bytes(SOURCE_BYTES)
    elif source.read_bytes() != SOURCE_BYTES:
        raise AssertionError("configured source bytes changed across processes")
    return CanonicalResourceIdentity.from_configured_jsonl("tm.primary", source)


def _digest(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "name": path.name,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _owner(
    root: Path,
    identity: CanonicalResourceIdentity,
    *,
    recovery_fault: str | None = None,
):
    def fault(phase: str) -> None:
        if phase == recovery_fault:
            raise OSError("test recovery publication fault")

    backend = platform_fs_windows.WindowsPlatformAdapter(
        _fault_injector=None if recovery_fault is None else fault
    )
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
        coordinator=coordinator,
        platform_backend=backend,
    )
    return backend, coordinator, service


def _prepare_terminal_candidate_residue(
    *,
    root: Path,
    backend: platform_fs_windows.WindowsPlatformAdapter,
    reservation: object,
    prepared: object,
    state: str,
) -> dict[str, object] | None:
    if state == "none":
        return None
    private_root = root / prepared.private_directory_name
    pending_path = private_root / prepared.journal_name
    pending = tm_activation_journal._parse_portable_activation_journal_bytes(
        pending_path.read_bytes()
    )
    private_parent = backend.bind_parent(
        reservation._root,
        PurePath(prepared.private_directory_name, "candidate-placeholder"),
    )
    private_evidence = backend.prove_private(private_parent)
    key_file = backend.open_regular(
        reservation._root,
        PurePath(prepared.private_directory_name, "device.key"),
    )
    secret = backend.bind_device_secret(key_file)
    try:
        cancelled_unsigned = replace(pending.unsigned, closure="CANCELLED")
        context = PrivateProofContext(
            PrivateProofObjectRole.PRIVATE_DIRECTORY,
            tm_activation_journal._portable_activation_owner_context_sha256(
                cancelled_unsigned
            ),
        )
        proof = backend.mint(private_evidence, secret, context)
        cancelled = tm_activation_journal._create_portable_activation_journal_record(
            cancelled_unsigned,
            proof,
        )
        candidate_name, candidate_bytes, _expected = (
            tm_activation_journal._portable_terminal_candidate_content(cancelled)
        )
        payload = (
            b"wrong-terminal-candidate"
            if state in {"wrong", "wrong-target"}
            else candidate_bytes
        )
        candidate = private_parent.create_candidate(candidate_name, private=True)
        candidate.write_all(payload)
        facts = CandidateContentFacts(len(payload), hashlib.sha256(payload).digest())
        candidate.flush_content()
        candidate.close()
    finally:
        secret.close()
        key_file.close()
        private_evidence.close()
        private_parent.close()

    target_path = (
        root
        / tm_activation_journal._PORTABLE_ACTIVATION_QUARANTINE_ROOT
        / tm_activation_journal._portable_activation_quarantine_name(
            cancelled.unsigned
        )
        / candidate_name
    )
    if state in {"target", "coexist", "wrong-target"}:
        source_parent = backend.bind_retirement_source_directory(
            reservation._root,
            PurePath(prepared.private_directory_name, candidate_name),
        )
        source = backend.open_existing_retirement_source(source_parent, facts)
        quarantine_root = backend.bind_or_create_child_directory(
            reservation._root,
            tm_activation_journal._PORTABLE_ACTIVATION_QUARANTINE_ROOT,
        )
        quarantine_target = backend.bind_or_create_child_directory(
            quarantine_root,
            tm_activation_journal._portable_activation_quarantine_name(
                cancelled.unsigned
            ),
        )
        retained = backend.retire_existing_exclusive(
            source_parent,
            source,
            quarantine_target,
            candidate_name,
        )
        retained.reprove()
        retained.close()
        source_parent.close()
        quarantine_target.close()
        quarantine_root.close()
        if state == "coexist":
            private_parent = backend.bind_parent(
                reservation._root,
                PurePath(prepared.private_directory_name, "candidate-placeholder"),
            )
            candidate = private_parent.create_candidate(candidate_name, private=True)
            candidate.write_all(candidate_bytes)
            candidate.flush_content()
            candidate.close()
            private_parent.close()
    elif state == "unknown":
        private_parent = backend.bind_parent(
            reservation._root,
            PurePath(prepared.private_directory_name, "candidate-placeholder"),
        )
        foreign = private_parent.create_candidate("foreign.candidate", private=True)
        foreign.write_all(b"foreign")
        foreign.flush_content()
        foreign.close()
        private_parent.close()
    elif state == "quarantine-unknown":
        quarantine_root = backend.bind_or_create_child_directory(
            reservation._root,
            tm_activation_journal._PORTABLE_ACTIVATION_QUARANTINE_ROOT,
        )
        quarantine_target = backend.bind_or_create_child_directory(
            quarantine_root,
            tm_activation_journal._portable_activation_quarantine_name(
                cancelled.unsigned
            ),
        )
        quarantine_target.close()
        quarantine_root.close()
        target_parent = backend.bind_parent(
            reservation._root,
            PurePath(
                tm_activation_journal._PORTABLE_ACTIVATION_QUARANTINE_ROOT,
                tm_activation_journal._portable_activation_quarantine_name(
                    cancelled.unsigned
                ),
                "foreign-placeholder",
            ),
        )
        foreign = target_parent.create_candidate("foreign.candidate", private=True)
        foreign.write_all(b"foreign")
        foreign.flush_content()
        pending_foreign = target_parent.begin_publish(
            foreign,
            "foreign.candidate",
            mode=PublishMode.CREATE_IF_ABSENT,
            lease=None,
        )
        pending_foreign.terminal_reproof()
        pending_foreign.close()
        target_parent.close()

    visible = target_path if target_path.exists() else private_root / candidate_name
    return _digest(visible)


def _creator(root: Path, *, candidate_state: str) -> dict[str, object]:
    identity = _identity(root, create_source=True)
    backend, coordinator, service = _owner(root, identity)
    reservation, sealed, _registry = _portable_sealed_stage(
        service,
        coordinator,
        identity,
    )
    try:
        preparation = coordinator.activate(sealed)
        prepared = coordinator.publish_portable_prepared_activation(
            preparation,
            **reservation.portable_journal_inputs(),
        )
        private_root = root / prepared.private_directory_name
        pending_path = private_root / prepared.journal_name
        pending = tm_activation_journal._parse_portable_activation_journal_bytes(
            pending_path.read_bytes()
        )
        candidate = _prepare_terminal_candidate_residue(
            root=root,
            backend=backend,
            reservation=reservation,
            prepared=prepared,
            state=candidate_state,
        )
        unsigned = pending.unsigned
        reservation.reprove()
        return {
            "mode": "creator",
            "pid": os.getpid(),
            "preparation_id": prepared.preparation_id,
            "private_directory_name": prepared.private_directory_name,
            "pending": _digest(pending_path),
            "stage": _digest(root / unsigned.candidate_stage_db_name),
            "manifest": _digest(root / unsigned.candidate_manifest_temp_name),
            "terminal_candidate": candidate,
            "canonical_absent": not identity.canonical_sidecar_path.exists(),
        }
    finally:
        # The process boundary destroys registry-only preparation authority;
        # releasing only W1 here models an orderly creator exit without cleanup.
        reservation.release()


def _fresh_cancel(
    root: Path,
    *,
    recovery_fault: str | None,
) -> dict[str, object]:
    identity = _identity(root, create_source=False)
    _backend, coordinator, service = _owner(
        root,
        identity,
        recovery_fault=recovery_fault,
    )
    private_roots = tuple(root.glob(".localcat-activation-private-v1.*"))
    if len(private_roots) != 1:
        raise AssertionError("fresh recovery requires one exact private directory")
    private_root = private_roots[0]
    main = private_root / "activation-journal-v3.json"
    terminal = private_root / "activation-terminal-v3.json"
    authority_path = main if main.exists() else terminal if terminal.exists() else None
    record = (
        None
        if authority_path is None
        else tm_activation_journal._parse_portable_activation_journal_bytes(
            authority_path.read_bytes()
        )
    )
    reservation = service._acquire_initial_reservation()
    try:
        reservation.reprove()
        recovery_inputs = reservation.portable_recovery_inputs()
        if record is None:
            snapshot = (
                tm_activation_journal._WindowsPortableFreshRecoveryOwner.inspect(
                    identity=identity,
                    canonical_store_id="store.primary",
                    backend=recovery_inputs["platform"],
                    persistent_private=recovery_inputs["persistent_private"],
                    descendant_inspection=recovery_inputs["descendant_inspection"],
                    caller_borrow=recovery_inputs["caller_borrow"],
                )
            )
            if snapshot.state != "NO_FACTS" or snapshot.namespace_state != "KEY_ONLY":
                raise AssertionError("stable cancellation must reopen as key-only")
            report = None
        else:
            report = coordinator.recover_portable_prepared_cancellation(
                **recovery_inputs,
            )
        reservation.reprove()
        terminal_matches = tuple(
            (
                root
                / tm_activation_journal._PORTABLE_ACTIVATION_QUARANTINE_ROOT
            ).glob("portable-*/activation-terminal-v3.json")
        )
        if len(terminal_matches) != 1:
            raise AssertionError("fresh recovery requires one archived terminal")
        terminal = terminal_matches[0]
        terminal_record = (
            tm_activation_journal._parse_portable_activation_journal_bytes(
                terminal.read_bytes()
            )
        )
        quarantine_name = (
            tm_activation_journal._portable_activation_quarantine_name(
                terminal_record.unsigned
            )
        )
        quarantine = terminal.parent
        files = {
            path.name: _digest(path)
            for path in sorted(quarantine.iterdir(), key=lambda item: item.name)
        }
        return {
            "mode": "recover",
            "pid": os.getpid(),
            "input_closure": (
                "NO_FACTS" if record is None else record.unsigned.closure
            ),
            "phase": None if report is None else report.phase,
            "action": "NONE" if report is None else report.action,
            "generation": None if report is None else report.generation,
            "state": coordinator.state,
            "terminal": _digest(terminal),
            "private_names": sorted(path.name for path in private_root.iterdir()),
            "quarantine": files,
            "canonical_absent": not identity.canonical_sidecar_path.exists(),
        }
    finally:
        reservation.release()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("creator", "recover"))
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--candidate-state",
        choices=(
            "none",
            "source",
            "target",
            "wrong",
            "wrong-target",
            "coexist",
            "unknown",
            "quarantine-unknown",
        ),
        default="none",
    )
    parser.add_argument(
        "--recovery-fault",
        choices=("publish_before_rename",),
        default=None,
    )
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        result = (
            _creator(root, candidate_state=arguments.candidate_state)
            if arguments.mode == "creator"
            else _fresh_cancel(root, recovery_fault=arguments.recovery_fault)
        )
    except (tm_activation_journal.ActivationPreparationError, PlatformFileError) as error:
        print(
            json.dumps(
                {
                    "mode": arguments.mode,
                    "pid": os.getpid(),
                    "error_code": error.code,
                    "retryable": error.retryable,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
