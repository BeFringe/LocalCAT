"""Independent-process worker for Windows portable activation/recovery."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import traceback
from unittest import mock

import tm_activation_journal
import tm_migration
import tm_sqlite_store
import tm_stage_sealer
import platform_fs_windows
from platform_fs_contracts import (
    BoundExistingFileMutationGuard,
    BoundRegularFile,
    FileObjectIdentity,
    PendingPublication,
    PlatformFileError,
    PlatformFileErrorCode,
)
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationFailure,
    MigrationReport,
    SnapshotManifest,
    contract_from_json,
    contract_to_json,
)
from tm_engine import TMEngine
from tm_candidate_index import CandidateRetriever
from tm_sqlite_store import (
    ActivationPreparationError,
    ResourceStoreCoordinator,
    SQLiteTMStore,
)
from tools.windows_c3b_source_snapshot import source_snapshot_sha256


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
_PROCESS_SCHEMA = "localcat.windows-tm-activation-process-recovery.v1"
_REBOOT_TICKET_SCHEMA = "localcat.windows-tm-activation-reboot-ticket.v1"
_WORKER_MODULE = "tests.windows_tm_portable_publication_recovery_worker"
_REBOOT_SOURCE_KEYS = (
    "platform_fs.py",
    "platform_fs_contracts.py",
    "platform_fs_windows.py",
    "tm_activation_journal.py",
    "tm_activation_recovery.py",
    "tm_candidate_index.py",
    "tm_candidate_store_contracts.py",
    "tm_content_attestation.py",
    "tm_contracts.py",
    "tm_engine.py",
    "tm_migration.py",
    "tm_schema_upgrade.py",
    "tm_similarity.py",
    "tm_snapshot_artifacts.py",
    "tm_snapshot_recovery.py",
    "tm_sqlite_candidate_projection.py",
    "tm_sqlite_store.py",
    "tm_stage_sealer.py",
    "tests/windows_tm_portable_publication_recovery_worker.py",
    "windows_file_api.py",
)
_PROCESS_TERMINATION_PHASES = (
    "LOCK_HELD",
    "PREPARED",
    "DB_REPLACED",
    "MANIFEST_PUBLISHED",
    "GENERATION_PUBLISHED",
    *(
        f"{asset}.{boundary}"
        for asset in ("DB", "MANIFEST")
        for boundary in (
            "post_begin",
            "owner_durable",
            "business_reproof",
            "terminal_reproof",
        )
    ),
)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_marker(marker: Path, phase: str, details: object = None) -> None:
    payload = {
        "details": details,
        "phase": phase,
        "pid": os.getpid(),
        "schema": _PROCESS_SCHEMA,
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _park_at_marker(marker: Path, phase: str, details: object = None) -> None:
    _write_marker(marker, phase, details)
    while True:
        time.sleep(30.0)


def _wait_for_path(path: Path, *, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path.name}")
        time.sleep(0.02)


def _terminate_process(process: subprocess.Popen[bytes], exit_code: int = 93) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    terminate = kernel32.TerminateProcess
    terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    terminate.restype = ctypes.c_int32
    if not terminate(int(process._handle), exit_code):
        raise ctypes.WinError(ctypes.get_last_error())


def _current_boot_session() -> str:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToFileTimeUtc()",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    boot_session = completed.stdout.strip()
    if (
        completed.returncode != 0
        or not boot_session.isascii()
        or not boot_session.isdigit()
    ):
        raise RuntimeError("Windows boot session is unavailable")
    return boot_session


def _source_snapshot() -> str:
    return source_snapshot_sha256(
        Path(__file__).resolve().parents[1],
        keys=_REBOOT_SOURCE_KEYS,
    )


def _coordinator_projection(
    coordinator: ResourceStoreCoordinator,
) -> dict[str, object]:
    return {
        "generation": coordinator.current_generation,
        "state": coordinator.state,
        "view_visible": coordinator._view is not None,
    }


class _FreshReproveFaultGuard(BoundExistingFileMutationGuard):
    __slots__ = ("delegate", "primary", "cleanup", "delegate_closed")

    def __init__(
        self,
        delegate: BoundExistingFileMutationGuard,
        primary: BaseException,
        cleanup: BaseException,
    ) -> None:
        identity = delegate.reprove()
        if type(identity) is not FileObjectIdentity:
            raise TypeError("delegate guard returned invalid identity")
        super().__init__(identity)
        self.delegate = delegate
        self.primary = primary
        self.cleanup = cleanup
        self.delegate_closed = False

    def _reprove_guard(self) -> FileObjectIdentity:
        raise self.primary

    def _close_authority(self) -> None:
        try:
            self.delegate.close()
        finally:
            self.delegate_closed = self.delegate.closed
        raise self.cleanup


def _prove_paths_released(paths: tuple[Path, ...]) -> dict[str, bool]:
    released: dict[str, bool] = {}
    for target in paths:
        if not target.is_file():
            released[target.name] = False
            continue
        replacement = target.with_name(target.name + ".release-probe")
        replacement.write_bytes(target.read_bytes())
        try:
            os.replace(replacement, target)
        except PermissionError:
            released[target.name] = False
        else:
            released[target.name] = True
        finally:
            if replacement.exists():
                replacement.unlink()
    return released


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
    active_attestation = None
    if records:
        active = records[-1].unsigned.active_content_attestation
        if active is not None:
            active_attestation = {
                "generation": active.generation,
                "activation_digest": active.activation_digest,
                "attestation_digest": active.attestation_digest,
                "snapshot_receipt_digest": active.snapshot_receipt_digest,
                "database_sha256": active.database.sha256,
                "manifest_sha256": active.manifest.sha256,
                "source_sha256": active.source.sha256,
                "index_kind": active.semantic_facts.candidate_index_kind,
                "sqlite_version": active.semantic_facts.sqlite_runtime_version,
                "unicode_version": active.semantic_facts.unicode_runtime_version,
                "fts5_available": active.semantic_facts.fts5_available,
            }

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
        "active_attestation": active_attestation,
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
            fts_ddl_row = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'tm_fts'"
            ).fetchone()
            return {
                "meta": dict(connection.execute("SELECT key, value FROM tm_meta")),
                "fts_ddl": None if fts_ddl_row is None else fts_ddl_row[0],
                "compile_options": sorted(
                    row[0] for row in connection.execute("PRAGMA compile_options")
                ),
                "schema_names": [
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
                    )
                ],
                "integrity_check": connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0],
                "foreign_key_check": connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall(),
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
        "lineage_marker": _file_facts(
            tm_sqlite_store._activation_lineage_marker_path(identity)
        ),
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


def _fts5_activate(root: Path) -> dict[str, object]:
    """Publish one real FTS5 canonical generation before process exit."""

    identity = _identity(root, create_source=True)
    coordinator, service = _owner(identity)
    outcome = service.activate_initial(
        identity.configured_jsonl_path,
        identity.resource_id,
    )
    return {
        "mode": "fts5-activate",
        "pid": os.getpid(),
        "outcome": _outcome_facts(outcome),
        "runtime": _runtime_facts(identity, coordinator),
        "journal": _journal_facts(root, identity),
        "disk": _disk_facts(identity),
    }


def _fts5_cold_query(root: Path) -> dict[str, object]:
    """Cold-open the published authority and query through production ports."""

    identity = _identity(root, create_source=False)
    engine = TMEngine(
        str(identity.configured_jsonl_path),
        update=False,
        expected_resource_id=identity.resource_id,
    )
    store = engine.canonical_store
    if not engine.canonical_active or store is None:
        raise AssertionError("cold open did not bind the canonical store")

    runtime = tm_sqlite_store.detect_sqlite_runtime()
    health = store.health()
    with store.query_lease() as view:
        leased_health = view.health()
        report = CandidateRetriever().candidates_from_view(
            identity.resource_id,
            view,
            "sam",
            result_limit=10,
        )
        record_ids = tuple(candidate.record_id for candidate in report.candidates)
        records = view.records_by_id(record_ids)

    return {
        "mode": "fts5-cold-query",
        "pid": os.getpid(),
        "runtime_capability": {
            "sqlite_version": runtime.sqlite_version,
            "unicode_version": runtime.unicode_version,
            "fts5_available": runtime.fts5_available,
        },
        "health": {
            "healthy": health.healthy,
            "schema_version": health.schema_version,
            "generation": health.generation,
            "record_count": health.record_count,
            "index_kind": health.index_kind,
            "snapshot_binding_digest": health.snapshot_binding_digest,
            "source_binding_state": (
                None
                if health.source_binding_state is None
                else health.source_binding_state.value
            ),
            "exact_available": health.exact_available,
            "diagnostic_codes": health.diagnostic_codes,
            "leased_equal": leased_health == health,
        },
        "query": {
            "folded_query": "sam",
            "candidate_ids": record_ids,
            "candidate_stages": [
                candidate_stage.stage.value
                for candidate_stage in report.metadata.stages
            ],
            "candidate_pretruncate_ranks": [
                candidate.pretruncate_rank for candidate in report.candidates
            ],
            "candidate_recall_stages": [
                [stage.value for stage in candidate.recall_stages]
                for candidate in report.candidates
            ],
            "record_ids": [record.record_id for record in records],
            "record_sources": [record.source_raw for record in records],
            "record_targets": [record.target_raw for record in records],
            "index_kind": report.metadata.index_kind,
            "fuzzy_available": report.metadata.fuzzy_available,
            "unavailable_code": report.metadata.fuzzy_unavailable_code,
        },
        "journal": _journal_facts(root, identity),
        "disk": _disk_facts(identity),
    }


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
    elif mutation == "physical-manifest-ahead-sealed":
        if phases != ["DB_REPLACED"]:
            raise AssertionError("physical manifest ahead requires DB phase")
        stage_manifest = root / pending.unsigned.candidate_manifest_temp_name
        identity.snapshot_manifest_path.write_bytes(stage_manifest.read_bytes())
    elif mutation in {
        "same-byte-database",
        "same-byte-stage-database",
        "same-byte-source",
    }:
        if phases != ["DB_REPLACED"]:
            raise AssertionError("same-byte replacement requires DB phase")
        if mutation == "same-byte-database":
            target = identity.canonical_sidecar_path
        elif mutation == "same-byte-stage-database":
            target = root / pending.unsigned.candidate_stage_db_name
        else:
            target = identity.configured_jsonl_path
        replacement = root / (target.name + ".same-byte-replacement")
        replacement.write_bytes(target.read_bytes())
        os.replace(replacement, target)
    elif mutation == "foreign-database-replacement":
        if phases != ["DB_REPLACED"]:
            raise AssertionError("foreign database replacement requires DB phase")
        replacement = root / "foreign-database-replacement"
        replacement.write_bytes(b"foreign-different-database")
        os.replace(replacement, identity.canonical_sidecar_path)
    elif mutation == "foreign-manifest-replacement":
        if phases != ["DB_REPLACED", "MANIFEST_PUBLISHED"]:
            raise AssertionError("foreign manifest replacement requires MANIFEST phase")
        replacement = root / "foreign-manifest-replacement"
        replacement.write_bytes(b"foreign-different-manifest")
        os.replace(replacement, identity.snapshot_manifest_path)
    elif mutation == "stage-database-multilink":
        if phases != ["DB_REPLACED"]:
            raise AssertionError("stage hardlink requires DB phase")
        stage = root / pending.unsigned.candidate_stage_db_name
        os.link(stage, root / "foreign-stage-hardlink")
    elif mutation == "stage-database-junction":
        if phases != ["DB_REPLACED"]:
            raise AssertionError("stage junction requires DB phase")
        stage = root / pending.unsigned.candidate_stage_db_name
        stage.unlink()
        junction_target = root / "foreign-junction-target"
        junction_target.mkdir()
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(stage), str(junction_target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr or completed.stdout)
    elif mutation == "private-journal-foreign-acl":
        replacement = root / "foreign-private-journal"
        replacement.write_bytes(pending_path.read_bytes())
        os.replace(replacement, pending_path)
    elif mutation == "private-generation-owner-envelope":
        if phases != [
            "DB_REPLACED",
            "MANIFEST_PUBLISHED",
            "GENERATION_PUBLISHED",
        ]:
            raise AssertionError(
                "private proof mutation requires completed generation"
            )
        generation_path = (
            private_root
            / tm_activation_journal._portable_publication_phase_name(
                "GENERATION_PUBLISHED"
            )
        )
        generation = (
            tm_activation_journal._parse_portable_publication_phase_bytes(
                generation_path.read_bytes()
            )
        )
        tampered_unsigned = replace(
            generation.unsigned,
            token_id="token.tampered-owner-envelope",
        )
        tampered_proof = replace(
            generation.private_directory_proof,
            owner_context_sha256=(
                tm_activation_journal._portable_publication_owner_context_sha256(
                    tampered_unsigned
                )
            ),
            device_secret_mac=b"x" * 32,
        )
        tampered = (
            tm_activation_journal._create_portable_publication_phase_record(
                tampered_unsigned,
                tampered_proof,
            )
        )
        generation_path.write_bytes(
            tm_activation_journal._serialize_portable_publication_phase_record(
                tampered
            )
        )
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


def _cold_open_private_failure(
    root: Path,
    *,
    fail_narrowing: bool,
) -> dict[str, object]:
    identity = _identity(root, create_source=False)
    before_disk = _disk_facts(identity)
    before_journal = _journal_facts(root, identity)
    generic_fallback_calls = 0

    def reject_generic_fallback(
        coordinator: ResourceStoreCoordinator,
    ) -> object:
        nonlocal generic_fallback_calls
        del coordinator
        generic_fallback_calls += 1
        raise AssertionError("generic legacy/v2 fallback used")

    error: ValueError | None = None
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(
                ResourceStoreCoordinator,
                "rehydrate_runtime_authority",
                new=reject_generic_fallback,
            )
        )
        if fail_narrowing:
            stack.enter_context(
                mock.patch(
                    "tm_migration.narrow_windows_persistent_private_proof",
                    side_effect=PlatformFileError(
                        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                        retryable=False,
                    ),
                )
            )
        try:
            TMEngine(
                str(identity.configured_jsonl_path),
                update=False,
                expected_resource_id=identity.resource_id,
            )
        except ValueError as caught:
            error = caught
    if error is None:
        raise AssertionError("private cold open unexpectedly succeeded")
    return {
        "mode": (
            "cold-open-narrow-failure"
            if fail_narrowing
            else "cold-open-private-tamper"
        ),
        "pid": os.getpid(),
        "failure_type": type(error).__name__,
        "failure_message": str(error),
        "generic_fallback_calls": generic_fallback_calls,
        "before_disk": before_disk,
        "after_disk": _disk_facts(identity),
        "before_journal": before_journal,
        "after_journal": _journal_facts(root, identity),
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
    expected_primary: BaseException | None = None
    caught_error: BaseException | None = None
    cleanup_faults = 0
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
        elif phase == "MANIFEST_PHYSICAL":
            real_publish = (
                tm_activation_journal._WindowsPortablePublicationPhaseOwner.publish
            )

            def fail_manifest_phase(**kwargs: object) -> object:
                nonlocal boundary_calls
                unsigned = kwargs.get("unsigned")
                if (
                    isinstance(
                        unsigned,
                        tm_activation_journal._PortablePublicationPhaseUnsigned,
                    )
                    and unsigned.phase == "MANIFEST_PUBLISHED"
                ):
                    boundary_calls += 1
                    raise injected
                return real_publish(**kwargs)

            boundary = stack.enter_context(
                mock.patch.object(
                    tm_activation_journal._WindowsPortablePublicationPhaseOwner,
                    "publish",
                    new=fail_manifest_phase,
                )
            )
        elif phase == "DB_MUTATION_GUARD":
            real_open_connection = tm_sqlite_store._open_configured_connection

            def attempt_same_byte_swap(path: Path, **kwargs: object) -> object:
                nonlocal boundary_calls
                if path == identity.canonical_sidecar_path and path.is_file():
                    boundary_calls += 1
                    replacement = path.with_name(path.name + ".live-swap")
                    replacement.write_bytes(path.read_bytes())
                    try:
                        try:
                            os.replace(replacement, path)
                        except PermissionError:
                            pass
                        else:
                            raise AssertionError(
                                "mutation guard allowed a live same-byte swap"
                            )
                    finally:
                        if replacement.exists():
                            replacement.unlink()
                return real_open_connection(path, **kwargs)

            boundary = stack.enter_context(
                mock.patch.object(
                    tm_sqlite_store,
                    "_open_configured_connection",
                    new=attempt_same_byte_swap,
                )
            )
        elif phase == "ACTIVE_SET_CLOSE":
            real_close = tm_sqlite_store._PortableActiveSetAuthority._close_authority

            def fail_active_set_close(authority: object) -> None:
                nonlocal boundary_calls
                boundary_calls += 1
                real_close(authority)
                raise PlatformFileError(
                    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                    retryable=False,
                )

            boundary = stack.enter_context(
                mock.patch.object(
                    tm_sqlite_store._PortableActiveSetAuthority,
                    "_close_authority",
                    new=fail_active_set_close,
                )
            )
        elif phase == "APPLY_PROGRAMMER_CLOSE":
            expected_primary = AssertionError("apply-business-primary")
            real_open_connection = tm_sqlite_store._open_configured_connection
            real_guard_init = (
                platform_fs_windows._WindowsExistingFileMutationGuard.__init__
            )
            real_guard_close = (
                platform_fs_windows._WindowsExistingFileMutationGuard
                ._close_authority
            )
            guard_acquired = False

            def record_guard_acquisition(*args: object, **kwargs: object) -> None:
                nonlocal guard_acquired
                real_guard_init(*args, **kwargs)
                guard_acquired = True

            def fail_apply_business(path: Path, **kwargs: object) -> object:
                nonlocal boundary_calls
                if (
                    guard_acquired
                    and path == identity.canonical_sidecar_path
                    and path.is_file()
                ):
                    boundary_calls += 1
                    assert expected_primary is not None
                    raise expected_primary
                return real_open_connection(path, **kwargs)

            def fail_guard_cleanup(authority: object) -> None:
                nonlocal cleanup_faults
                real_guard_close(authority)
                cleanup_faults += 1
                raise AttributeError("apply-guard-cleanup")

            stack.enter_context(
                mock.patch.object(
                    platform_fs_windows._WindowsExistingFileMutationGuard,
                    "__init__",
                    new=record_guard_acquisition,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    tm_sqlite_store,
                    "_open_configured_connection",
                    new=fail_apply_business,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    platform_fs_windows._WindowsExistingFileMutationGuard,
                    "_close_authority",
                    new=fail_guard_cleanup,
                )
            )
        elif phase == "SYNCHRONIZED_CLOSE":
            real_sync_close = (
                platform_fs_windows._WindowsBoundSynchronizedRegularFile
                ._close_authority
            )

            def fail_synchronized_close(authority: object) -> None:
                nonlocal boundary_calls, cleanup_faults
                real_sync_close(authority)
                if Path(getattr(authority, "_entry_path")).name == (
                    identity.canonical_sidecar_path.name
                ):
                    boundary_calls += 1
                    cleanup_faults += 1
                    raise PlatformFileError(
                        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                        retryable=False,
                    )

            stack.enter_context(
                mock.patch.object(
                    platform_fs_windows._WindowsBoundSynchronizedRegularFile,
                    "_close_authority",
                    new=fail_synchronized_close,
                )
            )
        elif phase == "ACTIVE_REPROVE_PROGRAMMER_CLOSE":
            expected_primary = AssertionError("active-reprove-primary")
            primary_raised = False
            real_file_close = (
                platform_fs_windows._WindowsBoundRegularFile._close_authority
            )
            real_guard_close = (
                platform_fs_windows._WindowsExistingFileMutationGuard
                ._close_authority
            )

            def fail_live_construction(*args: object, **kwargs: object) -> None:
                nonlocal primary_raised, boundary_calls
                del args, kwargs
                primary_raised = True
                boundary_calls += 1
                assert expected_primary is not None
                raise expected_primary

            def fail_file_cleanup(authority: object) -> None:
                nonlocal cleanup_faults
                real_file_close(authority)
                if primary_raised:
                    cleanup_faults += 1
                    raise AttributeError("active-file-cleanup")

            def fail_active_guard_cleanup(authority: object) -> None:
                nonlocal cleanup_faults
                real_guard_close(authority)
                if primary_raised:
                    cleanup_faults += 1
                    raise AttributeError("active-guard-cleanup")

            stack.enter_context(
                mock.patch.object(
                    tm_sqlite_store._PortableActiveSetAuthority,
                    "__init__",
                    new=fail_live_construction,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    platform_fs_windows._WindowsBoundRegularFile,
                    "_close_authority",
                    new=fail_file_cleanup,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    platform_fs_windows._WindowsExistingFileMutationGuard,
                    "_close_authority",
                    new=fail_active_guard_cleanup,
                )
            )
        else:
            raise ValueError("unknown durable phase")
        try:
            outcome = service.activate_initial(
                identity.configured_jsonl_path,
                identity.resource_id,
            )
        except BaseException as error:
            if expected_primary is None:
                raise
            caught_error = error
            outcome = None
    if phase not in {
        "DB_ACTIVE",
        "MANIFEST_PHYSICAL",
        "DB_MUTATION_GUARD",
        "ACTIVE_SET_CLOSE",
        "APPLY_PROGRAMMER_CLOSE",
        "SYNCHRONIZED_CLOSE",
        "ACTIVE_REPROVE_PROGRAMMER_CLOSE",
    }:
        boundary_calls = boundary.call_count

    released = (
        _prove_paths_released(
            (
                identity.canonical_sidecar_path,
                identity.snapshot_manifest_path,
                identity.configured_jsonl_path,
            )
        )
        if phase in {
            "APPLY_PROGRAMMER_CLOSE",
            "SYNCHRONIZED_CLOSE",
            "ACTIVE_REPROVE_PROGRAMMER_CLOSE",
        }
        else {}
    )

    return {
        "mode": "create",
        "pid": os.getpid(),
        "requested_phase": phase,
        "boundary_calls": boundary_calls,
        "issue_count": issue_count,
        "outcome": _outcome_facts(outcome),
        "caught_exception_type": (
            None if caught_error is None else type(caught_error).__name__
        ),
        "caught_exception_message": (
            None if caught_error is None else str(caught_error)
        ),
        "caught_is_primary": (
            caught_error is not None and caught_error is expected_primary
        ),
        "cleanup_faults": cleanup_faults,
        "released": released,
        "runtime": _runtime_facts(identity, coordinator),
        "journal": _journal_facts(root, identity),
    }


def _activate(
    root: Path,
    *,
    mode: str,
    guarded: bool,
    reject_database_materialization: bool = False,
    probe_mutation_guard: bool = False,
    ready_swap: str | None = None,
    fail_active_set_close: bool = False,
    fail_fresh_guard_reprove_close: bool = False,
) -> dict[str, object]:
    identity = _identity(root, create_source=False)
    coordinator, service = _owner(identity)
    registry = coordinator._sealed_registry
    boundary_calls = 0
    expected_primary: BaseException | None = None
    caught_error: BaseException | None = None
    fault_guard: _FreshReproveFaultGuard | None = None
    with ExitStack() as stack:
        if reject_database_materialization:
            private_root = (
                root
                / tm_activation_journal._portable_activation_private_directory_name(
                    identity
                )
            )
            pending = tm_activation_journal._parse_portable_activation_journal_bytes(
                (private_root / "activation-journal-v3.json").read_bytes()
            )
            database_names = {
                identity.canonical_sidecar_path.name,
                pending.unsigned.candidate_stage_db_name,
            }
            real_read_all = BoundRegularFile.read_all

            def bounded_read_all(authority: BoundRegularFile) -> bytes:
                entry_path = getattr(authority, "_entry_path", "")
                if Path(entry_path).name in database_names:
                    raise AssertionError("fresh recovery materialized a database")
                return real_read_all(authority)

            stack.enter_context(
                mock.patch.object(
                    BoundRegularFile,
                    "read_all",
                    new=bounded_read_all,
                )
            )
        if probe_mutation_guard:
            real_open_connection = tm_sqlite_store._open_configured_connection

            def attempt_same_byte_swap(path: Path, **kwargs: object) -> object:
                nonlocal boundary_calls
                if path == identity.canonical_sidecar_path and path.is_file():
                    boundary_calls += 1
                    replacement = path.with_name(path.name + ".fresh-live-swap")
                    replacement.write_bytes(path.read_bytes())
                    try:
                        try:
                            os.replace(replacement, path)
                        except PermissionError:
                            pass
                        else:
                            raise AssertionError(
                                "fresh recovery mutation guard allowed replacement"
                            )
                    finally:
                        if replacement.exists():
                            replacement.unlink()
                return real_open_connection(path, **kwargs)

            stack.enter_context(
                mock.patch.object(
                    tm_sqlite_store,
                    "_open_configured_connection",
                    new=attempt_same_byte_swap,
                )
            )
        if ready_swap is not None:
            real_marker = coordinator._ensure_portable_activation_lineage_marker

            def attempt_ready_swap(*args: object, **kwargs: object) -> None:
                nonlocal boundary_calls
                boundary_calls += 1
                if ready_swap.startswith("manifest-"):
                    target = identity.snapshot_manifest_path
                elif ready_swap.startswith("source-"):
                    target = identity.configured_jsonl_path
                else:
                    raise AssertionError("unknown READY swap target")
                payload = target.read_bytes()
                if ready_swap.endswith("different"):
                    payload += b"foreign-post-business-swap"
                replacement = target.with_name(target.name + ".ready-swap")
                replacement.write_bytes(payload)
                try:
                    try:
                        os.replace(replacement, target)
                    except PermissionError:
                        pass
                    else:
                        raise AssertionError(
                            "active-set authority allowed a pre-READY swap"
                        )
                finally:
                    if replacement.exists():
                        replacement.unlink()
                real_marker(*args, **kwargs)

            stack.enter_context(
                mock.patch.object(
                    coordinator,
                    "_ensure_portable_activation_lineage_marker",
                    new=attempt_ready_swap,
                )
            )
        if fail_active_set_close:
            real_close = tm_sqlite_store._PortableActiveSetAuthority._close_authority

            def fail_close(authority: object) -> None:
                nonlocal boundary_calls
                boundary_calls += 1
                real_close(authority)
                raise PlatformFileError(
                    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                    retryable=False,
                )

            stack.enter_context(
                mock.patch.object(
                    tm_sqlite_store._PortableActiveSetAuthority,
                    "_close_authority",
                    new=fail_close,
                )
            )
        if fail_fresh_guard_reprove_close:
            expected_primary = AssertionError("fresh-guard-reprove-primary")
            cleanup = AttributeError("fresh-guard-close-cleanup")
            real_apply = (
                tm_sqlite_store._CoordinatorStorePort
                .apply_portable_receipt_activation
            )

            def return_faulting_guard(
                port: object,
                **kwargs: object,
            ) -> object:
                nonlocal fault_guard, boundary_calls
                database, closure, guard = real_apply(port, **kwargs)
                assert expected_primary is not None
                fault_guard = _FreshReproveFaultGuard(
                    guard,
                    expected_primary,
                    cleanup,
                )
                boundary_calls += 1
                return database, closure, fault_guard

            stack.enter_context(
                mock.patch.object(
                    tm_sqlite_store._CoordinatorStorePort,
                    "apply_portable_receipt_activation",
                    new=return_faulting_guard,
                )
            )
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
        try:
            outcome = service.activate_initial(
                identity.configured_jsonl_path,
                identity.resource_id,
            )
        except BaseException as error:
            if expected_primary is None:
                raise
            caught_error = error
            outcome = None
    released = (
        _prove_paths_released((identity.canonical_sidecar_path,))
        if fail_fresh_guard_reprove_close
        else {}
    )
    return {
        "mode": mode,
        "pid": os.getpid(),
        "guarded": guarded,
        "boundary_calls": boundary_calls,
        "outcome": _outcome_facts(outcome),
        "caught_exception_type": (
            None if caught_error is None else type(caught_error).__name__
        ),
        "caught_exception_message": (
            None if caught_error is None else str(caught_error)
        ),
        "caught_is_primary": (
            caught_error is not None and caught_error is expected_primary
        ),
        "fault_guard_closed": (
            fault_guard is not None
            and fault_guard.closed
            and fault_guard.delegate_closed
        ),
        "released": released,
        "runtime": _runtime_facts(identity, coordinator),
        "journal": _journal_facts(root, identity),
        "disk": _disk_facts(identity),
    }


def _asset_inventory(identity: CanonicalResourceIdentity) -> dict[str, object]:
    return {
        "database": _file_facts(identity.canonical_sidecar_path),
        "lineage_marker": _file_facts(
            tm_sqlite_store._activation_lineage_marker_path(identity)
        ),
        "manifest": _file_facts(identity.snapshot_manifest_path),
        "source": _file_facts(identity.configured_jsonl_path),
    }


def _pending_destination_name(pending: PendingPublication) -> str | None:
    retained = pending.retained_destination()
    entry_path = getattr(retained, "_entry_path", None)
    if type(entry_path) is not str:
        return None
    return Path(entry_path).name


def _install_activation_process_fault(
    stack: ExitStack,
    *,
    phase: str,
    marker: Path,
    identity: CanonicalResourceIdentity,
    coordinator: ResourceStoreCoordinator,
    service: tm_migration.TMMigrationService,
) -> None:
    if phase not in _PROCESS_TERMINATION_PHASES:
        raise ValueError("unknown process-termination phase")

    if phase == "LOCK_HELD":
        def hold_resource_owner(**kwargs: object) -> object:
            del kwargs
            _park_at_marker(
                marker,
                phase,
                {
                    "coordinator": _coordinator_projection(coordinator),
                    "owner": "activate_initial",
                },
            )

        stack.enter_context(
            mock.patch.object(
                service,
                "_activate_initial_with_resource_reservation",
                new=hold_resource_owner,
            )
        )
        return

    if phase in {"PREPARED", *_PHASES}:
        registry = coordinator._sealed_registry

        def hold(*args: object, **kwargs: object) -> object:
            del args, kwargs
            _park_at_marker(
                marker,
                phase,
                {
                    "coordinator": _coordinator_projection(coordinator),
                    "owner": "activation_journal",
                },
            )

        if phase == "PREPARED":
            target = coordinator
            member = "publish_portable_activation"
        elif phase == "DB_REPLACED":
            target = coordinator
            member = "_apply_portable_receipt_activation"
        elif phase == "MANIFEST_PUBLISHED":
            target = coordinator
            member = "_publish_portable_generation"
        else:
            target = registry
            member = "consume"
        stack.enter_context(mock.patch.object(target, member, new=hold))
        return

    asset, boundary = phase.split(".", 1)
    journal_phase = {
        "DB": "DB_REPLACED",
        "MANIFEST": "MANIFEST_PUBLISHED",
    }[asset]
    destination_name = {
        "DB": identity.canonical_sidecar_path.name,
        "MANIFEST": identity.snapshot_manifest_path.name,
    }[asset]

    if boundary == "terminal_reproof":
        real_terminal = PendingPublication.terminal_reproof

        def terminal(pending: PendingPublication) -> object:
            if _pending_destination_name(pending) == destination_name:
                _park_at_marker(
                    marker,
                    phase,
                    {
                        "asset": asset,
                        "boundary": boundary,
                        "coordinator": _coordinator_projection(coordinator),
                        "preliminary": True,
                    },
                )
            return real_terminal(pending)

        stack.enter_context(
            mock.patch.object(PendingPublication, "terminal_reproof", new=terminal)
        )
        return

    real_publish = tm_activation_journal._WindowsPortablePublicationPhaseOwner.publish

    def publish(**kwargs: object) -> object:
        unsigned = kwargs.get("unsigned")
        if getattr(unsigned, "phase", None) != journal_phase:
            return real_publish(**kwargs)
        if boundary == "post_begin":
            _park_at_marker(
                marker,
                phase,
                {
                    "asset": asset,
                    "boundary": boundary,
                    "coordinator": _coordinator_projection(coordinator),
                },
            )
        if boundary == "owner_durable":
            real_owner_commit = kwargs["owner_commit"]

            def owner_commit(record: object) -> None:
                result = real_owner_commit(record)
                if result is not None:
                    raise TypeError("owner commit must return None")
                _park_at_marker(
                    marker,
                    phase,
                    {
                        "asset": asset,
                        "boundary": boundary,
                        "coordinator": _coordinator_projection(coordinator),
                    },
                )

            kwargs["owner_commit"] = owner_commit
        elif boundary == "business_reproof":
            real_business_reprove = kwargs["business_reprove"]

            def business_reprove(record: object) -> None:
                result = real_business_reprove(record)
                if result is not None:
                    raise TypeError("business reproof must return None")
                _park_at_marker(
                    marker,
                    phase,
                    {
                        "asset": asset,
                        "boundary": boundary,
                        "coordinator": _coordinator_projection(coordinator),
                    },
                )

            kwargs["business_reprove"] = business_reprove
        return real_publish(**kwargs)

    stack.enter_context(
        mock.patch.object(
            tm_activation_journal._WindowsPortablePublicationPhaseOwner,
            "publish",
            new=publish,
        )
    )


def _activation_process_fault(
    root: Path,
    phase: str,
    marker: Path,
) -> dict[str, object]:
    identity = _identity(root, create_source=not _identity_source_exists(root))
    coordinator, service = _owner(identity)
    with ExitStack() as stack:
        _install_activation_process_fault(
            stack,
            phase=phase,
            marker=marker,
            identity=identity,
            coordinator=coordinator,
            service=service,
        )
        outcome = service.activate_initial(
            identity.configured_jsonl_path,
            identity.resource_id,
        )
    return {
        "mode": "activation-fault",
        "phase": phase,
        "outcome": _outcome_facts(outcome),
    }


def _identity_source_exists(root: Path) -> bool:
    return (root / "tm.primary.jsonl").is_file()


def _cold_open_probe(root: Path) -> dict[str, object]:
    identity = _identity(root, create_source=False)
    engine: TMEngine | None = None
    exact: object | None = None
    error: BaseException | None = None
    try:
        engine = TMEngine(
            str(identity.configured_jsonl_path),
            update=False,
        )
        exact = engine.query_exact("same")
    except (ValueError, OSError) as caught:
        error = caught
    canonical_active = engine is not None and engine.canonical_active
    canonical_store_visible = engine is not None and engine.canonical_store is not None
    exact_result = (
        None
        if exact is None
        else {
            "source": getattr(exact, "source"),
            "target": getattr(exact, "target"),
        }
    )
    return {
        "mode": "cold-open-probe",
        "pid": os.getpid(),
        "canonical_active": canonical_active,
        "runtime_profile": (
            "CANONICAL"
            if canonical_active and canonical_store_visible
            else "LEGACY"
            if engine is not None and exact_result is not None
            else "UNAVAILABLE"
        ),
        "canonical_store_visible": canonical_store_visible,
        "exact_result": exact_result,
        "failure_type": None if error is None else type(error).__name__,
        "failure_message": None if error is None else str(error),
    }


def _inspect_process_state(root: Path) -> dict[str, object]:
    identity = _identity(root, create_source=False)
    return {
        "mode": "inspect",
        "pid": os.getpid(),
        "inventory": _asset_inventory(identity),
        "journal": _journal_facts(root, identity),
    }


def _reboot_inventory(root: Path) -> dict[str, object]:
    identity = _identity(root, create_source=False)
    journal = _journal_facts(root, identity)
    return {
        "assets": _asset_inventory(identity),
        "journal": {
            "phase_files": journal["phase_files"],
            "phases": journal["phases"],
            "predecessor_closed": journal["predecessor_closed"],
            "prepared": journal["prepared"],
            "terminal": journal["terminal"],
        },
    }


def _load_reboot_ticket(root: Path, ticket_path: Path) -> dict[str, object]:
    raw = ticket_path.read_bytes()
    ticket = json.loads(raw.decode("utf-8"))
    if type(ticket) is not dict or _canonical_json_bytes(ticket) != raw:
        raise RuntimeError("activation reboot ticket is not canonical")
    expected_keys = {
        "boot_session",
        "expected",
        "inventory",
        "root",
        "schema",
        "source_snapshot_sha256",
        "ticket_sha256",
        "worker_sha256",
    }
    if set(ticket) != expected_keys or ticket["schema"] != _REBOOT_TICKET_SCHEMA:
        raise RuntimeError("activation reboot ticket shape mismatch")
    unsigned = dict(ticket)
    digest = unsigned.pop("ticket_sha256")
    if (
        ticket["root"] != str(root)
        or ticket["worker_sha256"]
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        or ticket["source_snapshot_sha256"] != _source_snapshot()
        or digest != hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()
    ):
        raise RuntimeError("activation reboot ticket authority mismatch")
    return ticket


def _reboot_prepare(root: Path, ticket_path: Path) -> dict[str, object]:
    if ticket_path.parent != root.parent:
        raise ValueError("activation reboot root and ticket must share one directory")
    if root.exists() or ticket_path.exists():
        raise ValueError("activation reboot prepare requires a new root and ticket")
    marker = ticket_path.with_suffix(ticket_path.suffix + ".phase.json")
    if marker.exists():
        raise ValueError("activation reboot marker already exists")
    command = [
        sys.executable,
        "-B",
        "-m",
        _WORKER_MODULE,
        "activation-fault",
        str(root),
        "--phase",
        "GENERATION_PUBLISHED",
        "--marker",
        str(marker),
    ]
    process = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    terminated = False
    try:
        _wait_for_path(marker)
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                "activation reboot creator exited before termination: "
                f"{process.returncode}; stdout={stdout!r}; stderr={stderr!r}"
            )
        _terminate_process(process)
        terminated = True
        stdout, stderr = process.communicate(timeout=30.0)
        if process.returncode != 93:
            raise AssertionError(
                "activation reboot creator exit mismatch: "
                f"{process.returncode}; stdout={stdout!r}; stderr={stderr!r}"
            )
    finally:
        if process.poll() is None:
            _terminate_process(process)
            process.communicate(timeout=30.0)
        if not terminated and marker.exists():
            marker.unlink()

    inventory = _reboot_inventory(root)
    if inventory["journal"]["phases"] != list(_PHASES):
        raise AssertionError("reboot preparation did not reach generation phase")
    unsigned = {
        "boot_session": _current_boot_session(),
        "expected": {
            "generation": 0,
            "query": {"source": "same", "targets": ["winner", "first"]},
            "record_count": 3,
        },
        "inventory": inventory,
        "root": str(root),
        "schema": _REBOOT_TICKET_SCHEMA,
        "source_snapshot_sha256": _source_snapshot(),
        "worker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    ticket = {
        **unsigned,
        "ticket_sha256": hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest(),
    }
    ticket_bytes = _canonical_json_bytes(ticket)
    with ticket_path.open("xb") as stream:
        stream.write(ticket_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    if ticket_path.read_bytes() != ticket_bytes:
        raise RuntimeError("activation reboot ticket readback mismatch")
    return {
        "mode": "activation-reboot-prepare",
        "reboot": "PREPARED",
        "schema": _REBOOT_TICKET_SCHEMA,
        "ticket": str(ticket_path),
        "ticket_sha256": ticket["ticket_sha256"],
    }


def _reboot_resume(root: Path, ticket_path: Path) -> dict[str, object]:
    ticket = _load_reboot_ticket(root, ticket_path)
    if _current_boot_session() == ticket["boot_session"]:
        return {
            "mode": "activation-reboot-resume",
            "reboot": "NOT_RUN",
            "schema": _REBOOT_TICKET_SCHEMA,
            "ticket_sha256": ticket["ticket_sha256"],
        }
    if _reboot_inventory(root) != ticket["inventory"]:
        raise RuntimeError("activation reboot inventory changed before resume")
    recovered = _activate(root, mode="reboot-recover", guarded=True)
    query = _fts5_cold_query(root)
    expected = ticket["expected"]
    if (
        recovered["outcome"].get("kind") != "MigrationReport"
        or recovered["outcome"].get("generation") != expected["generation"]
        or recovered["outcome"].get("record_count") != expected["record_count"]
        or recovered["runtime"]["canonical"]["same_targets"]
        != expected["query"]["targets"]
        or query["health"]["generation"] != expected["generation"]
        or query["query"]["record_targets"] != ["first", "winner"]
    ):
        raise AssertionError("activation reboot recovery result mismatch")
    return {
        "mode": "activation-reboot-resume",
        "reboot": "PASS",
        "recovery": recovered,
        "query": query,
        "schema": _REBOOT_TICKET_SCHEMA,
        "ticket_sha256": ticket["ticket_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=(
            "activate",
            "activation-fault",
            "activation-reboot-prepare",
            "activation-reboot-resume",
            "cold-open-probe",
            "create",
            "fts5-activate",
            "fts5-cold-query",
            "inspect",
            "mutate",
            "recover",
            "recover-bounded",
            "recover-guard",
            "recover-ready-manifest-same",
            "recover-ready-manifest-different",
            "recover-ready-source-different",
            "recover-close",
            "recover-guard-reprove-close",
            "cold-open-private-tamper",
            "cold-open-narrow-failure",
            "retry",
            "replay",
        ),
    )
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--phase",
        choices=(
            *_PROCESS_TERMINATION_PHASES,
            "DB_ACTIVE",
            "DB_MUTATION_GUARD",
            "MANIFEST_PHYSICAL",
            "ACTIVE_SET_CLOSE",
            "APPLY_PROGRAMMER_CLOSE",
            "SYNCHRONIZED_CLOSE",
            "ACTIVE_REPROVE_PROGRAMMER_CLOSE",
        ),
    )
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--ticket", type=Path)
    parser.add_argument(
        "--mutation",
        choices=(
            "missing-manifest",
            "sealed-database",
            "stage-database-missing",
            "stage-database-changed",
            "source-changed",
            "wrong-manifest",
            "physical-manifest-ahead-sealed",
            "same-byte-database",
            "same-byte-stage-database",
            "same-byte-source",
            "foreign-database-replacement",
            "foreign-manifest-replacement",
            "stage-database-multilink",
            "stage-database-junction",
            "private-journal-foreign-acl",
            "private-generation-owner-envelope",
            "unknown-private",
        ),
    )
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    if arguments.mode not in {
        "activation-reboot-prepare",
        "activation-reboot-resume",
    }:
        root.mkdir(parents=True, exist_ok=True)
    try:
        if arguments.mode == "activation-fault":
            if arguments.phase is None or arguments.marker is None:
                parser.error("activation-fault requires --phase and --marker")
            result = _activation_process_fault(
                root,
                arguments.phase,
                arguments.marker.resolve(),
            )
        elif arguments.mode == "activation-reboot-prepare":
            if arguments.ticket is None:
                parser.error("activation-reboot-prepare requires --ticket")
            result = _reboot_prepare(root, arguments.ticket.resolve())
        elif arguments.mode == "activation-reboot-resume":
            if arguments.ticket is None:
                parser.error("activation-reboot-resume requires --ticket")
            result = _reboot_resume(root, arguments.ticket.resolve())
        elif arguments.mode == "cold-open-probe":
            result = _cold_open_probe(root)
        elif arguments.mode == "inspect":
            result = _inspect_process_state(root)
        elif arguments.mode == "create":
            if arguments.phase is None:
                parser.error("create requires --phase")
            result = _creator(root, arguments.phase)
        elif arguments.mode == "fts5-activate":
            result = _fts5_activate(root)
        elif arguments.mode == "fts5-cold-query":
            result = _fts5_cold_query(root)
        elif arguments.mode == "mutate":
            if arguments.mutation is None:
                parser.error("mutate requires --mutation")
            result = _mutate(root, arguments.mutation)
        elif arguments.mode in {
            "cold-open-private-tamper",
            "cold-open-narrow-failure",
        }:
            result = _cold_open_private_failure(
                root,
                fail_narrowing=(
                    arguments.mode == "cold-open-narrow-failure"
                ),
            )
        else:
            result = _activate(
                root,
                mode=arguments.mode,
                guarded=(
                    arguments.mode.startswith("recover")
                    or arguments.mode == "replay"
                ),
                reject_database_materialization=(
                    arguments.mode == "recover-bounded"
                ),
                probe_mutation_guard=arguments.mode == "recover-guard",
                ready_swap={
                    "recover-ready-manifest-same": "manifest-same",
                    "recover-ready-manifest-different": "manifest-different",
                    "recover-ready-source-different": "source-different",
                }.get(arguments.mode),
                fail_active_set_close=arguments.mode == "recover-close",
                fail_fresh_guard_reprove_close=(
                    arguments.mode == "recover-guard-reprove-close"
                ),
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
