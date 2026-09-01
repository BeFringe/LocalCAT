"""Independent-process Windows configured-snapshot recovery worker.

The worker exercises only public TM business entry points.  Test-only patches
park execution at exact production boundaries so the parent can use the real
Windows ``TerminateProcess`` API; they never manufacture SQLite/private owner
state or substitute a successful refresh/recovery result.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
from dataclasses import fields, is_dataclass
import hashlib
import json
import os
from pathlib import Path, PurePath
import sqlite3
import stat
import subprocess
import sys
import time
import traceback
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tm_migration
import tm_snapshot_artifacts
import tm_snapshot_recovery
from platform_fs_contracts import (
    BoundRegularFile,
    CandidateContentFacts,
    CandidateFile,
    PendingPublication,
)
from platform_fs_windows import WindowsPlatformAdapter
from tm_contracts import (
    CanonicalResourceIdentity,
    ExportFailure,
    ExportReport,
    MigrationFailure,
    MigrationReport,
    SnapshotManifest,
    SnapshotReceipt,
    SourceBindingState,
    contract_from_json,
    snapshot_receipt_digest,
)
from tm_engine import open_canonical_tm_store
from tm_migration import TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteTMStore
from tests.test_tm_portable_replacement_owner_windows import _INITIAL_SOURCE, _fixture
from tests.test_tm_snapshot_refresh import _draft


SCHEMA = "localcat.windows-tm-snapshot-recovery-worker.v1"
REBOOT_TICKET_SCHEMA = "localcat.windows-tm-snapshot-recovery-reboot-ticket.v1"
STORE_ID = "store.primary"
RESOURCE_ID = "tm.primary"
REFRESH_PREFIX = "snapshot.refresh."
RETIREMENT_ROOT_NAME = ".localcat-snapshot-refresh-retirement-v1"
ARTIFACT_ROLES = (
    "jsonl_temp",
    "manifest_temp",
    "jsonl_recovery",
    "manifest_recovery",
)
EXISTING_CANDIDATE_RECOVERY_PHASES = (
    "existing_candidate_recovery_before_restore",
    "existing_candidate_recovery_after_restore",
    "existing_candidate_recovery_before_terminal",
)

ADAPTER_PUBLISH_EVENTS = (
    "publish_before_rename",
    "publish_after_rename",
    "publish_after_final_facts",
    "publish_before_destination_reopen",
    "publish_after_destination_reopen",
)

PUBLICATION_PHASES = (
    *(f"candidate_flush.{edge}.{ordinal}" for ordinal in range(1, 3) for edge in ("before", "after")),
    "issued.before",
    "issued.after",
    *(
        f"adapter.{event}.{ordinal}"
        for event in ADAPTER_PUBLISH_EVENTS
        for ordinal in range(1, 5)
    ),
    *(f"retained_readback.{edge}.{ordinal}" for ordinal in range(1, 4) for edge in ("before", "after")),
    "owner_completion.before",
    "owner_completion.after",
    "business_reproof.before",
    "business_reproof.after",
    *(f"terminal_reproof.{edge}.{ordinal}" for ordinal in range(1, 5) for edge in ("before", "after")),
    *(f"close.{edge}.{ordinal}" for ordinal in range(1, 5) for edge in ("before", "after")),
)

RECOVERY_PHASES = (
    "classified",
    "effect_before",
    "effect_after",
    "retire_before.jsonl_temp",
    "retire_after.jsonl_temp",
    "retire_before.manifest_temp",
    "retire_after.manifest_temp",
    "retire_before.jsonl_recovery",
    "retire_after.jsonl_recovery",
    "retire_before.manifest_recovery",
    "retire_after.manifest_recovery",
    "terminal_cleanup",
)

RECONSTRUCTION_PHASES = (
    *(f"adapter.{event}.1" for event in ADAPTER_PUBLISH_EVENTS),
    "terminal_reproof.before.1",
    "terminal_reproof.after.1",
    "close.before.1",
    "close.after.1",
)


def _identity(root: Path) -> CanonicalResourceIdentity:
    source = (root / "tm.primary.jsonl").resolve()
    return CanonicalResourceIdentity.from_configured_jsonl(RESOURCE_ID, source)


def _owner(
    identity: CanonicalResourceIdentity,
) -> tuple[ResourceStoreCoordinator, TMMigrationService]:
    coordinator = ResourceStoreCoordinator(
        canonical_store_id=STORE_ID,
        resource_identity=identity,
    )
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id=STORE_ID,
        coordinator=coordinator,
    )
    return coordinator, service


def _json_value(value: object) -> object:
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, Path):
        return str(value)
    enum_value = getattr(value, "value", None)
    if type(enum_value) in {bool, int, float, str}:
        return enum_value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return repr(value)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _test_native_path(path: Path) -> str:
    text = str(path)
    return f"\\\\?\\{text}" if os.name == "nt" else text


def _file_facts(path: Path) -> dict[str, object] | None:
    try:
        observed = os.lstat(_test_native_path(path))
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(observed.st_mode):
        return {"kind": "unsafe", "name": path.name}
    with open(_test_native_path(path), "rb") as stream:
        payload = stream.read()
    return {
        "name": path.name,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _outcome_facts(outcome: object) -> dict[str, object]:
    result: dict[str, object] = {"kind": type(outcome).__name__}
    if type(outcome) in {MigrationReport, MigrationFailure, ExportReport, ExportFailure}:
        result["value"] = _json_value(outcome)
        return result
    state = getattr(outcome, "state", None)
    if state is not None:
        result["state"] = getattr(state, "value", str(state))
    diagnostics = getattr(outcome, "diagnostics", None)
    if diagnostics is not None:
        result["diagnostics"] = _json_value(diagnostics)
    receipts = getattr(outcome, "receipts", None)
    if receipts is not None:
        result["receipts"] = _json_value(receipts)
    error_code = getattr(outcome, "error_code", None)
    if error_code is not None:
        result["error_code"] = _json_value(error_code)
    retryable = getattr(outcome, "retryable", None)
    if retryable is not None:
        result["retryable"] = _json_value(retryable)
    return result


def _database_facts(identity: CanonicalResourceIdentity) -> dict[str, object]:
    connection = sqlite3.connect(str(identity.canonical_sidecar_path))
    try:
        refresh_rows = connection.execute(
            "SELECT snapshot_id, exported_revision, jsonl_digest, record_count, "
            "status, destination_jsonl_path, destination_manifest_path, "
            "resource_id, canonical_store_id, format_version "
            "FROM tm_snapshot_receipt WHERE snapshot_id LIKE ? "
            "ORDER BY created_at, snapshot_id",
            (f"{REFRESH_PREFIX}%",),
        ).fetchall()
        binding = connection.execute(
            "SELECT configured_jsonl_path, manifest_path, snapshot_kind, "
            "snapshot_id FROM tm_snapshot_binding"
        ).fetchone()
        handoff_rows = connection.execute(
            "SELECT key, value FROM tm_meta "
            "WHERE key LIKE 'bound_refresh_handoff.%' ORDER BY key"
        ).fetchall()
        handoffs: list[dict[str, object]] = []
        for key, value in handoff_rows:
            try:
                decoded: object = json.loads(str(value))
            except json.JSONDecodeError:
                decoded = {"invalid_json": str(value)}
            handoffs.append({"key": str(key), "value": _json_value(decoded)})
        legacy_handoff_rows = connection.execute(
            "SELECT key, value FROM tm_meta "
            "WHERE key LIKE 'artifact_handoff.%' ORDER BY key"
        ).fetchall()
        legacy_handoffs: list[dict[str, object]] = []
        for key, value in legacy_handoff_rows:
            try:
                decoded = json.loads(str(value))
            except json.JSONDecodeError:
                decoded = {"invalid_json": str(value)}
            legacy_handoffs.append(
                {"key": str(key), "value": _json_value(decoded)}
            )
        record_rows = connection.execute(
            "SELECT * FROM tm_record ORDER BY record_id"
        ).fetchall()
        return {
            "refresh_rows": _json_value(refresh_rows),
            "binding": _json_value(binding),
            "generation": int(
                connection.execute(
                    "SELECT value FROM tm_meta WHERE key = 'generation'"
                ).fetchone()[0]
            ),
            "head_revision": int(
                connection.execute(
                    "SELECT value FROM tm_meta WHERE key = 'head_revision'"
                ).fetchone()[0]
            ),
            "canonical_record_count": len(record_rows),
            "canonical_rows_digest": _canonical_digest(record_rows),
            "divergence_latched": connection.execute(
                "SELECT value FROM tm_meta WHERE key = 'divergence_latched'"
            ).fetchone()[0],
            "bound_refresh_handoffs": handoffs,
            "artifact_handoffs": legacy_handoffs,
        }
    finally:
        connection.close()


def _artifact_facts(identity: CanonicalResourceIdentity) -> dict[str, object]:
    paths = tm_migration._export_artifact_paths(identity.configured_jsonl_path)
    artifacts = {
        "jsonl_temp": paths.jsonl_temp,
        "manifest_temp": paths.manifest_temp,
        "jsonl_recovery": paths.jsonl_recovery,
        "manifest_recovery": paths.manifest_recovery,
    }
    return {role: _file_facts(path) for role, path in artifacts.items()}


def _retirement_facts(root: Path) -> dict[str, object]:
    retirement_root = root / RETIREMENT_ROOT_NAME
    if not retirement_root.exists():
        return {"root": None, "receipts": {}}
    receipts: dict[str, object] = {}
    for receipt_directory in sorted(
        retirement_root.iterdir(),
        key=lambda item: item.name,
    ):
        receipt_stat = os.lstat(_test_native_path(receipt_directory))
        if not stat.S_ISDIR(receipt_stat.st_mode):
            receipts[receipt_directory.name] = {
                "kind": "unsafe",
                "entries": None,
            }
            continue
        entries: dict[str, object] = {}
        for entry in sorted(receipt_directory.iterdir(), key=lambda item: item.name):
            entry_stat = os.lstat(_test_native_path(entry))
            if stat.S_ISREG(entry_stat.st_mode):
                entries[entry.name] = _file_facts(entry)
            else:
                entries[entry.name] = {"kind": "unsafe"}
        receipts[receipt_directory.name] = {
            "kind": "directory",
            "entries": entries,
        }
    return {
        "root": retirement_root.name,
        "receipts": receipts,
    }


def _receipt_from_row(row: list[object] | tuple[object, ...]) -> SnapshotReceipt:
    if len(row) != 10:
        raise AssertionError(f"unexpected refresh receipt row: {row!r}")
    return SnapshotReceipt(
        snapshot_id=str(row[0]),
        exported_revision=int(row[1]),
        jsonl_digest=str(row[2]),
        record_count=int(row[3]),
        resource_id=str(row[7]),
        canonical_store_id=str(row[8]),
        format_version=str(row[9]),
    )


def _retirement_leaf(receipt: SnapshotReceipt) -> str:
    return snapshot_receipt_digest(receipt)


def _snapshot_facts(
    identity: CanonicalResourceIdentity,
    store: SQLiteTMStore,
    *,
    observation: object | None = None,
) -> dict[str, object]:
    # Read durable owner facts before projecting the source monitor.  The
    # caller must pass the exact observation it intentionally performed;
    # this helper never lets monitor recovery manufacture a green latch.
    database = _database_facts(identity)
    canonical = store.capture_export_snapshot()
    source_state = None
    source_diagnostics: object = None
    if observation is not None:
        state = getattr(observation, "state", None)
        source_state = getattr(state, "value", str(state))
        source_diagnostics = _json_value(
            getattr(observation, "diagnostic_codes", ())
        )
    manifest: object = None
    if identity.snapshot_manifest_path.is_file():
        try:
            manifest = contract_from_json(
                identity.snapshot_manifest_path.read_text(encoding="utf-8")
            )
        except BaseException as error:
            manifest = f"ERROR:{type(error).__name__}:{error}"
    return {
        "canonical_digest": _canonical_digest(canonical),
        "canonical": _json_value(canonical),
        "generation": canonical.revision.generation,
        "head_revision": canonical.revision.head_revision,
        "record_count": canonical.revision.record_count,
        "source_state": source_state,
        "source_diagnostics": source_diagnostics,
        "jsonl": _file_facts(identity.configured_jsonl_path),
        "manifest": _file_facts(identity.snapshot_manifest_path),
        "manifest_contract": _json_value(manifest),
        "database": database,
        "artifacts": _artifact_facts(identity),
        "retirement": _retirement_facts(identity.configured_jsonl_path.parent),
    }


def _raw_snapshot_facts(identity: CanonicalResourceIdentity) -> dict[str, object]:
    """Read durable pair/ledger facts without running startup or recovery."""

    manifest: object = None
    if identity.snapshot_manifest_path.is_file():
        try:
            manifest = contract_from_json(
                identity.snapshot_manifest_path.read_text(encoding="utf-8")
            )
        except BaseException as error:
            manifest = f"ERROR:{type(error).__name__}:{error}"
    return {
        "jsonl": _file_facts(identity.configured_jsonl_path),
        "manifest": _file_facts(identity.snapshot_manifest_path),
        "manifest_contract": _json_value(manifest),
        "database": _database_facts(identity),
        "artifacts": _artifact_facts(identity),
        "retirement": _retirement_facts(identity.configured_jsonl_path.parent),
    }


def _blocked_snapshot_facts(raw: dict[str, object]) -> dict[str, object]:
    """Project exact durable facts when public cold-open correctly fails closed."""

    database = raw["database"]
    if type(database) is not dict:
        raise AssertionError("blocked cold-open database facts are unavailable")
    return {
        "canonical_digest": None,
        "canonical": None,
        "generation": database["generation"],
        "head_revision": database["head_revision"],
        "record_count": database["canonical_record_count"],
        "source_state": None,
        "source_diagnostics": None,
        "jsonl": raw["jsonl"],
        "manifest": raw["manifest"],
        "manifest_contract": raw["manifest_contract"],
        "database": database,
        "artifacts": raw["artifacts"],
        "retirement": raw["retirement"],
    }


def _write_marker(marker: Path, phase: str, details: object = None) -> None:
    payload = {
        "details": _json_value(details),
        "phase": phase,
        "pid": os.getpid(),
        "schema": SCHEMA,
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    while True:
        time.sleep(30.0)


def _prepare(root: Path) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=False)
    identity, coordinator, service = _fixture(root)
    replacement = service.import_snapshot(
        identity.configured_jsonl_path,
        identity.resource_id,
    )
    if type(replacement) is not MigrationReport or replacement.activated_generation != 1:
        raise AssertionError(f"generation-one replacement failed: {replacement!r}")
    store = SQLiteTMStore.from_coordinator(coordinator)
    store.append(_draft("history-before-refresh", "generation-one-history"))
    observation = store.source_binding_monitor.observe()
    if observation.state is not SourceBindingState.VERIFIED_HISTORY:
        raise AssertionError("prepared generation-one store is not VERIFIED_HISTORY")
    return {
        "mode": "prepare",
        "replacement": _outcome_facts(replacement),
        "snapshot": _snapshot_facts(identity, store, observation=observation),
    }


def _prepare_generation_zero(root: Path) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=False)
    identity, coordinator, _service = _fixture(root)
    identity.configured_jsonl_path.write_bytes(_INITIAL_SOURCE)
    store = SQLiteTMStore.from_coordinator(coordinator)
    observation = store.source_binding_monitor.observe()
    if coordinator.current_generation != 0:
        raise AssertionError("initial activation did not remain at generation zero")
    return {
        "mode": "prepare-gen0",
        "snapshot": _snapshot_facts(identity, store, observation=observation),
    }


def _cold_open(
    root: Path,
    *,
    platform_backend: WindowsPlatformAdapter | None = None,
) -> tuple[
    CanonicalResourceIdentity,
    ResourceStoreCoordinator,
    TMMigrationService,
    SQLiteTMStore,
    object,
]:
    identity = _identity(root)
    store = open_canonical_tm_store(
        identity.configured_jsonl_path,
        expected_resource_id=identity.resource_id,
    )
    if store is None:
        raise AssertionError("public canonical cold open fell back to legacy")
    coordinator = store.coordinator
    if coordinator.state != "READY" or coordinator.current_generation is None:
        raise AssertionError("public canonical cold open did not recover READY")
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id=coordinator.canonical_store_id,
        coordinator=coordinator,
        platform_backend=platform_backend,
    )
    opening = {
        "api": "open_canonical_tm_store",
        "canonical_store_id": coordinator.canonical_store_id,
        "generation": coordinator.current_generation,
    }
    return identity, coordinator, service, store, opening


def _install_publication_fault(
    stack: ExitStack,
    selected: str,
    marker: Path,
    *,
    advance_head_after_issued: bool = False,
) -> WindowsPlatformAdapter:
    if selected not in PUBLICATION_PHASES:
        raise ValueError("unknown publication phase")
    counts = {
        "flush": 0,
        "readback": 0,
        "terminal": 0,
        "close": 0,
    }
    adapter_counts = {event: 0 for event in ADAPTER_PUBLISH_EVENTS}
    state = {"issued": False, "owner_committed": False, "business_seen": False}

    def adapter_fault(event: str) -> None:
        if event not in adapter_counts:
            return
        adapter_counts[event] += 1
        phase = f"adapter.{event}.{adapter_counts[event]}"
        if selected == phase:
            _write_marker(marker, phase, {"ordinal": adapter_counts[event]})

    backend = WindowsPlatformAdapter(_fault_injector=adapter_fault)

    real_flush = CandidateFile.flush_content

    def flush(candidate: CandidateFile) -> object:
        counts["flush"] += 1
        ordinal = counts["flush"]
        before = f"candidate_flush.before.{ordinal}"
        after = f"candidate_flush.after.{ordinal}"
        if selected == before:
            _write_marker(marker, before, {"ordinal": ordinal})
        result = real_flush(candidate)
        if selected == after:
            _write_marker(marker, after, {"ordinal": ordinal})
        return result

    stack.enter_context(mock.patch.object(CandidateFile, "flush_content", new=flush))

    real_register = SQLiteTMStore.register_issued_refresh_receipt

    def register(store: SQLiteTMStore, receipt: object, **kwargs: object) -> None:
        if selected == "issued.before":
            _write_marker(
                marker,
                selected,
                {"snapshot_id": getattr(receipt, "snapshot_id", None)},
            )
        real_register(store, receipt, **kwargs)
        state["issued"] = True
        if advance_head_after_issued:
            store.append(
                _draft(
                    "history-after-issued",
                    "issued-receipt-became-history",
                )
            )
        if selected == "issued.after":
            _write_marker(
                marker,
                selected,
                {
                    "head_advanced": advance_head_after_issued,
                    "snapshot_id": receipt.snapshot_id,
                },
            )

    stack.enter_context(
        mock.patch.object(SQLiteTMStore, "register_issued_refresh_receipt", new=register)
    )

    real_read_all = BoundRegularFile.read_all

    def read_all(authority: BoundRegularFile) -> bytes:
        if not state["issued"]:
            return real_read_all(authority)
        counts["readback"] += 1
        ordinal = counts["readback"]
        before = f"retained_readback.before.{ordinal}"
        after = f"retained_readback.after.{ordinal}"
        if selected == before:
            _write_marker(marker, before, {"ordinal": ordinal})
        payload = real_read_all(authority)
        if selected == after:
            _write_marker(marker, after, {"ordinal": ordinal})
        return payload

    stack.enter_context(mock.patch.object(BoundRegularFile, "read_all", new=read_all))

    real_complete = SQLiteTMStore.complete_bound_issued_refresh_receipt

    def complete(store: SQLiteTMStore, snapshot_id: str, **kwargs: object) -> None:
        if selected == "owner_completion.before":
            _write_marker(marker, selected, {"snapshot_id": snapshot_id})
        real_complete(store, snapshot_id, **kwargs)
        state["owner_committed"] = True
        if selected == "owner_completion.after":
            _write_marker(marker, selected, {"snapshot_id": snapshot_id})

    stack.enter_context(
        mock.patch.object(
            SQLiteTMStore,
            "complete_bound_issued_refresh_receipt",
            new=complete,
        )
    )

    real_owner_reprove = tm_migration._InitialActivationResourceReservation.reprove_bound_refresh_owner

    def owner_reprove(owner: object, *args: object, **kwargs: object) -> None:
        if state["owner_committed"] and not state["business_seen"]:
            state["business_seen"] = True
            if selected == "business_reproof.before":
                _write_marker(marker, selected)
        real_owner_reprove(owner, *args, **kwargs)
        if state["business_seen"] and selected == "business_reproof.after":
            _write_marker(marker, selected)

    stack.enter_context(
        mock.patch.object(
            tm_migration._InitialActivationResourceReservation,
            "reprove_bound_refresh_owner",
            new=owner_reprove,
        )
    )

    real_terminal = PendingPublication.terminal_reproof

    def terminal(pending: PendingPublication) -> object:
        counts["terminal"] += 1
        ordinal = counts["terminal"]
        before = f"terminal_reproof.before.{ordinal}"
        after = f"terminal_reproof.after.{ordinal}"
        if selected == before:
            _write_marker(marker, before, {"ordinal": ordinal})
        facts = real_terminal(pending)
        if selected == after:
            _write_marker(marker, after, {"ordinal": ordinal})
        return facts

    stack.enter_context(
        mock.patch.object(PendingPublication, "terminal_reproof", new=terminal)
    )

    real_pending_close = PendingPublication.close

    def pending_close(pending: PendingPublication) -> None:
        counts["close"] += 1
        ordinal = counts["close"]
        before = f"close.before.{ordinal}"
        after = f"close.after.{ordinal}"
        if selected == before:
            _write_marker(marker, before, {"ordinal": ordinal})
        real_pending_close(pending)
        if selected == after:
            _write_marker(marker, after, {"ordinal": ordinal})

    stack.enter_context(
        mock.patch.object(PendingPublication, "close", new=pending_close)
    )
    return backend


def _refresh_fault(
    root: Path,
    phase: str,
    marker: Path,
    *,
    advance_head_after_issued: bool = False,
) -> dict[str, object]:
    with ExitStack() as stack:
        backend = _install_publication_fault(
            stack,
            phase,
            marker,
            advance_head_after_issued=advance_head_after_issued,
        )
        identity, _coordinator, service, store, opening = _cold_open(
            root,
            platform_backend=backend,
        )
        before = _snapshot_facts(identity, store)
        outcome = service.refresh_configured_snapshot(store)
    raise AssertionError(
        f"selected publication phase was not reached: {phase}; outcome={outcome!r}; "
        f"opening={opening!r}; before={before!r}"
    )


def _refresh(root: Path) -> dict[str, object]:
    identity, _coordinator, service, store, opening = _cold_open(root)
    before = _snapshot_facts(identity, store)
    outcome = service.refresh_configured_snapshot(store)
    observation = store.source_binding_monitor.observe()
    return {
        "mode": "refresh",
        "opening": opening,
        "outcome": _outcome_facts(outcome),
        "before": before,
        "snapshot": _snapshot_facts(identity, store, observation=observation),
    }


def _recovery_hook_target(phase: str) -> tuple[object, str, str | None]:
    if phase == "classified":
        return tm_snapshot_recovery, "_after_bound_refresh_recovery_classified", None
    if phase == "effect_before":
        return tm_snapshot_recovery, "_before_bound_refresh_recovery_effect", None
    if phase == "effect_after":
        return tm_snapshot_recovery, "_after_bound_refresh_recovery_effect", None
    if phase == "terminal_cleanup":
        return tm_migration, "_after_bound_refresh_terminal_cleanup", None
    prefix, separator, role = phase.partition(".")
    if not separator or role not in {
        "jsonl_temp",
        "manifest_temp",
        "jsonl_recovery",
        "manifest_recovery",
    }:
        raise ValueError("unknown recovery phase")
    if prefix == "retire_before":
        return tm_migration, "_before_bound_refresh_retire_role", role
    if prefix == "retire_after":
        return tm_migration, "_after_bound_refresh_retire_role", role
    raise ValueError("unknown recovery phase")


def _install_recovery_fault(
    stack: ExitStack,
    selected: str,
    marker: Path,
) -> None:
    if selected not in RECOVERY_PHASES:
        raise ValueError("unknown recovery phase")
    module, name, selected_role = _recovery_hook_target(selected)
    if not hasattr(module, name):
        raise AssertionError(f"production recovery fault seam is unavailable: {name}")
    original = getattr(module, name)

    def hook(*args: object, **kwargs: object) -> object:
        role = kwargs.get("role")
        if role is None and args:
            for value in reversed(args):
                if type(value) is str and value in {
                    "jsonl_temp",
                    "manifest_temp",
                    "jsonl_recovery",
                    "manifest_recovery",
                }:
                    role = value
                    break
        result = original(*args, **kwargs)
        if selected_role is None or role == selected_role:
            _write_marker(marker, selected, {"args": args, "kwargs": kwargs})
        return result

    stack.enter_context(mock.patch.object(module, name, new=hook))


def _install_reconstruction_fault(
    stack: ExitStack,
    selected: str,
    marker: Path,
) -> WindowsPlatformAdapter:
    if selected not in RECONSTRUCTION_PHASES:
        raise ValueError("unknown manifest reconstruction phase")
    adapter_counts = {event: 0 for event in ADAPTER_PUBLISH_EVENTS}
    counts = {"terminal": 0, "close": 0}

    def adapter_fault(event: str) -> None:
        if event not in adapter_counts:
            return
        adapter_counts[event] += 1
        phase = f"adapter.{event}.{adapter_counts[event]}"
        if selected == phase:
            _write_marker(marker, phase, {"ordinal": adapter_counts[event]})

    backend = WindowsPlatformAdapter(_fault_injector=adapter_fault)
    stack.enter_context(
        mock.patch.object(
            tm_migration,
            "compose_platform_file_backend",
            return_value=backend,
        )
    )

    real_terminal = PendingPublication.terminal_reproof

    def terminal(pending: PendingPublication) -> object:
        counts["terminal"] += 1
        ordinal = counts["terminal"]
        before = f"terminal_reproof.before.{ordinal}"
        after = f"terminal_reproof.after.{ordinal}"
        if selected == before:
            _write_marker(marker, before, {"ordinal": ordinal})
        facts = real_terminal(pending)
        if selected == after:
            _write_marker(marker, after, {"ordinal": ordinal})
        return facts

    stack.enter_context(
        mock.patch.object(PendingPublication, "terminal_reproof", new=terminal)
    )
    real_close = PendingPublication.close

    def close(pending: PendingPublication) -> None:
        counts["close"] += 1
        ordinal = counts["close"]
        before = f"close.before.{ordinal}"
        after = f"close.after.{ordinal}"
        if selected == before:
            _write_marker(marker, before, {"ordinal": ordinal})
        real_close(pending)
        if selected == after:
            _write_marker(marker, after, {"ordinal": ordinal})

    stack.enter_context(mock.patch.object(PendingPublication, "close", new=close))
    return backend


def _install_existing_candidate_recovery_fault(
    stack: ExitStack,
    selected: str,
    marker: Path,
) -> WindowsPlatformAdapter:
    if selected not in EXISTING_CANDIDATE_RECOVERY_PHASES:
        raise ValueError("unknown existing-candidate recovery phase")

    def fault(event: str) -> None:
        if event == selected:
            _write_marker(marker, selected)

    backend = WindowsPlatformAdapter(_fault_injector=fault)
    stack.enter_context(
        mock.patch.object(
            tm_migration,
            "compose_platform_file_backend",
            return_value=backend,
        )
    )
    return backend


def _mutate_bound_owner_race(
    identity: CanonicalResourceIdentity,
    *,
    action: str,
    mutation: str,
) -> dict[str, object]:
    connection = sqlite3.connect(str(identity.canonical_sidecar_path))
    try:
        row = connection.execute(
            "SELECT key, value FROM tm_meta "
            "WHERE key LIKE 'bound_refresh_handoff.%' ORDER BY key"
        ).fetchall()
        if len(row) != 1:
            raise AssertionError("owner race requires one bound handoff")
        key, encoded = str(row[0][0]), str(row[0][1])
        payload = json.loads(encoded)
        if type(payload) is not dict:
            raise AssertionError("owner race handoff is not an object")
        snapshot_id = str(payload["snapshot_id"])
        replacement_key = key
        if mutation == "receipt":
            updated = connection.execute(
                "UPDATE tm_snapshot_receipt SET record_count=record_count+1 "
                "WHERE snapshot_id=?",
                (snapshot_id,),
            )
            if updated.rowcount != 1:
                raise AssertionError("owner race receipt is missing")
        elif mutation == "second-receipt":
            columns = tuple(
                str(item[1])
                for item in connection.execute(
                    "PRAGMA table_info(tm_snapshot_receipt)"
                ).fetchall()
            )
            receipt_rows = connection.execute(
                "SELECT * FROM tm_snapshot_receipt WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchall()
            if len(receipt_rows) != 1 or "snapshot_id" not in columns:
                raise AssertionError("owner race source receipt is ambiguous")
            duplicate = list(receipt_rows[0])
            duplicate[columns.index("snapshot_id")] = f"{snapshot_id}.second"
            duplicate[columns.index("status")] = "issued"
            connection.execute(
                f"INSERT INTO tm_snapshot_receipt ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(duplicate),
            )
        elif mutation == "prior":
            payload["prior_snapshot_id"] = "snapshot.race.prior"
        elif mutation == "id":
            raced_id = f"{snapshot_id}.race"
            payload["snapshot_id"] = raced_id
            replacement_key = tm_snapshot_recovery._bound_refresh_handoff_meta_key(
                raced_id
            )
        elif mutation == "kind":
            payload["prior_snapshot_kind"] = (
                "EXPLICIT_EXPORT"
                if payload["prior_snapshot_kind"] != "EXPLICIT_EXPORT"
                else "MIGRATION_SOURCE"
            )
        elif mutation == "digest":
            payload["prior_jsonl_digest"] = (
                "0" * 64
                if payload["prior_jsonl_digest"] != "0" * 64
                else "1" * 64
            )
        elif mutation == "whole":
            payload.update(
                {
                    "prior_snapshot_id": "snapshot.race.whole",
                    "prior_snapshot_kind": "EXPLICIT_EXPORT",
                    "prior_jsonl_digest": "2" * 64,
                    "prior_manifest_digest": "3" * 64,
                }
            )
        else:
            raise ValueError("unknown owner race mutation")
        if mutation not in {"receipt", "second-receipt"}:
            replacement = json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            if replacement_key != key:
                connection.execute("DELETE FROM tm_meta WHERE key=?", (key,))
                connection.execute(
                    "INSERT INTO tm_meta(key, value) VALUES (?, ?)",
                    (replacement_key, replacement),
                )
            else:
                connection.execute(
                    "UPDATE tm_meta SET value=? WHERE key=?",
                    (replacement, key),
                )
        connection.commit()
    finally:
        connection.close()
    return {
        "action": action,
        "mutation": mutation,
        "snapshot": _raw_snapshot_facts(identity),
    }


def _configured_legacy_handoff_ambiguity(root: Path) -> dict[str, object]:
    """Add one valid legacy owner for the configured bound destination."""

    identity = _identity(root)
    paths = tm_migration._export_artifact_paths(identity.configured_jsonl_path)

    def file_identity(path: Path) -> tuple[int, int] | None:
        try:
            observed = os.lstat(_test_native_path(path))
        except FileNotFoundError:
            return None
        return (int(observed.st_dev), int(observed.st_ino))

    parent_identity = file_identity(identity.configured_jsonl_path.parent)
    configured_jsonl = _file_facts(identity.configured_jsonl_path)
    configured_manifest = _file_facts(identity.snapshot_manifest_path)
    if (
        parent_identity is None
        or configured_jsonl is None
        or configured_manifest is None
    ):
        raise AssertionError("configured legacy handoff requires a proven pair")

    connection = sqlite3.connect(str(identity.canonical_sidecar_path))
    try:
        bound_rows = connection.execute(
            "SELECT key FROM tm_meta "
            "WHERE key LIKE 'bound_refresh_handoff.%' ORDER BY key"
        ).fetchall()
        if len(bound_rows) != 1:
            raise AssertionError("configured ambiguity requires one bound handoff")
        source_snapshot_id = str(bound_rows[0][0]).removeprefix(
            "bound_refresh_handoff."
        )
        columns = tuple(
            str(item[1])
            for item in connection.execute(
                "PRAGMA table_info(tm_snapshot_receipt)"
            ).fetchall()
        )
        receipt_rows = connection.execute(
            "SELECT * FROM tm_snapshot_receipt WHERE snapshot_id=? "
            "AND destination_jsonl_path=? AND destination_manifest_path=?",
            (
                source_snapshot_id,
                str(identity.configured_jsonl_path),
                str(identity.snapshot_manifest_path),
            ),
        ).fetchall()
        if len(receipt_rows) != 1 or "snapshot_id" not in columns:
            raise AssertionError("configured issued receipt is ambiguous")
        duplicate = list(receipt_rows[0])
        legacy_snapshot_id = f"{source_snapshot_id}.legacy"
        duplicate[columns.index("snapshot_id")] = legacy_snapshot_id
        duplicate[columns.index("status")] = "completed"
        connection.execute(
            f"INSERT INTO tm_snapshot_receipt ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(duplicate),
        )
        handoff_value = tm_snapshot_artifacts._artifact_handoff_meta_value(
            artifact_parent_identity=parent_identity,
            jsonl_temp_identity=file_identity(paths.jsonl_temp),
            manifest_temp_identity=file_identity(paths.manifest_temp),
            jsonl_recovery_identity=file_identity(paths.jsonl_recovery),
            manifest_recovery_identity=file_identity(paths.manifest_recovery),
            prior_jsonl_identity=file_identity(identity.configured_jsonl_path),
            prior_jsonl_digest=str(configured_jsonl["sha256"]),
            prior_jsonl_absent=False,
            prior_manifest_identity=file_identity(identity.snapshot_manifest_path),
            prior_manifest_digest=str(configured_manifest["sha256"]),
            prior_manifest_absent=False,
        )
        handoff_key = tm_snapshot_artifacts._artifact_handoff_meta_key(
            legacy_snapshot_id
        )
        if (
            tm_snapshot_artifacts._artifact_handoff_from_meta(
                handoff_key, handoff_value
            )
            is None
        ):
            raise AssertionError("configured legacy handoff is not strict")
        connection.execute(
            "INSERT INTO tm_meta(key, value) VALUES (?, ?)",
            (handoff_key, handoff_value),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "mode": "configured-legacy-handoff-ambiguity",
        "bound_snapshot_id": source_snapshot_id,
        "legacy_snapshot_id": legacy_snapshot_id,
        "legacy_handoff_key": handoff_key,
        "snapshot": _raw_snapshot_facts(identity),
    }


def _install_bound_owner_race(
    stack: ExitStack,
    identity: CanonicalResourceIdentity,
    *,
    action: str,
    mutation: str,
    captured: list[dict[str, object]],
) -> None:
    module = tm_snapshot_recovery
    name = "_before_bound_refresh_recovery_effect"
    if not hasattr(module, name):
        raise AssertionError(f"owner race seam is unavailable: {name}")
    original = getattr(module, name)

    def hook(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)
        if captured:
            return result
        observed_action = args[1] if len(args) > 1 else kwargs.get("action")
        if observed_action != action:
            return result
        captured.append(
            _mutate_bound_owner_race(
                identity,
                action=action,
                mutation=mutation,
            )
        )
        return result

    stack.enter_context(mock.patch.object(module, name, new=hook))


def _recover(
    root: Path,
    *,
    phase: str | None = None,
    marker: Path | None = None,
    race_pair_directory: Path | None = None,
    observe_after: bool = True,
    reconstruction_phase: str | None = None,
    existing_candidate_phase: str | None = None,
    owner_race_action: str | None = None,
    owner_race_mutation: str | None = None,
) -> dict[str, object]:
    identity = _identity(root)
    durable_before_open = _raw_snapshot_facts(identity)
    captured: list[object] = []
    owner_race: list[dict[str, object]] = []
    real_public_recovery = TMMigrationService.recover_configured_refresh

    def capture_public_recovery(
        service: TMMigrationService,
        store: SQLiteTMStore,
    ) -> object:
        outcome = real_public_recovery(service, store)
        captured.append(outcome)
        return outcome

    with ExitStack() as stack:
        if phase is not None:
            if marker is None:
                raise ValueError("recovery fault requires marker")
            _install_recovery_fault(stack, phase, marker)
        if reconstruction_phase is not None:
            if marker is None:
                raise ValueError("reconstruction fault requires marker")
            _install_reconstruction_fault(stack, reconstruction_phase, marker)
        if existing_candidate_phase is not None:
            if marker is None:
                raise ValueError("existing candidate fault requires marker")
            _install_existing_candidate_recovery_fault(
                stack, existing_candidate_phase, marker
            )
        if owner_race_action is not None:
            if owner_race_mutation is None:
                raise ValueError("owner race mutation is required")
            _install_bound_owner_race(
                stack,
                identity,
                action=owner_race_action,
                mutation=owner_race_mutation,
                captured=owner_race,
            )
        if race_pair_directory is not None:
            hook_name = "_before_bound_refresh_recovery_effect"
            if not hasattr(tm_snapshot_recovery, hook_name):
                raise AssertionError(
                    f"production recovery race seam is unavailable: {hook_name}"
                )
            original_hook = getattr(tm_snapshot_recovery, hook_name)
            raced = False

            def replace_pair(*args: object, **kwargs: object) -> object:
                nonlocal raced
                result = original_hook(*args, **kwargs)
                if not raced:
                    raced = True
                    identity.configured_jsonl_path.write_bytes(
                        (race_pair_directory / "prior.jsonl").read_bytes()
                    )
                    identity.snapshot_manifest_path.write_bytes(
                        (race_pair_directory / "prior.manifest.json").read_bytes()
                    )
                return result

            stack.enter_context(
                mock.patch.object(
                    tm_snapshot_recovery,
                    hook_name,
                    new=replace_pair,
                )
            )
        stack.enter_context(
            mock.patch.object(
                TMMigrationService,
                "recover_configured_refresh",
                new=capture_public_recovery,
            )
        )
        try:
            identity, _coordinator, service, store, opening = _cold_open(root)
        except ValueError as opening_error:
            if len(captured) != 1:
                raise
            first = captured[0]
            if getattr(getattr(first, "state", None), "value", None) != "BLOCKED":
                raise
            durable_after_first = _raw_snapshot_facts(identity)
            second: object | None = None
            durable_after_second: dict[str, object] | None = None
            if observe_after:
                try:
                    _cold_open(root)
                except ValueError:
                    if len(captured) != 2:
                        raise
                    second = captured[1]
                    if (
                        getattr(getattr(second, "state", None), "value", None)
                        != "BLOCKED"
                    ):
                        raise
                    durable_after_second = _raw_snapshot_facts(identity)
                else:
                    raise AssertionError(
                        "second public cold-open unexpectedly exposed a store"
                    )
            if owner_race_action is not None and len(owner_race) != 1:
                raise AssertionError("owner race seam did not run exactly once")
            return {
                "mode": "recover" if phase is None else "recover-fault",
                "opening": {
                    "api": "open_canonical_tm_store",
                    "error": str(opening_error),
                },
                "durable_before_open": durable_before_open,
                "durable_after_open": durable_after_first,
                "durable_after_observation": durable_after_second,
                "first_recovery": _outcome_facts(first),
                "second_recovery": (
                    None if second is None else _outcome_facts(second)
                ),
                "first_observation": None,
                "owner_race": owner_race[0] if owner_race else None,
                "snapshot": _blocked_snapshot_facts(
                    durable_after_first
                    if durable_after_second is None
                    else durable_after_second
                ),
            }
    if len(captured) != 1:
        raise AssertionError(
            "open_canonical_tm_store must run exactly one bound refresh "
            f"recovery before returning; observed={captured!r}"
        )
    first = captured[0]
    durable_after_open = _raw_snapshot_facts(identity)
    if observe_after:
        observation = store.source_binding_monitor.observe()
        durable_after_observation = _raw_snapshot_facts(identity)
        second: object | None = service.recover_configured_refresh(store)
    else:
        observation = None
        durable_after_observation = None
        second = None
    if owner_race_action is not None and len(owner_race) != 1:
        raise AssertionError("owner race seam did not run exactly once")
    return {
        "mode": "recover" if phase is None else "recover-fault",
        "opening": _outcome_facts(opening),
        "durable_before_open": durable_before_open,
        "durable_after_open": durable_after_open,
        "durable_after_observation": durable_after_observation,
        "first_recovery": _outcome_facts(first),
        "second_recovery": None if second is None else _outcome_facts(second),
        "first_observation": (
            None if observation is None else _json_value(observation)
        ),
        "owner_race": owner_race[0] if owner_race else None,
        "snapshot": _snapshot_facts(
            identity,
            store,
            observation=observation,
        ),
    }


def _mutate_unmatched(root: Path) -> dict[str, object]:
    identity = _identity(root)
    prior = {
        "jsonl": identity.configured_jsonl_path.read_bytes(),
        "manifest": identity.snapshot_manifest_path.read_bytes(),
    }
    identity.configured_jsonl_path.write_bytes(
        b'{"source":"foreign","target":"unmatched-jsonl"}\n'
    )
    identity.snapshot_manifest_path.write_bytes(
        b'{"manifest":"unmatched"}\n'
    )
    return {
        "mode": "mutate-unmatched",
        "jsonl": _file_facts(identity.configured_jsonl_path),
        "manifest": _file_facts(identity.snapshot_manifest_path),
        "prior": {
            name: {
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in prior.items()
        },
    }


def _mutate_unmatched_with_backup(root: Path, backup: Path) -> dict[str, object]:
    if backup.exists():
        raise ValueError("race backup directory must not exist")
    identity = _identity(root)
    backup.mkdir(parents=True)
    (backup / "prior.jsonl").write_bytes(
        identity.configured_jsonl_path.read_bytes()
    )
    (backup / "prior.manifest.json").write_bytes(
        identity.snapshot_manifest_path.read_bytes()
    )
    return _mutate_unmatched(root)


def _mutate_pair_state(root: Path, kind: str) -> dict[str, object]:
    identity = _identity(root)
    if kind == "manifest-absent":
        identity.snapshot_manifest_path.unlink()
    elif kind == "foreign-jsonl-manifest-absent":
        identity.configured_jsonl_path.write_bytes(
            b'{"source":"foreign","target":"stable-absent"}\n'
        )
        identity.snapshot_manifest_path.unlink()
    elif kind == "jsonl-absent":
        identity.configured_jsonl_path.unlink()
    elif kind == "both-absent":
        identity.configured_jsonl_path.unlink()
        identity.snapshot_manifest_path.unlink()
    elif kind == "jsonl-unsafe-directory":
        identity.configured_jsonl_path.unlink()
        identity.configured_jsonl_path.mkdir()
    else:
        raise ValueError("unknown pair mutation")
    return {
        "mode": "mutate-pair",
        "kind": kind,
        "snapshot": _raw_snapshot_facts(identity),
    }


def _mutate_retirement_unknown(root: Path) -> dict[str, object]:
    identity = _identity(root)
    database = _database_facts(identity)
    rows = database["refresh_rows"]
    if type(rows) is not list or len(rows) != 1:
        raise AssertionError("unknown collision requires one refresh receipt")
    receipt = _receipt_from_row(rows[0])
    leaf = _retirement_leaf(receipt)
    target = root / RETIREMENT_ROOT_NAME / leaf
    target.mkdir(parents=True, exist_ok=True)
    foreign = target / "foreign.bin"
    foreign.write_bytes(b"foreign-retirement-collision\n")
    return {
        "mode": "mutate-retirement-unknown",
        "retirement_leaf": leaf,
        "foreign": _file_facts(foreign),
        "retirement": _retirement_facts(root),
    }


def _mutate_reconstruction_candidate_retirement_collision(
    root: Path,
) -> dict[str, object]:
    """Copy the exact candidate into its receipt leaf as an adversarial twin."""

    identity = _identity(root)
    database = _database_facts(identity)
    rows = database["refresh_rows"]
    if type(rows) is not list or len(rows) != 1 or rows[0][4] != "issued":
        raise AssertionError("candidate collision requires one issued receipt")
    receipt = _receipt_from_row(rows[0])
    paths = tm_migration._export_artifact_paths(identity.configured_jsonl_path)
    source = paths.manifest_temp
    source_facts = _file_facts(source)
    if source_facts is None:
        raise AssertionError("reconstruction candidate is absent")
    leaf = root / RETIREMENT_ROOT_NAME / _retirement_leaf(receipt)
    os.makedirs(_test_native_path(leaf), exist_ok=True)
    target = leaf / source.name
    if _file_facts(target) is not None:
        raise AssertionError("candidate retirement collision already exists")
    with open(_test_native_path(source), "rb") as input_stream:
        payload = input_stream.read()
    with open(_test_native_path(target), "xb") as output_stream:
        output_stream.write(payload)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    return {
        "mode": "mutate-reconstruction-candidate-retirement-collision",
        "retirement_leaf": leaf.name,
        "snapshot": _raw_snapshot_facts(identity),
    }


def _move_reconstruction_candidate_to_retirement(
    root: Path,
) -> dict[str, object]:
    """Create the real target-only candidate state via rooted no-overwrite move."""

    identity = _identity(root)
    database = _database_facts(identity)
    rows = database["refresh_rows"]
    if type(rows) is not list or len(rows) != 1 or rows[0][4] != "issued":
        raise AssertionError("target-only setup requires one issued receipt")
    receipt = _receipt_from_row(rows[0])
    paths = tm_migration._export_artifact_paths(identity.configured_jsonl_path)
    source_facts = _file_facts(paths.manifest_temp)
    if source_facts is None:
        raise AssertionError("target-only setup candidate is absent")
    expected = CandidateContentFacts(
        int(source_facts["size"]),
        bytes.fromhex(str(source_facts["sha256"])),
    )
    adapter = WindowsPlatformAdapter()
    rooted = retirement_root = leaf = source_parent = source = retained = None
    try:
        rooted = adapter.bind_root(root)
        retirement_root = adapter.bind_or_create_child_directory(
            rooted, RETIREMENT_ROOT_NAME
        )
        leaf = adapter.bind_or_create_child_directory(
            retirement_root, _retirement_leaf(receipt)
        )
        source_parent = adapter.bind_retirement_source_directory(
            rooted, PurePath(paths.manifest_temp.name)
        )
        source = adapter.open_existing_retirement_source(source_parent, expected)
        retained = adapter.retire_existing_exclusive(
            source_parent,
            source,
            leaf,
            paths.manifest_temp.name,
        )
        source = None
        retained.reprove()
    finally:
        for authority in (
            retained,
            source,
            source_parent,
            leaf,
            retirement_root,
            rooted,
        ):
            if authority is not None:
                authority.close()
    return {
        "mode": "move-reconstruction-candidate-to-retirement",
        "retirement_leaf": _retirement_leaf(receipt),
        "snapshot": _raw_snapshot_facts(identity),
    }


def _tamper_terminal_handoff(root: Path, kind: str) -> dict[str, object]:
    if kind == "unknown-target":
        result = _mutate_retirement_unknown(root)
        return {**result, "mode": "tamper-terminal-handoff", "kind": kind}
    identity = _identity(root)
    database = _database_facts(identity)
    handoffs = database["bound_refresh_handoffs"]
    if type(handoffs) is not list or len(handoffs) != 1:
        raise AssertionError("terminal tamper requires one portable handoff")
    row = handoffs[0]
    key = str(row["key"])
    value = row["value"]
    if type(value) is not dict:
        raise AssertionError("portable handoff is not decodable")
    changed = dict(value)
    if kind == "prior-link":
        changed["prior_snapshot_id"] = "snapshot.foreign.prior"
    elif kind == "new-digest":
        changed["new_jsonl_digest"] = "0" * 64
    else:
        raise ValueError("unknown terminal handoff tamper")
    connection = sqlite3.connect(str(identity.canonical_sidecar_path))
    try:
        updated = connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = ?",
            (
                json.dumps(
                    changed,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                key,
            ),
        )
        if updated.rowcount != 1:
            raise AssertionError("portable handoff tamper did not update one row")
        connection.commit()
    finally:
        connection.close()
    return {
        "mode": "tamper-terminal-handoff",
        "kind": kind,
        "handoff": _database_facts(identity)["bound_refresh_handoffs"],
    }


def _tamper_terminal_binding(root: Path, kind: str) -> dict[str, object]:
    identity = _identity(root)
    connection = sqlite3.connect(str(identity.canonical_sidecar_path))
    try:
        if kind == "path":
            updated = connection.execute(
                "UPDATE tm_snapshot_binding SET configured_jsonl_path = ?",
                (str(root / "foreign-configured.jsonl"),),
            )
        elif kind == "status":
            updated = connection.execute(
                "UPDATE tm_snapshot_receipt SET status = 'issued' "
                "WHERE snapshot_id LIKE ? AND status = 'completed'",
                (f"{REFRESH_PREFIX}%",),
            )
        else:
            raise ValueError("unknown terminal binding tamper")
        if updated.rowcount != 1:
            raise AssertionError("terminal binding tamper did not update one row")
        connection.commit()
    finally:
        connection.close()
    return {
        "mode": "tamper-terminal-binding",
        "kind": kind,
        "database": _database_facts(identity),
    }


def _tamper_multiple_bound_rows(root: Path) -> dict[str, object]:
    identity = _identity(root)
    connection = sqlite3.connect(str(identity.canonical_sidecar_path))
    try:
        columns = tuple(
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(tm_snapshot_receipt)"
            ).fetchall()
        )
        rows = connection.execute(
            "SELECT * FROM tm_snapshot_receipt WHERE snapshot_id LIKE ?",
            (f"{REFRESH_PREFIX}%",),
        ).fetchall()
        if len(rows) != 1 or "snapshot_id" not in columns:
            raise AssertionError("multiple-row tamper requires one refresh receipt")
        duplicate_id = f"{rows[0][columns.index('snapshot_id')]}.duplicate"
        duplicate_row = list(rows[0])
        duplicate_row[columns.index("snapshot_id")] = duplicate_id
        connection.execute(
            f"INSERT INTO tm_snapshot_receipt ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(duplicate_row),
        )
        handoff_rows = connection.execute(
            "SELECT key, value FROM tm_meta "
            "WHERE key LIKE 'bound_refresh_handoff.%'",
        ).fetchall()
        if len(handoff_rows) != 1:
            raise AssertionError("multiple-row tamper requires one bound handoff")
        handoff = json.loads(str(handoff_rows[0][1]))
        handoff["snapshot_id"] = duplicate_id
        connection.execute(
            "INSERT INTO tm_meta(key, value) VALUES(?, ?)",
            (
                f"bound_refresh_handoff.{duplicate_id}",
                json.dumps(
                    handoff,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "mode": "tamper-multiple-bound-rows",
        "database": _database_facts(identity),
    }


def _inspect(root: Path) -> dict[str, object]:
    identity = _identity(root)
    return {
        "mode": "inspect",
        "snapshot": _raw_snapshot_facts(identity),
    }


def _mutate_configured_manifest(root: Path, kind: str) -> dict[str, object]:
    identity = _identity(root)
    if kind == "foreign":
        identity.snapshot_manifest_path.write_bytes(
            b'{"manifest":"foreign-completed-runtime-observation"}\n'
        )
    elif kind == "absent":
        identity.snapshot_manifest_path.unlink()
    else:
        raise ValueError("unknown configured manifest mutation")
    return {
        "mode": "mutate-configured-manifest",
        "kind": kind,
        "snapshot": _raw_snapshot_facts(identity),
    }


def _tamper_hydration_authority(root: Path, kind: str) -> dict[str, object]:
    identity = _identity(root)
    target_facts: dict[str, object] | None = None
    if kind == "canonical-sidecar":
        payload = identity.canonical_sidecar_path.read_bytes()
        if not payload:
            raise AssertionError("canonical sidecar is empty")
        identity.canonical_sidecar_path.write_bytes(
            bytes((payload[0] ^ 1,)) + payload[1:]
        )
        target = identity.canonical_sidecar_path
    elif kind == "private-lineage":
        lineage_name = "activation-publication-generation-published-v1.json"
        candidates = tuple(
            path
            for path in root.glob(".localcat-activation-private-v1.*")
            if lineage_name in os.listdir("\\\\?\\" + str(path))
        )
        if len(candidates) != 1:
            raise AssertionError(
                "completed private lineage directory is not unique: "
                f"{tuple(path.name for path in candidates)!r}"
            )
        private_root = candidates[0]
        target = private_root / lineage_name
        extended_target = os.path.join("\\\\?\\" + str(private_root), lineage_name)
        with open(extended_target, "rb") as stream:
            payload = stream.read()
        changed = payload + b" "
        with open(extended_target, "wb") as stream:
            stream.write(changed)
        target_facts = {
            "name": lineage_name,
            "sha256": hashlib.sha256(changed).hexdigest(),
            "size": len(changed),
        }
    elif kind == "active-attestation":
        connection = sqlite3.connect(str(identity.canonical_sidecar_path))
        try:
            updated = connection.execute(
                "UPDATE tm_meta SET value = ? WHERE key = 'activation_digest'",
                ("0" * 64,),
            )
            if updated.rowcount != 1:
                raise AssertionError("active attestation digest row is unavailable")
            connection.commit()
        finally:
            connection.close()
        target = identity.canonical_sidecar_path
    else:
        raise ValueError("unknown hydration authority tamper")
    return {
        "mode": "tamper-hydration-authority",
        "kind": kind,
        "target": _file_facts(target) if target_facts is None else target_facts,
    }


def _cold_open_probe(root: Path) -> dict[str, object]:
    identity = _identity(root)
    recoveries: list[object] = []
    trace: list[str] = []
    real_recover = TMMigrationService.recover_configured_refresh

    def capture(
        service: TMMigrationService,
        store: SQLiteTMStore,
    ) -> object:
        outcome = real_recover(service, store)
        recoveries.append(outcome)
        return outcome

    def platform_trace(event: str) -> None:
        if event.startswith("existing_retirement_") or event.startswith(
            "retirement_directory_"
        ):
            trace.append(f"platform.{event}")

    backend = WindowsPlatformAdapter(_fault_injector=platform_trace)
    real_before_retire = tm_migration._before_bound_refresh_retire_role
    real_after_retire = tm_migration._after_bound_refresh_retire_role
    real_terminal_cleanup = tm_migration._after_bound_refresh_terminal_cleanup

    def before_retire(snapshot_id: str, role: str) -> None:
        trace.append(f"retire.before.{role}")
        real_before_retire(snapshot_id, role)

    def after_retire(snapshot_id: str, role: str) -> None:
        real_after_retire(snapshot_id, role)
        trace.append(f"retire.after.{role}")

    def terminal_cleanup(snapshot_id: str) -> None:
        real_terminal_cleanup(snapshot_id)
        trace.append("retire.terminal_cleanup")

    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(
                TMMigrationService,
                "recover_configured_refresh",
                new=capture,
            )
        )
        stack.enter_context(
            mock.patch.object(
                tm_migration,
                "compose_platform_file_backend",
                return_value=backend,
            )
        )
        stack.enter_context(
            mock.patch.object(
                tm_migration,
                "_before_bound_refresh_retire_role",
                new=before_retire,
            )
        )
        stack.enter_context(
            mock.patch.object(
                tm_migration,
                "_after_bound_refresh_retire_role",
                new=after_retire,
            )
        )
        stack.enter_context(
            mock.patch.object(
                tm_migration,
                "_after_bound_refresh_terminal_cleanup",
                new=terminal_cleanup,
            )
        )
        try:
            store = open_canonical_tm_store(
                identity.configured_jsonl_path,
                expected_resource_id=identity.resource_id,
            )
        except Exception as error:
            return {
                "mode": "cold-open-probe",
                "opened": False,
                "opening_error": {
                    "message": str(error),
                    "type": type(error).__name__,
                },
                "recoveries": [_outcome_facts(item) for item in recoveries],
                "trace": trace,
            }
    if store is None:
        raise AssertionError("completed runtime cold-open fell back to legacy")
    observation = store.source_binding_monitor.observe()
    return {
        "mode": "cold-open-probe",
        "opened": True,
        "opening_error": None,
        "recoveries": [_outcome_facts(item) for item in recoveries],
        "trace": trace,
        "snapshot": _snapshot_facts(identity, store, observation=observation),
    }


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    terminate = kernel32.TerminateProcess
    terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    terminate.restype = ctypes.c_int32
    if not terminate(int(process._handle), 94):
        raise ctypes.WinError(ctypes.get_last_error())


def _wait_for_marker(marker: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 180.0
    while not marker.is_file() or marker.stat().st_size == 0:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                "reboot preparation worker exited before marker: "
                f"{process.returncode}; "
                f"stdout={stdout.decode('utf-8', errors='replace')!r}; "
                f"stderr={stderr.decode('utf-8', errors='replace')!r}"
            )
        if time.monotonic() >= deadline:
            raise AssertionError("reboot preparation marker timeout")
        time.sleep(0.02)


def _reboot_prepare(root: Path, ticket_path: Path) -> dict[str, object]:
    if root.exists() or ticket_path.exists():
        raise ValueError("reboot prepare requires a new root and ticket")
    baseline = _prepare(root)
    marker = ticket_path.with_suffix(f"{ticket_path.suffix}.phase.json")
    marker.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "refresh-fault",
            str(root),
            "--phase",
            "issued.after",
            "--marker",
            str(marker),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_marker(marker, process)
        _terminate_process(process)
        stdout, stderr = process.communicate(timeout=30.0)
        if process.returncode != 94:
            raise AssertionError(
                "reboot preparation termination failed: "
                f"{process.returncode}; "
                f"stdout={stdout.decode('utf-8', errors='replace')!r}; "
                f"stderr={stderr.decode('utf-8', errors='replace')!r}"
            )
    finally:
        if process.poll() is None:
            _terminate_process(process)
            process.communicate(timeout=30.0)
    raw = _inspect(root)
    unsigned = {
        "baseline_canonical_digest": baseline["snapshot"]["canonical_digest"],
        "boot_session": _current_boot_session(),
        "phase": "issued.after",
        "prepared_raw": raw,
        "root": str(root),
        "schema": REBOOT_TICKET_SCHEMA,
        "worker_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
    }
    ticket = {
        **unsigned,
        "ticket_sha256": hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest(),
    }
    ticket_path.write_bytes(_canonical_json_bytes(ticket))
    return {
        "mode": "reboot-prepare",
        "reboot": "PREPARED",
        "ticket": str(ticket_path),
        "ticket_sha256": ticket["ticket_sha256"],
    }


def _reboot_resume(root: Path, ticket_path: Path) -> dict[str, object]:
    ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
    if type(ticket) is not dict or set(ticket) != {
        "baseline_canonical_digest",
        "boot_session",
        "phase",
        "prepared_raw",
        "root",
        "schema",
        "ticket_sha256",
        "worker_sha256",
    }:
        raise ValueError("reboot ticket shape mismatch")
    unsigned = {key: value for key, value in ticket.items() if key != "ticket_sha256"}
    if (
        ticket["schema"] != REBOOT_TICKET_SCHEMA
        or ticket["root"] != str(root)
        or ticket["phase"] != "issued.after"
        or ticket["worker_sha256"]
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        or ticket["ticket_sha256"]
        != hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()
    ):
        raise ValueError("reboot ticket authority mismatch")
    current_boot_session = _current_boot_session()
    if current_boot_session == ticket["boot_session"]:
        return {
            "mode": "reboot-resume",
            "reboot": "NOT_RUN",
            "ticket_sha256": ticket["ticket_sha256"],
        }
    recovered = _recover(root)
    if (
        recovered["snapshot"]["canonical_digest"]
        != ticket["baseline_canonical_digest"]
    ):
        raise AssertionError("post-reboot canonical snapshot changed")
    return {
        "mode": "reboot-resume",
        "reboot": "PASS",
        "recovery": recovered,
        "ticket_sha256": ticket["ticket_sha256"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in (
        "prepare",
        "prepare-gen0",
        "refresh",
        "recover",
        "inspect",
        "cold-open-probe",
        "mutate-retirement-unknown",
        "mutate-reconstruction-candidate-retirement-collision",
        "move-reconstruction-candidate-to-retirement",
        "tamper-multiple-bound-rows",
    ):
        command = subparsers.add_parser(mode)
        command.add_argument("root", type=Path)
    unmatched = subparsers.add_parser("mutate-unmatched")
    unmatched.add_argument("root", type=Path)
    unmatched.add_argument("--backup-pair", type=Path)
    race = subparsers.add_parser("recover-race")
    race.add_argument("root", type=Path)
    race.add_argument("--pair", type=Path, required=True)
    tamper = subparsers.add_parser("tamper-terminal-handoff")
    tamper.add_argument("root", type=Path)
    tamper.add_argument(
        "--kind",
        choices=("prior-link", "new-digest", "unknown-target"),
        required=True,
    )
    tamper_binding = subparsers.add_parser("tamper-terminal-binding")
    tamper_binding.add_argument("root", type=Path)
    tamper_binding.add_argument(
        "--kind",
        choices=("path", "status"),
        required=True,
    )
    mutate_pair = subparsers.add_parser("mutate-pair")
    mutate_pair.add_argument("root", type=Path)
    mutate_pair.add_argument(
        "--kind",
        choices=(
            "manifest-absent",
            "foreign-jsonl-manifest-absent",
            "jsonl-absent",
            "both-absent",
            "jsonl-unsafe-directory",
        ),
        required=True,
    )
    mutate_manifest = subparsers.add_parser("mutate-configured-manifest")
    mutate_manifest.add_argument("root", type=Path)
    mutate_manifest.add_argument(
        "--kind",
        choices=("foreign", "absent"),
        required=True,
    )
    tamper_hydration = subparsers.add_parser("tamper-hydration-authority")
    tamper_hydration.add_argument("root", type=Path)
    tamper_hydration.add_argument(
        "--kind",
        choices=("canonical-sidecar", "private-lineage", "active-attestation"),
        required=True,
    )
    configured_legacy = subparsers.add_parser(
        "configured-legacy-handoff-ambiguity"
    )
    configured_legacy.add_argument("root", type=Path)
    fault = subparsers.add_parser("refresh-fault")
    fault.add_argument("root", type=Path)
    fault.add_argument("--phase", choices=PUBLICATION_PHASES, required=True)
    fault.add_argument("--marker", type=Path, required=True)
    fault.add_argument("--advance-head-after-issued", action="store_true")
    recovery_fault = subparsers.add_parser("recover-fault")
    recovery_fault.add_argument("root", type=Path)
    recovery_fault.add_argument("--phase", choices=RECOVERY_PHASES, required=True)
    recovery_fault.add_argument("--marker", type=Path, required=True)
    reconstruction_fault = subparsers.add_parser("reconstruct-fault")
    reconstruction_fault.add_argument("root", type=Path)
    reconstruction_fault.add_argument(
        "--phase",
        choices=RECONSTRUCTION_PHASES,
        required=True,
    )
    reconstruction_fault.add_argument("--marker", type=Path, required=True)
    existing_candidate_fault = subparsers.add_parser(
        "recover-existing-candidate-fault"
    )
    existing_candidate_fault.add_argument("root", type=Path)
    existing_candidate_fault.add_argument(
        "--phase",
        choices=EXISTING_CANDIDATE_RECOVERY_PHASES,
        required=True,
    )
    existing_candidate_fault.add_argument("--marker", type=Path, required=True)
    owner_race = subparsers.add_parser("recover-owner-race")
    owner_race.add_argument("root", type=Path)
    owner_race.add_argument(
        "--action",
        choices=("cancel", "complete", "diverged", "clear"),
        required=True,
    )
    owner_race.add_argument(
        "--mutation",
        choices=(
            "receipt",
            "second-receipt",
            "prior",
            "id",
            "kind",
            "digest",
            "whole",
        ),
        required=True,
    )
    for mode in ("reboot-prepare", "reboot-resume"):
        command = subparsers.add_parser(mode)
        command.add_argument("root", type=Path)
        command.add_argument("--ticket", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if os.name != "nt":
        print("WINDOWS_TM_SNAPSHOT_RECOVERY_UNAVAILABLE", file=sys.stderr)
        return 2
    root = arguments.root.resolve()
    try:
        if arguments.mode == "prepare":
            result = _prepare(root)
        elif arguments.mode == "prepare-gen0":
            result = _prepare_generation_zero(root)
        elif arguments.mode == "refresh":
            result = _refresh(root)
        elif arguments.mode == "refresh-fault":
            result = _refresh_fault(
                root,
                arguments.phase,
                arguments.marker.resolve(),
                advance_head_after_issued=arguments.advance_head_after_issued,
            )
        elif arguments.mode == "recover-fault":
            result = _recover(
                root,
                phase=arguments.phase,
                marker=arguments.marker.resolve(),
            )
        elif arguments.mode == "reconstruct-fault":
            result = _recover(
                root,
                marker=arguments.marker.resolve(),
                reconstruction_phase=arguments.phase,
            )
        elif arguments.mode == "recover-existing-candidate-fault":
            result = _recover(
                root,
                marker=arguments.marker.resolve(),
                existing_candidate_phase=arguments.phase,
            )
        elif arguments.mode == "recover-owner-race":
            result = _recover(
                root,
                observe_after=False,
                owner_race_action=arguments.action,
                owner_race_mutation=arguments.mutation,
            )
        elif arguments.mode == "mutate-unmatched":
            result = (
                _mutate_unmatched(root)
                if arguments.backup_pair is None
                else _mutate_unmatched_with_backup(
                    root,
                    arguments.backup_pair.resolve(),
                )
            )
        elif arguments.mode == "recover-race":
            result = _recover(
                root,
                race_pair_directory=arguments.pair.resolve(),
                observe_after=False,
            )
        elif arguments.mode == "tamper-terminal-handoff":
            result = _tamper_terminal_handoff(root, arguments.kind)
        elif arguments.mode == "tamper-terminal-binding":
            result = _tamper_terminal_binding(root, arguments.kind)
        elif arguments.mode == "tamper-multiple-bound-rows":
            result = _tamper_multiple_bound_rows(root)
        elif arguments.mode == "mutate-pair":
            result = _mutate_pair_state(root, arguments.kind)
        elif arguments.mode == "mutate-configured-manifest":
            result = _mutate_configured_manifest(root, arguments.kind)
        elif arguments.mode == "tamper-hydration-authority":
            result = _tamper_hydration_authority(root, arguments.kind)
        elif arguments.mode == "configured-legacy-handoff-ambiguity":
            result = _configured_legacy_handoff_ambiguity(root)
        elif arguments.mode == "mutate-retirement-unknown":
            result = _mutate_retirement_unknown(root)
        elif (
            arguments.mode
            == "mutate-reconstruction-candidate-retirement-collision"
        ):
            result = _mutate_reconstruction_candidate_retirement_collision(root)
        elif arguments.mode == "move-reconstruction-candidate-to-retirement":
            result = _move_reconstruction_candidate_to_retirement(root)
        elif arguments.mode == "inspect":
            result = _inspect(root)
        elif arguments.mode == "cold-open-probe":
            result = _cold_open_probe(root)
        elif arguments.mode == "reboot-prepare":
            result = _reboot_prepare(root, arguments.ticket.resolve())
        elif arguments.mode == "reboot-resume":
            result = _reboot_resume(root, arguments.ticket.resolve())
        else:
            result = _recover(root)
    except BaseException as error:
        result = {
            "exception_message": str(error),
            "exception_type": type(error).__name__,
            "mode": arguments.mode,
            "schema": SCHEMA,
            "traceback": "".join(traceback.format_exception(error)),
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 1
    result["schema"] = SCHEMA
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
