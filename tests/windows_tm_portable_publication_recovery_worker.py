"""Independent-process worker for Windows portable activation recovery."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import traceback
from unittest import mock

import tm_activation_journal
import tm_migration
import tm_sqlite_store
import tm_stage_sealer
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationFailure,
    MigrationReport,
    SnapshotManifest,
    contract_from_json,
    contract_to_json,
)
from tm_sqlite_store import (
    ActivationPreparationError,
    ResourceStoreCoordinator,
    SQLiteTMStore,
)


_SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"same","target":"winner"}\n'
    b'{"source":"other","target":"value"}\n'
)
_PHASES = (
    "DB_REPLACED",
    "MANIFEST_PUBLISHED",
    "GENERATION_PUBLISHED",
)


def _identity(root: Path, *, create_source: bool) -> CanonicalResourceIdentity:
    source = (root / "tm.primary.jsonl").resolve()
    if create_source:
        source.write_bytes(_SOURCE_BYTES)
    elif not source.is_file():
        raise AssertionError("configured source is missing across processes")
    return CanonicalResourceIdentity.from_configured_jsonl("tm.primary", source)


def _owner(
    identity: CanonicalResourceIdentity,
) -> tuple[ResourceStoreCoordinator, tm_migration.TMMigrationService]:
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )
    service = tm_migration.TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
        coordinator=coordinator,
    )
    return coordinator, service


def _outcome_facts(outcome: object) -> dict[str, object]:
    if type(outcome) is MigrationReport:
        report = outcome
        assert isinstance(report, MigrationReport)
        return {
            "kind": "MigrationReport",
            "contract_json": contract_to_json(report),
            "generation": report.activated_generation,
            "snapshot_id": report.snapshot_receipt.snapshot_id,
            "record_count": report.snapshot_receipt.record_count,
            "source_digest": report.source_digest,
        }
    if type(outcome) is MigrationFailure:
        failure = outcome
        assert isinstance(failure, MigrationFailure)
        return {
            "kind": "MigrationFailure",
            "stage": failure.stage,
            "error_code": failure.error_code,
            "retryable": failure.retryable,
            "active_generation": failure.active_generation,
            "canonical_authority_published": (
                failure.canonical_authority_published
            ),
            "canonical_authority_ambiguous": (
                failure.canonical_authority_ambiguous
            ),
        }
    return {"kind": type(outcome).__name__}


def _file_facts(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    payload = path.read_bytes()
    return {
        "name": path.name,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _journal_facts(
    root: Path,
    identity: CanonicalResourceIdentity,
) -> dict[str, object]:
    private_root = (
        root
        / tm_activation_journal._portable_activation_private_directory_name(
            identity
        )
    )
    main = private_root / "activation-journal-v3.json"
    terminal = private_root / "activation-terminal-v3.json"
    prepared = None
    closure = None
    if main.is_file():
        prepared = tm_activation_journal._parse_portable_activation_journal_bytes(
            main.read_bytes()
        )
        closure = prepared.unsigned.closure
    elif terminal.is_file():
        prepared = tm_activation_journal._parse_portable_activation_journal_bytes(
            terminal.read_bytes()
        )
        closure = prepared.unsigned.closure

    records: list[object] = []
    phase_files: dict[str, dict[str, object]] = {}
    for phase in _PHASES:
        path = private_root / tm_activation_journal._portable_publication_phase_name(
            phase
        )
        if path.is_file():
            facts = _file_facts(path)
            assert facts is not None
            phase_files[phase] = facts
            records.append(
                tm_activation_journal._parse_portable_publication_phase_bytes(
                    path.read_bytes()
                )
            )

    chain = [record.unsigned.phase for record in records]
    predecessor_closed = prepared is not None
    predecessor = None if prepared is None else prepared.record_digest
    for record in records:
        predecessor_closed = (
            predecessor_closed
            and record.unsigned.predecessor_digest == predecessor
        )
        predecessor = record.record_digest

    retirement: dict[str, object] | None = None
    if prepared is not None:
        unsigned = prepared.unsigned
        names = (
            unsigned.candidate_stage_db_name,
            unsigned.candidate_manifest_temp_name,
        )
        quarantine_name = (
            tm_activation_journal._portable_initial_stage_quarantine_name(
                identity,
                unsigned,
            )
        )
        quarantine = (
            root
            / tm_activation_journal._PORTABLE_ACTIVATION_QUARANTINE_ROOT
            / quarantine_name
        )
        matches = {
            name: (
                [_file_facts(Path("\\\\?\\" + str(quarantine / name)))]
                if Path("\\\\?\\" + str(quarantine / name)).is_file()
                else []
            )
            for name in names
        }
        retirement = {
            "source_absent": all(not (root / name).exists() for name in names),
            "quarantine_matches": matches,
        }

    return {
        "private_exists": private_root.is_dir(),
        "private_names": (
            sorted(path.name for path in private_root.iterdir())
            if private_root.is_dir()
            else []
        ),
        "closure": closure,
        "phases": chain,
        "phase_files": phase_files,
        "predecessor_closed": predecessor_closed,
        "prepared": _file_facts(main),
        "terminal": _file_facts(terminal),
        "retirement": retirement,
        "candidate_residue": sorted(
            str(path.relative_to(root))
            for path in root.rglob("*.candidate")
            if path.is_file()
        ),
        "initial_stage_residue": sorted(
            path.name
            for path in root.glob(".localcat-migration.initial-*")
        ),
        "stage_assets": (
            {
                name: _file_facts(root / name)
                for name in (
                    prepared.unsigned.candidate_stage_db_name,
                    prepared.unsigned.candidate_manifest_temp_name,
                )
            }
            if prepared is not None
            else {}
        ),
        "private_namespace": (
            {
                path.name: _file_facts(path)
                for path in private_root.iterdir()
                if path.is_file()
            }
            if private_root.is_dir()
            else {}
        ),
    }


def _database_sql_facts(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
        )
        try:
            return {
                "meta": dict(connection.execute("SELECT key, value FROM tm_meta")),
                "receipt_statuses": [
                    row[0]
                    for row in connection.execute(
                        "SELECT status FROM tm_snapshot_receipt ORDER BY snapshot_id"
                    )
                ],
                "binding_count": connection.execute(
                    "SELECT COUNT(*) FROM tm_snapshot_binding"
                ).fetchone()[0],
            }
        finally:
            connection.close()
    except sqlite3.Error as error:
        return {"sqlite_error": type(error).__name__}


def _disk_facts(identity: CanonicalResourceIdentity) -> dict[str, object]:
    return {
        "source": _file_facts(identity.configured_jsonl_path),
        "database": _file_facts(identity.canonical_sidecar_path),
        "manifest": _file_facts(identity.snapshot_manifest_path),
        "database_sql": _database_sql_facts(identity.canonical_sidecar_path),
    }


def _runtime_facts(
    identity: CanonicalResourceIdentity,
    coordinator: ResourceStoreCoordinator,
) -> dict[str, object]:
    facts: dict[str, object] = {
        "state": coordinator.state,
        "generation": coordinator.current_generation,
        "view_visible": coordinator._view is not None,
        "active_store_path": (
            None
            if coordinator.active_store_path is None
            else str(coordinator.active_store_path)
        ),
        "database": _file_facts(identity.canonical_sidecar_path),
        "manifest": _file_facts(identity.snapshot_manifest_path),
        "database_sql": _database_sql_facts(identity.canonical_sidecar_path),
    }
    marker = tm_sqlite_store._activation_lineage_marker_path(identity)
    marker_facts = _file_facts(marker)
    if marker_facts is not None:
        marker_facts["exact"] = (
            marker.read_bytes()
            == tm_sqlite_store._activation_lineage_marker_payload(identity)
        )
    facts["lineage_marker"] = marker_facts

    if coordinator.current_generation != 0:
        return facts

    manifest = contract_from_json(
        identity.snapshot_manifest_path.read_text(encoding="utf-8")
    )
    if type(manifest) is not SnapshotManifest:
        raise AssertionError("canonical manifest is not SnapshotManifest")
    connection = sqlite3.connect(str(identity.canonical_sidecar_path))
    try:
        meta = dict(connection.execute("SELECT key, value FROM tm_meta"))
        receipt_rows = connection.execute(
            "SELECT snapshot_id, resource_id, canonical_store_id, "
            "record_count, status FROM tm_snapshot_receipt"
        ).fetchall()
        binding_rows = connection.execute(
            "SELECT configured_jsonl_path, manifest_path, snapshot_kind, "
            "snapshot_id FROM tm_snapshot_binding"
        ).fetchall()
        fts_count = connection.execute("SELECT COUNT(*) FROM tm_fts").fetchone()[0]
        fts_matches = connection.execute(
            "SELECT COUNT(*) FROM tm_fts WHERE tm_fts MATCH 'same'"
        ).fetchone()[0]
    finally:
        connection.close()

    store = SQLiteTMStore.from_coordinator(coordinator)
    health = store.health()
    facts["canonical"] = {
        "manifest_receipt_json": contract_to_json(manifest.receipt),
        "meta": meta,
        "receipt_rows": receipt_rows,
        "binding_rows": binding_rows,
        "fts5_runtime": tm_sqlite_store.detect_sqlite_runtime().fts5_available,
        "fts_count": fts_count,
        "fts_matches": fts_matches,
        "health": {
            "healthy": health.healthy,
            "exact_available": health.exact_available,
            "generation": health.generation,
            "index_kind": health.index_kind,
            "record_count": health.record_count,
        },
        "same_targets": [
            record.target_raw for record in store.exact_records("same")
        ],
    }
    return facts


def _mutate(root: Path, mutation: str) -> dict[str, object]:
    identity = _identity(root, create_source=False)
    private_root = (
        root
        / tm_activation_journal._portable_activation_private_directory_name(
            identity
        )
    )
    pending_path = private_root / "activation-journal-v3.json"
    pending = tm_activation_journal._parse_portable_activation_journal_bytes(
        pending_path.read_bytes()
    )
    before = _journal_facts(root, identity)
    phases = before["phases"]
    if mutation == "missing-manifest":
        if phases != ["DB_REPLACED", "MANIFEST_PUBLISHED"]:
            raise AssertionError("missing-manifest requires MANIFEST phase")
        identity.snapshot_manifest_path.unlink()
    elif mutation == "sealed-database":
        if phases not in (
            ["DB_REPLACED", "MANIFEST_PUBLISHED"],
            ["DB_REPLACED", "MANIFEST_PUBLISHED", "GENERATION_PUBLISHED"],
        ):
            raise AssertionError("sealed-database requires MANIFEST or GEN phase")
        stage = root / pending.unsigned.candidate_stage_db_name
        payload = stage.read_bytes()
        expected = pending.unsigned.sealed_content_attestation.database
        if (
            len(payload) != expected.size
            or hashlib.sha256(payload).hexdigest() != expected.sha256
        ):
            raise AssertionError("honest creator did not retain exact sealed DB")
        identity.canonical_sidecar_path.write_bytes(payload)
    elif mutation == "stage-database-missing":
        if phases != ["DB_REPLACED"]:
            raise AssertionError("stage DB mutation requires DB phase")
        (root / pending.unsigned.candidate_stage_db_name).unlink()
    elif mutation == "stage-database-changed":
        if phases != ["DB_REPLACED"]:
            raise AssertionError("stage DB mutation requires DB phase")
        (root / pending.unsigned.candidate_stage_db_name).write_bytes(
            b"changed-sealed-stage"
        )
    elif mutation == "source-changed":
        if phases != ["DB_REPLACED"]:
            raise AssertionError("source mutation requires DB phase")
        identity.configured_jsonl_path.write_bytes(
            _SOURCE_BYTES + b'{"source":"new","target":"changed"}\n'
        )
    elif mutation == "wrong-manifest":
        if phases != ["DB_REPLACED"]:
            raise AssertionError("wrong manifest requires DB phase")
        identity.snapshot_manifest_path.write_bytes(b"wrong-manifest")
    elif mutation == "unknown-private":
        (private_root / "foreign.candidate").write_bytes(
            b"foreign-recovery-residue"
        )
    else:
        raise ValueError("unknown fixture mutation")
    return {
        "mode": "mutate",
        "pid": os.getpid(),
        "mutation": mutation,
        "before": before,
        "journal": _journal_facts(root, identity),
        "disk": _disk_facts(identity),
    }


def _creator(root: Path, phase: str) -> dict[str, object]:
    identity = _identity(root, create_source=True)
    coordinator, service = _owner(identity)
    registry = coordinator._sealed_registry
    issue_count = 0
    real_issue = registry.issue_token

    def issue_token(*args: object, **kwargs: object) -> object:
        nonlocal issue_count
        issue_count += 1
        return real_issue(*args, **kwargs)

    injected = ActivationPreparationError(
        "ACTIVATION.TEST_ORDERLY_OWNER_EXIT",
        retryable=True,
    )
    boundary_calls = 0
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(registry, "issue_token", side_effect=issue_token)
        )
        if phase == "PREPARED":
            boundary = stack.enter_context(
                mock.patch.object(
                    coordinator,
                    "publish_portable_activation",
                    side_effect=injected,
                )
            )
        elif phase == "DB_REPLACED":
            boundary = stack.enter_context(
                mock.patch.object(
                    coordinator,
                    "_apply_portable_receipt_activation",
                    side_effect=injected,
                )
            )
        elif phase == "MANIFEST_PUBLISHED":
            boundary = stack.enter_context(
                mock.patch.object(
                    coordinator,
                    "_publish_portable_generation",
                    side_effect=injected,
                )
            )
        elif phase == "GENERATION_PUBLISHED":
            boundary = stack.enter_context(
                mock.patch.object(
                    registry,
                    "consume",
                    side_effect=tm_stage_sealer.StageSealError(
                        "SEALER.ATTESTATION_UNAVAILABLE"
                    ),
                )
            )
        elif phase == "DB_ACTIVE":
            real_copy = (
                tm_stage_sealer._PortableAuthorityBorrow
                .copy_asset_to_new_candidate
            )

            def fail_manifest_copy(
                borrow: object,
                *,
                platform: object,
                parent: object,
                asset: str,
                candidate_name: str,
            ) -> object:
                nonlocal boundary_calls
                if asset == "manifest":
                    boundary_calls += 1
                    raise PlatformFileError(
                        PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                        retryable=False,
                    )
                return real_copy(
                    borrow,
                    platform=platform,
                    parent=parent,
                    asset=asset,
                    candidate_name=candidate_name,
                )

            boundary = stack.enter_context(
                mock.patch.object(
                    tm_stage_sealer._PortableAuthorityBorrow,
                    "copy_asset_to_new_candidate",
                    new=fail_manifest_copy,
                )
            )
        else:
            raise ValueError("unknown durable phase")
        outcome = service.activate_initial(
            identity.configured_jsonl_path,
            identity.resource_id,
        )
    if phase != "DB_ACTIVE":
        boundary_calls = boundary.call_count

    return {
        "mode": "create",
        "pid": os.getpid(),
        "requested_phase": phase,
        "boundary_calls": boundary_calls,
        "issue_count": issue_count,
        "outcome": _outcome_facts(outcome),
        "runtime": _runtime_facts(identity, coordinator),
        "journal": _journal_facts(root, identity),
    }


def _activate(root: Path, *, mode: str, guarded: bool) -> dict[str, object]:
    identity = _identity(root, create_source=False)
    coordinator, service = _owner(identity)
    registry = coordinator._sealed_registry
    with ExitStack() as stack:
        if guarded:
            stack.enter_context(
                mock.patch.object(
                    service,
                    "_build_stage",
                    side_effect=AssertionError(
                        "fresh portable recovery rebuilt a stage"
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    coordinator,
                    "_seal_stage",
                    side_effect=AssertionError(
                        "fresh portable recovery resealed a stage"
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    registry,
                    "issue_token",
                    side_effect=AssertionError(
                        "fresh portable recovery issued a new token"
                    ),
                )
            )
        outcome = service.activate_initial(
            identity.configured_jsonl_path,
            identity.resource_id,
        )
    return {
        "mode": mode,
        "pid": os.getpid(),
        "guarded": guarded,
        "outcome": _outcome_facts(outcome),
        "runtime": _runtime_facts(identity, coordinator),
        "journal": _journal_facts(root, identity),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=("create", "mutate", "recover", "retry", "replay"),
    )
    parser.add_argument("root", type=Path)
    parser.add_argument("--phase", choices=("PREPARED", "DB_ACTIVE", *_PHASES))
    parser.add_argument(
        "--mutation",
        choices=(
            "missing-manifest",
            "sealed-database",
            "stage-database-missing",
            "stage-database-changed",
            "source-changed",
            "wrong-manifest",
            "unknown-private",
        ),
    )
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        if arguments.mode == "create":
            if arguments.phase is None:
                parser.error("create requires --phase")
            result = _creator(root, arguments.phase)
        elif arguments.mode == "mutate":
            if arguments.mutation is None:
                parser.error("mutate requires --mutation")
            result = _mutate(root, arguments.mutation)
        else:
            result = _activate(
                root,
                mode=arguments.mode,
                guarded=arguments.mode in {"recover", "replay"},
            )
    except BaseException as error:
        result = {
            "mode": arguments.mode,
            "pid": os.getpid(),
            "exception_type": type(error).__name__,
            "exception_message": str(error),
            "traceback": "".join(traceback.format_exception(error)),
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
