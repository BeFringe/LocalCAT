"""Task 5.14 configured snapshot refresh crash recovery tests.

``recover_configured_refresh`` closes every Task 5.13 issued-receipt
crash window durably and idempotently: an old completed pair cancels
the issued receipt, an issued JSONL with an old or absent manifest
reconstructs the exact ledger manifest and completes, a full issued
pair completes and rebinds without republishing, and every unsafe,
foreign, ambiguous or invalid observation durably latches
``SOURCE_DIVERGED`` without touching foreign paths or canonical
authority.  Task 5.12 arbitrary-destination export receipts reuse the
same protocol without ever touching the active binding or divergence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest.mock import patch

import tm_migration
import tm_snapshot_artifacts
import tm_snapshot_recovery
import tm_sqlite_store
from tests.fault_matrix_registry import SNAPSHOT_PROCESS_DEATH_BOUNDARIES
from tm_activation_journal import (
    _activation_terminal_path,
    _activation_terminal_temp_path,
)
from tm_contracts import (
    SNAPSHOT_FORMAT_VERSION,
    CanonicalResourceIdentity,
    ExportReport,
    ExportFailure,
    MutableStageRef,
    SourceBindingState,
    SNAPSHOT_MANIFEST_VERSION,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    TMRecordDraft,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_migration import (
    ExportPreflightError,
    TMMigrationService,
    _export_artifact_paths,
)
from tm_snapshot_recovery import (
    IssuedReceiptRecovery,
    RecoveryError,
    RefreshRecoveryOutcome,
    RefreshRecoveryState,
)
from tm_sqlite_store import (
    SQLiteStoreLifecycleError,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
    initialize_stage_schema,
)


_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\Z")


def _stage(root: Path, resource_id: str = "tm.primary") -> MutableStageRef:
    configured = (root / f"{resource_id}.jsonl").resolve()
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        configured,
    )
    return MutableStageRef(
        stage_id=f"stage.{resource_id}",
        resource_identity=identity,
        staged_db_path=(root / f".{resource_id}.stage.sqlite3").resolve(),
        manifest_temp_path=(root / f".{resource_id}.snapshot.tmp").resolve(),
    )


def _draft(
    source: str,
    target: str,
    *,
    speaker: str | None = None,
    previous: str | None = None,
    following: str | None = None,
    file_source: str | None = None,
    provenance: tuple[tuple[str, str], ...] = (("source", "test"),),
) -> TMRecordDraft:
    return TMRecordDraft(
        source_raw=source,
        target_raw=target,
        speaker_raw=speaker,
        context_prev_raw=previous,
        context_next_raw=following,
        file_source=file_source,
        provenance=provenance,
    )


def _service(identity: Any) -> TMMigrationService:
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
    )


def _prepared_store(
    root: Path,
) -> tuple[Any, SQLiteTMStore]:
    """One live canonical store seeded with every supported draft variant."""

    stage = _stage(root)
    with patch("tm_sqlite_store._probe_fts5", return_value=False):
        initialize_stage_schema(stage, canonical_store_id="store.primary")
        store = SQLiteTMStore(stage, canonical_store_id="store.primary")
    _ = store.append_batch(
        batch_id="migration.seed.recovery",
        kind="migration",
        drafts=(
            _draft("same", "first", speaker="alice"),
            _draft("same", "second", speaker="alice"),
            _draft("Straße", "übersetzt"),
            _draft("minimal", "target only", provenance=()),
            _draft("context", "both", previous="p", following="f"),
            _draft("file", "src", file_source="book.txt"),
        ),
        source_digest="c" * 64,
        source_path=(root / "seed-source.jsonl").resolve(),
        legacy_line_nos=(1, 2, 3, 4, 5, 6),
    )
    return stage, store


def _fresh_store(stage: Any) -> SQLiteTMStore:
    """A second store instance over the same stage (process restart)."""

    with patch("tm_sqlite_store._probe_fts5", return_value=False):
        return SQLiteTMStore(stage, canonical_store_id="store.primary")


def _bind_current_snapshot(
    store: SQLiteTMStore,
    stage: Any,
    jsonl_bytes: bytes,
) -> SnapshotBinding:
    """Publish and register one completed binding for the current revision."""

    revision = store.canonical_revision()
    receipt = SnapshotReceipt(
        snapshot_id=f"snapshot.{stage.resource_identity.resource_id}.bound",
        resource_id=revision.resource_id,
        canonical_store_id=revision.canonical_store_id,
        exported_revision=revision.head_revision,
        jsonl_digest=hashlib.sha256(jsonl_bytes).hexdigest(),
        record_count=revision.record_count,
    )
    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    binding = SnapshotBinding(
        configured_jsonl_path=stage.resource_identity.configured_jsonl_path,
        manifest_path=stage.resource_identity.snapshot_manifest_path,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        manifest=manifest,
    )
    binding.configured_jsonl_path.write_bytes(jsonl_bytes)
    binding.manifest_path.write_text(
        contract_to_json(manifest),
        encoding="utf-8",
    )
    store.register_completed_snapshot_binding(binding)
    return binding


_PRIOR_JSONL = b'{"source":"bound","target":"pair"}\n'
_NEW_JSONL = b'{"source":"new","target":"content"}\n'


def _manifest_bytes_for(receipt: SnapshotReceipt) -> bytes:
    """Deterministic adjacent manifest bytes for one issued receipt."""

    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.EXPLICIT_EXPORT,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    return contract_to_json(manifest).encode("utf-8")


def _paths(identity: Any) -> Any:
    return _export_artifact_paths(identity.configured_jsonl_path)


def _pair(identity: Any) -> tuple[bytes, bytes]:
    return (
        identity.configured_jsonl_path.read_bytes(),
        identity.snapshot_manifest_path.read_bytes(),
    )


def _current_receipt(
    store: SQLiteTMStore,
    *,
    prefix: str,
    payload: bytes,
) -> SnapshotReceipt:
    """One ledger-valid receipt describing the current canonical revision."""

    revision = store.capture_export_snapshot().revision
    return SnapshotReceipt(
        snapshot_id=f"{prefix}{hashlib.sha256(payload).hexdigest()[:12]}",
        resource_id=revision.resource_id,
        canonical_store_id=revision.canonical_store_id,
        exported_revision=revision.head_revision,
        jsonl_digest=hashlib.sha256(payload).hexdigest(),
        record_count=revision.record_count,
    )


def _prior_state(
    path: Path,
) -> tuple[tuple[int, int] | None, str | None, bool]:
    """One (identity, digest, absent) prior-entry record for a path."""

    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return (None, None, True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ((observed.st_dev, observed.st_ino), digest, False)


def _identity_of(path: Path) -> tuple[int, int]:
    """The exact device/inode identity of one existing entry."""

    observed = os.lstat(path)
    return (observed.st_dev, observed.st_ino)


def _register_refresh(
    store: SQLiteTMStore,
    receipt: SnapshotReceipt,
    *,
    jsonl_temp_identity: tuple[int, int] | None = None,
    manifest_temp_identity: tuple[int, int] | None = None,
    payload: bytes = _NEW_JSONL,
) -> None:
    """Register one issued refresh with a durable handoff journal.

    The registration durably records the current configured pair as the
    prior pair (digest+identity or explicit absence) and the exclusive
    deterministic temporary identities.  The deterministic temporaries
    are created with the exact bytes the recovery protocol expects (the
    receipt JSONL payload and the deterministic adjacent manifest), so
    registration's descriptor-relative temp proof passes and recovery
    cleanup can prove and remove them by digest plus handoff identity.
    Callers that need a foreign/same-byte entry later replace the inode.
    """

    revision = store.capture_export_snapshot().revision
    identity = store._coordinator._resource_identity
    paths = _paths(identity)
    prior_jsonl = _prior_state(identity.configured_jsonl_path)
    prior_manifest = _prior_state(identity.snapshot_manifest_path)
    if jsonl_temp_identity is None:
        paths.jsonl_temp.write_bytes(payload)
        jsonl_temp_identity = _identity_of(paths.jsonl_temp)
    if manifest_temp_identity is None:
        paths.manifest_temp.write_bytes(_manifest_bytes_for(receipt))
        manifest_temp_identity = _identity_of(paths.manifest_temp)
    store.register_issued_refresh_receipt(
        receipt,
        expected_generation=revision.generation,
        jsonl_temp_identity=jsonl_temp_identity,
        manifest_temp_identity=manifest_temp_identity,
        artifact_parent_identity=_identity_of(
            identity.configured_jsonl_path.parent
        ),
        prior_jsonl_identity=prior_jsonl[0],
        prior_jsonl_digest=prior_jsonl[1],
        prior_jsonl_absent=prior_jsonl[2],
        prior_manifest_identity=prior_manifest[0],
        prior_manifest_digest=prior_manifest[1],
        prior_manifest_absent=prior_manifest[2],
    )


def _write_new_pair(identity: Any, receipt: SnapshotReceipt, payload: bytes) -> None:
    """Publish one full new pair exactly like the crashed publisher.

    The deterministic temporary entries are renamed into the final
    slots so the final inodes are the handed-off temp inodes (rename is
    inode-preserving); writing fresh files here would simulate a
    same-byte foreign inode at the final slot and must fail closed.
    """

    paths = _paths(identity)
    paths.jsonl_temp.write_bytes(payload)
    paths.manifest_temp.write_bytes(_manifest_bytes_for(receipt))
    os.replace(paths.jsonl_temp, identity.configured_jsonl_path)
    os.replace(
        paths.manifest_temp,
        identity.snapshot_manifest_path,
    )


def _publish_registered_pair(
    destination: Path,
    receipt: SnapshotReceipt,
    payload: bytes,
) -> None:
    """Rename a registered export's deterministic temps into the final
    slots exactly like the crashed publisher.

    The final inodes are then the handed-off temp inodes (rename is
    inode-preserving); writing fresh files at the final slots would
    simulate a same-byte foreign inode and must fail closed.
    """

    paths = _export_artifact_paths(destination)
    paths.jsonl_temp.write_bytes(payload)
    paths.manifest_temp.write_bytes(_manifest_bytes_for(receipt))
    os.replace(paths.jsonl_temp, paths.destination)
    os.replace(paths.manifest_temp, paths.manifest)


def _ledger_rows(
    store_path: Path,
    configured_jsonl: Path,
) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(store_path)
    try:
        rows = connection.execute(
            "SELECT snapshot_id, resource_id, canonical_store_id, "
            "exported_revision, jsonl_digest, record_count, "
            "format_version, destination_jsonl_path, "
            "destination_manifest_path, status "
            "FROM tm_snapshot_receipt "
            "WHERE destination_jsonl_path = ?",
            (str(configured_jsonl),),
        ).fetchall()
    finally:
        connection.close()
    return tuple(rows)


def _refresh_rows(
    store_path: Path,
    configured_jsonl: Path,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        row
        for row in _ledger_rows(store_path, configured_jsonl)
        if str(row[0]).startswith("snapshot.refresh.")
    )


def _issued_refresh_snapshot_id(store_path: Path) -> str:
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT snapshot_id FROM tm_snapshot_receipt "
            "WHERE snapshot_id LIKE 'snapshot.refresh.%' "
            "AND status = 'issued' ORDER BY snapshot_id"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise AssertionError("issued refresh receipt row is missing")
    return str(row[0])


def _refresh_snapshot_id(store_path: Path) -> str:
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT snapshot_id FROM tm_snapshot_receipt "
            "WHERE snapshot_id LIKE 'snapshot.refresh.%' "
            "ORDER BY snapshot_id"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise AssertionError("refresh receipt row is missing")
    return str(row[0])


def _status_for(store_path: Path, snapshot_id: str) -> str | None:
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT status FROM tm_snapshot_receipt "
            "WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


def _binding_row(store_path: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT binding_id, configured_jsonl_path, manifest_path, "
            "snapshot_kind, snapshot_id, binding_version "
            "FROM tm_snapshot_binding WHERE binding_id = 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise AssertionError("snapshot binding row is missing")
    return row


def _record_dump(store_path: Path) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(store_path)
    try:
        rows = connection.execute(
            "SELECT record_id, source_raw, target_raw, speaker_raw, "
            "context_prev_raw, context_next_raw, file_source, "
            "provenance_json, legacy_line_no, usage_count, last_used, "
            "origin_batch_id, origin_ordinal "
            "FROM tm_record ORDER BY record_id ASC"
        ).fetchall()
    finally:
        connection.close()
    return tuple(rows)


def _meta_value(store_path: Path, key: str) -> str | None:
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT value FROM tm_meta WHERE key = ?",
            (key,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


def _set_meta_value(store_path: Path, key: str, value: str) -> None:
    connection = sqlite3.connect(store_path)
    try:
        updated = connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = ?",
            (value, key),
        )
        if updated.rowcount != 1:
            raise AssertionError(f"missing tm_meta row {key!r}")
        connection.commit()
    finally:
        connection.close()


def _set_receipt_status(
    store_path: Path,
    snapshot_id: str,
    status: str,
) -> None:
    connection = sqlite3.connect(store_path)
    try:
        updated = connection.execute(
            "UPDATE tm_snapshot_receipt SET status = ? "
            "WHERE snapshot_id = ?",
            (status, snapshot_id),
        )
        if updated.rowcount != 1:
            raise AssertionError(f"missing receipt {snapshot_id!r}")
        connection.commit()
    finally:
        connection.close()


def _insert_ledger_row(
    store_path: Path,
    receipt: SnapshotReceipt,
    *,
    destination_jsonl_path: Path,
    destination_manifest_path: Path,
    status: str,
    resource_id: str | None = None,
    canonical_store_id: str | None = None,
) -> None:
    """Insert one receipt row directly, bypassing the store validation seams."""

    connection = sqlite3.connect(store_path)
    try:
        connection.execute(
            "INSERT INTO tm_snapshot_receipt("
            "snapshot_id, resource_id, canonical_store_id, "
            "exported_revision, jsonl_digest, record_count, "
            "format_version, destination_jsonl_path, "
            "destination_manifest_path, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt.snapshot_id,
                (
                    resource_id
                    if resource_id is not None
                    else receipt.resource_id
                ),
                (
                    canonical_store_id
                    if canonical_store_id is not None
                    else receipt.canonical_store_id
                ),
                receipt.exported_revision,
                receipt.jsonl_digest,
                receipt.record_count,
                receipt.format_version,
                str(destination_jsonl_path),
                str(destination_manifest_path),
                status,
                "2026-08-11T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _simulate_crash(
    service: TMMigrationService,
    store: SQLiteTMStore,
    *,
    crash_at: str,
) -> None:
    """Run one refresh that aborts at the exact crash boundary.

    A ``KeyboardInterrupt`` (BaseException) propagates past the export
    failure handling exactly like process death, leaving the issued
    ledger row and the intermediate filesystem state in place.
    """

    identity = service._resource_identity
    paths = _paths(identity)
    original_replace = tm_migration._replace_path

    def crashing_replace(
        source: Path,
        target: Path,
        **kwargs: Any,
    ) -> None:
        if crash_at == "before-jsonl" and source == paths.jsonl_temp:
            raise KeyboardInterrupt(
                "injected crash before first publication replace"
            )
        if crash_at == "jsonl" and source == paths.jsonl_temp:
            original_replace(source, target, **kwargs)
            raise KeyboardInterrupt("injected crash after jsonl replace")
        if crash_at == "manifest" and source == paths.manifest_temp:
            original_replace(source, target, **kwargs)
            raise KeyboardInterrupt("injected crash after manifest replace")
        original_replace(source, target, **kwargs)

    original_copy = tm_migration._copy_export_prior_pair

    def copying_then_raise(
        paths: Any,
        **kwargs: Any,
    ) -> Any:
        original_copy(paths, **kwargs)
        raise KeyboardInterrupt(
            "injected crash after recovery copies, before handoff"
        )

    if crash_at == "issued":
        inject = patch(
            "tm_migration._copy_export_prior_pair",
            side_effect=KeyboardInterrupt(
                "injected crash after issued receipt"
            ),
        )
    elif crash_at == "after-copies":
        inject = patch(
            "tm_migration._copy_export_prior_pair",
            side_effect=copying_then_raise,
        )
    elif crash_at in {"jsonl", "manifest"}:
        inject = patch(
            "tm_migration._replace_path",
            side_effect=crashing_replace,
        )
    elif crash_at == "before-jsonl":
        inject = patch(
            "tm_migration._replace_path",
            side_effect=crashing_replace,
        )
    else:
        raise AssertionError(f"unknown crash boundary {crash_at!r}")
    with inject:
        try:
            service.refresh_configured_snapshot(store)
        except KeyboardInterrupt:
            return
    raise AssertionError(f"refresh did not crash at {crash_at!r}")


_RECOVERY_CHILD_SCRIPT = r'''
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import tm_snapshot_recovery
from tm_contracts import CanonicalResourceIdentity, MutableStageRef
from tm_sqlite_store import SQLiteTMStore

mode = sys.argv[1]
resource_id = sys.argv[2]
configured = sys.argv[3]
staged_db = sys.argv[4]
manifest_temp = sys.argv[5]
marker = sys.argv[6]

stage = MutableStageRef(
    stage_id="stage." + resource_id,
    resource_identity=CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        Path(configured),
    ),
    staged_db_path=Path(staged_db),
    manifest_temp_path=Path(manifest_temp),
)
with patch("tm_sqlite_store._probe_fts5", return_value=False):
    store = SQLiteTMStore(stage, canonical_store_id="store.primary")
if mode == "crash":
    original_capture = tm_snapshot_recovery._recovery_parent_capture

    def capture_then_die(parent, name, asset_kind):
        result = original_capture(parent, name, asset_kind)
        Path(marker).write_text(asset_kind, encoding="utf-8")
        os._exit(86)

    with patch(
        "tm_snapshot_recovery._recovery_parent_capture",
        side_effect=capture_then_die,
    ):
        store.recover_configured_refresh()
    os._exit(0)
first = store.recover_configured_refresh()
second = store.recover_configured_refresh()
observed = os.lstat(stage.resource_identity.snapshot_manifest_path)
print(
    json.dumps(
        {
            "first_state": first.state,
            "first_receipts": [
                [item.snapshot_id, item.state, list(item.diagnostics)]
                for item in first.receipts
            ],
            "second_state": second.state,
            "second_receipts": [
                [item.snapshot_id, item.state, list(item.diagnostics)]
                for item in second.receipts
            ],
            "manifest_device": observed.st_dev,
            "manifest_inode": observed.st_ino,
        }
    )
)
'''


def _run_recovery_child(
    stage: Any,
    marker: Path,
    mode: str,
) -> subprocess.CompletedProcess[str]:
    """Run one recovery in a real child process over the same stage."""

    identity = stage.resource_identity
    repository_root = Path(__file__).resolve().parent.parent
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _RECOVERY_CHILD_SCRIPT,
            mode,
            identity.resource_id,
            str(identity.configured_jsonl_path),
            str(stage.staged_db_path),
            str(stage.manifest_temp_path),
            str(marker),
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


_PUBLICATION_DEATH_CHILD_SCRIPT = r'''
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import tm_migration
import tm_snapshot_artifacts
from tm_contracts import CanonicalResourceIdentity, MutableStageRef
from tm_migration import TMMigrationService
from tm_sqlite_store import SQLiteTMStore

mode = sys.argv[1]
seam = sys.argv[2]
ordinal = int(sys.argv[3])
resource_id = sys.argv[4]
configured = Path(sys.argv[5])
staged_db = Path(sys.argv[6])
manifest_temp = Path(sys.argv[7])
destination = Path(sys.argv[8])
marker = Path(sys.argv[9])

stage = MutableStageRef(
    stage_id="stage." + resource_id,
    resource_identity=CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        configured,
    ),
    staged_db_path=staged_db,
    manifest_temp_path=manifest_temp,
)
with patch("tm_sqlite_store._probe_fts5", return_value=False):
    store = SQLiteTMStore(stage, canonical_store_id="store.primary")
service = TMMigrationService(
    resource_identity=stage.resource_identity,
    canonical_store_id="store.primary",
)

def die() -> None:
    descriptor = os.open(
        marker,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        os.write(descriptor, seam.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os._exit(86)

def counted(original):
    count = 0
    def wrapper(*args, **kwargs):
        nonlocal count
        result = original(*args, **kwargs)
        count += 1
        if count == ordinal:
            die()
        return result
    return wrapper

with ExitStack() as stack:
    if seam == "file_fsync":
        stack.enter_context(
            patch.object(
                tm_migration,
                "_fsync_file",
                side_effect=counted(tm_migration._fsync_file),
            )
        )
    elif seam == "replace":
        stack.enter_context(
            patch.object(
                tm_migration,
                "_replace_path",
                side_effect=counted(tm_migration._replace_path),
            )
        )
    elif seam == "directory_fsync":
        stack.enter_context(
            patch.object(
                tm_migration,
                "_fsync_directory",
                side_effect=counted(tm_migration._fsync_directory),
            )
        )
    elif seam == "cleanup_unlink":
        stack.enter_context(
            patch.object(
                tm_snapshot_artifacts,
                "_remove_created_file",
                side_effect=counted(
                    tm_snapshot_artifacts._remove_created_file
                ),
            )
        )
    else:
        if seam == "register":
            method_name = (
                "register_issued_refresh_receipt"
                if mode == "refresh"
                else "register_issued_export_receipt"
            )
        elif seam == "handoff":
            method_name = "record_export_recovery_handoff"
        elif seam == "complete":
            method_name = (
                "complete_issued_refresh_receipt"
                if mode == "refresh"
                else "complete_issued_export_receipt"
            )
        elif seam == "clear":
            method_name = "clear_issued_receipt_handoff"
        else:
            raise AssertionError("unknown process-death seam " + seam)
        original = getattr(store, method_name)
        stack.enter_context(
            patch.object(
                store,
                method_name,
                side_effect=counted(original),
            )
        )
    if mode == "refresh":
        service.refresh_configured_snapshot(store)
    elif mode == "export":
        service.export_jsonl(store, destination)
    else:
        raise AssertionError("unknown process-death mode " + mode)
os._exit(87)
'''


def _run_publication_death_child(
    stage: Any,
    *,
    mode: str,
    seam: str,
    ordinal: int,
    destination: Path,
    marker: Path,
) -> subprocess.CompletedProcess[str]:
    identity = stage.resource_identity
    repository_root = Path(__file__).resolve().parent.parent
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _PUBLICATION_DEATH_CHILD_SCRIPT,
            mode,
            seam,
            str(ordinal),
            identity.resource_id,
            str(identity.configured_jsonl_path),
            str(stage.staged_db_path),
            str(stage.manifest_temp_path),
            str(destination),
            str(marker),
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _assert_recovery_outcome_shape(
    testcase: unittest.TestCase,
    outcome: RefreshRecoveryOutcome,
) -> None:
    testcase.assertIsInstance(outcome.state, RefreshRecoveryState)
    testcase.assertIsInstance(outcome.receipts, tuple)
    for item in outcome.receipts:
        testcase.assertIsInstance(item, IssuedReceiptRecovery)
        testcase.assertIsInstance(item.state, RefreshRecoveryState)
        testcase.assertIsInstance(item.diagnostics, tuple)
        for code in item.diagnostics:
            testcase.assertRegex(code, _IDENTIFIER)
    testcase.assertIsInstance(outcome.diagnostics, tuple)
    for code in outcome.diagnostics:
        testcase.assertRegex(code, _IDENTIFIER)
    if outcome.error_code is not None:
        testcase.assertRegex(outcome.error_code, _IDENTIFIER)
    testcase.assertIsInstance(outcome.retryable, bool)


class TMRecoveryConfiguredDecisionTests(unittest.TestCase):
    def test_old_pair_plus_issued_cancels_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.CANCELLED)
            self.assertEqual(outcome.snapshot_id, receipt.snapshot_id)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.CANCELLED,
                    ),
                ),
            )
            self.assertEqual(outcome.diagnostics, ())
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "cancelled",
            )
            self.assertEqual(_pair(identity), (_PRIOR_JSONL, prior_manifest))
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            paths = _paths(identity)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

    def test_jsonl_only_new_digest_reconstructs_manifest_and_completes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            os.replace(
                _paths(identity).jsonl_temp,
                identity.configured_jsonl_path,
            )
            prior_manifest = identity.snapshot_manifest_path.read_bytes()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(outcome.snapshot_id, receipt.snapshot_id)
            self.assertEqual(outcome.diagnostics, ())
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                _manifest_bytes_for(receipt),
            )
            self.assertNotEqual(
                identity.snapshot_manifest_path.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_full_new_pair_completes_without_republish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            jsonl_identity = os.lstat(identity.configured_jsonl_path)
            manifest_identity = os.lstat(identity.snapshot_manifest_path)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(outcome.diagnostics, ())
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )
            self.assertEqual(
                os.lstat(identity.configured_jsonl_path).st_ino,
                jsonl_identity.st_ino,
            )
            self.assertEqual(
                os.lstat(identity.snapshot_manifest_path).st_ino,
                manifest_identity.st_ino,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertNotEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_history_revision_pair_completes_as_verified_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            _ = store.append(_draft("newer", "appended after crash"))

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_HISTORY,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_foreign_manifest_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            foreign_manifest = b"foreign manifest bytes\n"
            identity.snapshot_manifest_path.write_bytes(foreign_manifest)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertEqual(outcome.snapshot_id, receipt.snapshot_id)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.DIVERGED,
                        ("RECOVERY.MANIFEST_FOREIGN",),
                    ),
                ),
            )
            self.assertIn("RECOVERY.MANIFEST_FOREIGN", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                foreign_manifest,
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

    def test_missing_pair_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.unlink()
            identity.snapshot_manifest_path.unlink()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.PAIR_UNMATCHED", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_symlink_pair_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            target = identity.configured_jsonl_path.with_name("foreign.jsonl")
            target.write_bytes(_NEW_JSONL)
            identity.configured_jsonl_path.unlink()
            identity.configured_jsonl_path.symlink_to(target)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.PAIR_UNSAFE", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertTrue(identity.configured_jsonl_path.is_symlink())
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_hardlink_pair_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            other = identity.configured_jsonl_path.with_name("linked.jsonl")
            other.write_bytes(_NEW_JSONL)
            os.replace(other, identity.configured_jsonl_path)
            os.link(identity.configured_jsonl_path, other)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.PAIR_UNSAFE", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(os.lstat(identity.configured_jsonl_path).st_nlink, 2)
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_directory_manifest_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            identity.snapshot_manifest_path.unlink()
            identity.snapshot_manifest_path.mkdir()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.PAIR_UNSAFE", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertTrue(identity.snapshot_manifest_path.is_dir())
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_ambiguous_issued_receipts_latch_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            first = _current_receipt(store, prefix="snapshot.refresh.a.", payload=_NEW_JSONL)
            second = _current_receipt(store, prefix="snapshot.refresh.b.", payload=_NEW_JSONL)
            _register_refresh(store, first)
            _register_refresh(store, second)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            identity.snapshot_manifest_path.write_bytes(b"foreign manifest\n")

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.AMBIGUOUS_JSONL", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, first.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, second.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_foreign_ledger_path_issued_is_export_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _insert_ledger_row(
                stage.staged_db_path,
                receipt,
                destination_jsonl_path=(root / "foreign.jsonl").resolve(),
                destination_manifest_path=(
                    root / "foreign.jsonl.localcat-snapshot.json"
                ).resolve(),
                status="issued",
            )

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.HANDOFF_MISSING",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_ancestry_invalid_receipt_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _insert_ledger_row(
                stage.staged_db_path,
                receipt,
                destination_jsonl_path=identity.configured_jsonl_path,
                destination_manifest_path=identity.snapshot_manifest_path,
                status="issued",
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_snapshot_receipt SET exported_revision = 999 "
                    "WHERE snapshot_id = ?",
                    (receipt.snapshot_id,),
                )
                connection.commit()
            finally:
                connection.close()
            _write_new_pair(identity, receipt, _NEW_JSONL)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.ANCESTRY_INVALID", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_binding_invalid_latches_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_snapshot_binding SET snapshot_id = ? "
                    "WHERE binding_id = 1",
                    (receipt.snapshot_id,),
                )
                connection.commit()
            finally:
                connection.close()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertIn("RECOVERY.BINDING_INVALID", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )
            self.assertEqual(
                store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

    def test_binding_tamper_never_cancels_old_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            prior_pair = _pair(identity)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_snapshot_binding SET snapshot_id = ? "
                    "WHERE binding_id = 1",
                    (receipt.snapshot_id,),
                )
                connection.commit()
            finally:
                connection.close()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.DIVERGED,
                        ("RECOVERY.BINDING_INVALID",),
                    ),
                ),
            )
            self.assertIn("RECOVERY.BINDING_INVALID", outcome.diagnostics)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(_pair(identity), prior_pair)

    def test_receipt_destination_tamper_never_cancels_old_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _insert_ledger_row(
                stage.staged_db_path,
                receipt,
                destination_jsonl_path=identity.snapshot_manifest_path,
                destination_manifest_path=identity.configured_jsonl_path,
                status="issued",
            )
            prior_pair = _pair(identity)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PATH_INVALID",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(_pair(identity), prior_pair)
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_foreign_identity_issued_never_noops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _insert_ledger_row(
                stage.staged_db_path,
                receipt,
                destination_jsonl_path=identity.configured_jsonl_path,
                destination_manifest_path=identity.snapshot_manifest_path,
                status="issued",
                resource_id="tm.foreign",
            )
            _write_new_pair(identity, receipt, _NEW_JSONL)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.DIVERGED,
                        ("RECOVERY.LEDGER_IDENTITY_INVALID",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.LEDGER_IDENTITY_INVALID",
                outcome.diagnostics,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(_pair(identity), (_NEW_JSONL, _manifest_bytes_for(receipt)))
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_pre_existing_divergence_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_meta SET value = '1' "
                    "WHERE key = 'divergence_latched'"
                )
                connection.commit()
            finally:
                connection.close()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.NOOP)
            self.assertIn("RECOVERY.DIVERGENCE_PRESERVED", outcome.diagnostics)
            self.assertEqual(outcome.snapshot_id, None)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )


class TMRecoveryCrashBoundaryTests(unittest.TestCase):
    def test_crash_after_issued_cancels_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            _simulate_crash(service, store, crash_at="issued")
            fresh = _fresh_store(stage)

            outcome = fresh.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.CANCELLED)
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "cancelled")
            self.assertEqual(_pair(identity), (_PRIOR_JSONL, prior_manifest))
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            paths = _paths(identity)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

    def test_crash_after_recovery_copies_cancels_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            _simulate_crash(service, store, crash_at="before-jsonl")
            snapshot_id = _issued_refresh_snapshot_id(stage.staged_db_path)
            fresh = _fresh_store(stage)

            outcome = fresh.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.CANCELLED)
            self.assertEqual(outcome.diagnostics, ())
            self.assertEqual(
                _status_for(
                    stage.staged_db_path,
                    snapshot_id,
                ),
                "cancelled",
            )
            self.assertEqual(_pair(identity), (_PRIOR_JSONL, prior_manifest))
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            paths = _paths(identity)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

    def test_crash_before_recovery_handoff_preserves_copies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_pair = _pair(identity)
            _simulate_crash(service, store, crash_at="after-copies")
            snapshot_id = _issued_refresh_snapshot_id(stage.staged_db_path)
            fresh = _fresh_store(stage)
            paths = _paths(identity)
            jsonl_recovery_before = os.lstat(paths.jsonl_recovery)
            manifest_recovery_before = os.lstat(paths.manifest_recovery)

            outcome = fresh.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertIn("RECOVERY.ARTIFACT_UNPROVEN", outcome.diagnostics)
            self.assertEqual(
                _status_for(
                    stage.staged_db_path,
                    snapshot_id,
                ),
                "issued",
            )
            self.assertEqual(_pair(identity), prior_pair)
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            self.assertEqual(
                (os.lstat(paths.jsonl_recovery).st_dev, os.lstat(paths.jsonl_recovery).st_ino),
                (jsonl_recovery_before.st_dev, jsonl_recovery_before.st_ino),
            )
            self.assertEqual(
                (
                    os.lstat(paths.manifest_recovery).st_dev,
                    os.lstat(paths.manifest_recovery).st_ino,
                ),
                (
                    manifest_recovery_before.st_dev,
                    manifest_recovery_before.st_ino,
                ),
            )
            self.assertTrue(paths.jsonl_temp.exists())
            self.assertTrue(paths.manifest_temp.exists())

    def test_crash_after_jsonl_replace_reconstructs_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="jsonl")
            fresh = _fresh_store(stage)

            outcome = fresh.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                str(refresh_rows[0][0]),
            )
            self.assertEqual(
                fresh.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            paths = _paths(identity)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

    def test_process_death_after_reconstruction_temp_fsync_replays_to_completion(
        self,
    ) -> None:
        """Recovery-within-recovery process death (reviewer P1 regression).

        The first recovery runs in a real child process and aborts via
        ``os._exit`` (bypassing every ``finally``) immediately after the
        reconstruction manifest temp has been re-proven/fsynced and
        before the manifest replace or receipt completion.  A fresh
        child-process recovery must reuse the exact same handed-off
        inode for the atomic replace and complete deterministically
        (second recovery NOOP, no issued/handoff wedge, canonical
        generation and records preserved) instead of permanently
        BLOCKing on ``RECOVERY.ARTIFACT_UNPROVEN``.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="jsonl")
            snapshot_id = _issued_refresh_snapshot_id(stage.staged_db_path)
            paths = _paths(identity)
            meta_key = "artifact_handoff." + snapshot_id
            handoff_value = _meta_value(stage.staged_db_path, meta_key)
            self.assertIsNotNone(handoff_value)
            assert handoff_value is not None
            handoff = json.loads(handoff_value)
            handed_off_identity = (
                handoff["manifest_temp_device"],
                handoff["manifest_temp_inode"],
            )
            self.assertEqual(
                _identity_of(paths.manifest_temp),
                handed_off_identity,
            )
            canonical_before = store.capture_export_snapshot().revision
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            marker = root / "reconstruction-crash.marker"

            crashed = _run_recovery_child(stage, marker, mode="crash")

            self.assertEqual(
                crashed.returncode,
                86,
                msg=crashed.stdout + crashed.stderr,
            )
            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                "RECOVERY_MANIFEST_TEMP",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, snapshot_id),
                "issued",
            )
            self.assertIsNotNone(
                _meta_value(stage.staged_db_path, meta_key)
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                _identity_of(paths.manifest_temp),
                handed_off_identity,
            )

            replayed = _run_recovery_child(stage, marker, mode="finish")

            self.assertEqual(
                replayed.returncode,
                0,
                msg=replayed.stdout + replayed.stderr,
            )
            facts = json.loads(replayed.stdout)
            self.assertEqual(facts["first_state"], "COMPLETED")
            self.assertEqual(
                facts["first_receipts"],
                [[snapshot_id, "COMPLETED", []]],
            )
            self.assertEqual(facts["second_state"], "NOOP")
            self.assertEqual(facts["second_receipts"], [])
            self.assertEqual(
                (facts["manifest_device"], facts["manifest_inode"]),
                handed_off_identity,
            )
            row = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )[0]
            issued_receipt = SnapshotReceipt(
                snapshot_id=str(row[0]),
                resource_id=str(row[1]),
                canonical_store_id=str(row[2]),
                exported_revision=int(str(row[3])),
                jsonl_digest=str(row[4]),
                record_count=int(str(row[5])),
                format_version=str(row[6]),
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                _manifest_bytes_for(issued_receipt),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, snapshot_id),
                "completed",
            )
            self.assertIsNone(
                _meta_value(stage.staged_db_path, meta_key)
            )
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            canonical_after = store.capture_export_snapshot().revision
            self.assertEqual(canonical_after.generation, canonical_before.generation)
            self.assertEqual(canonical_after.record_count, canonical_before.record_count)
            self.assertEqual(canonical_after.head_revision, canonical_before.head_revision)

    def test_crash_after_manifest_replace_completes_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="manifest")
            fresh = _fresh_store(stage)

            outcome = fresh.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                str(refresh_rows[0][0]),
            )
            self.assertEqual(
                fresh.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_crash_after_complete_commit_fails_closed_then_recovery_finishes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            original_complete = store.complete_issued_refresh_receipt

            def commit_then_raise(
                snapshot_id: str,
                **kwargs: Any,
            ) -> None:
                original_complete(snapshot_id, **kwargs)
                raise SQLiteStoreLifecycleError(
                    "STORE.LEDGER_UNAVAILABLE",
                    resource_id="tm.primary",
                    generation=0,
                    retryable=True,
                )

            with patch.object(
                store,
                "complete_issued_refresh_receipt",
                side_effect=commit_then_raise,
            ):
                result = service.refresh_configured_snapshot(store)

            self.assertIsInstance(result, ExportFailure)
            assert isinstance(result, ExportFailure)
            self.assertEqual(result.stage, "REFRESH.LEDGER")
            self.assertIn(
                "EXPORT.LEDGER_UNCLEAN",
                tuple(d.code for d in result.diagnostics),
            )
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                str(refresh_rows[0][0]),
            )
            fresh = _fresh_store(stage)
            replay = fresh.recover_configured_refresh()
            _assert_recovery_outcome_shape(self, replay)
            self.assertIs(replay.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                replay.receipts,
                (
                    IssuedReceiptRecovery(
                        str(refresh_rows[0][0]),
                        RefreshRecoveryState.COMPLETED,
                    ),
                ),
            )
            self.assertEqual(replay.diagnostics, ())
            self.assertEqual(
                fresh.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            second = fresh.recover_configured_refresh()
            self.assertIs(second.state, RefreshRecoveryState.NOOP)
            self.assertEqual(second.diagnostics, ())
            follow_up = service.refresh_configured_snapshot(fresh)
            self.assertIsInstance(follow_up, ExportReport)

    def test_observe_recovers_before_misclassifying_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="jsonl")
            fresh = _fresh_store(stage)

            observation = fresh.source_binding_monitor.observe()

            self.assertEqual(
                observation.state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(observation.diagnostic_codes, ())
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_refresh_after_crash_recovery_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="jsonl")
            fresh = _fresh_store(stage)

            result = service.refresh_configured_snapshot(fresh)

            self.assertIsInstance(result, ExportReport)
            assert isinstance(result, ExportReport)
            self.assertEqual(result.exported_count, 6)
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 2)
            self.assertEqual(
                tuple(str(row[9]) for row in refresh_rows),
                ("completed", "completed"),
            )
            self.assertEqual(
                fresh.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )


class TMRecoveryIdempotencyTests(unittest.TestCase):
    def test_repeated_recovery_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            os.replace(
                _paths(identity).jsonl_temp,
                identity.configured_jsonl_path,
            )

            first = store.recover_configured_refresh()
            second = store.recover_configured_refresh()
            third = store.recover_configured_refresh()

            self.assertIs(first.state, RefreshRecoveryState.COMPLETED)
            self.assertIs(second.state, RefreshRecoveryState.NOOP)
            self.assertIs(third.state, RefreshRecoveryState.NOOP)
            self.assertEqual(second.diagnostics, ())
            self.assertEqual(third.diagnostics, ())
            refresh_rows = _refresh_rows(
                stage.staged_db_path,
                identity.configured_jsonl_path,
            )
            self.assertEqual(len(refresh_rows), 1)
            self.assertEqual(str(refresh_rows[0][9]), "completed")
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )

    def test_fresh_store_replay_reaches_same_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            os.replace(
                _paths(identity).jsonl_temp,
                identity.configured_jsonl_path,
            )

            first = store.recover_configured_refresh()
            fresh = _fresh_store(stage)
            replay = fresh.recover_configured_refresh()

            self.assertIs(first.state, RefreshRecoveryState.COMPLETED)
            self.assertIs(replay.state, RefreshRecoveryState.NOOP)
            self.assertEqual(
                fresh.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )

    def test_cold_store_without_receipts_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                initialize_stage_schema(stage, canonical_store_id="store.primary")
                store = SQLiteTMStore(stage, canonical_store_id="store.primary")

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.NOOP)
            self.assertEqual(outcome.diagnostics, ())
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            self.assertFalse(
                stage.resource_identity.configured_jsonl_path.exists()
            )

    def test_cold_store_issued_receipt_without_handoff_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _stage(root)
            with patch("tm_sqlite_store._probe_fts5", return_value=False):
                initialize_stage_schema(stage, canonical_store_id="store.primary")
                store = SQLiteTMStore(stage, canonical_store_id="store.primary")
            revision = store.canonical_revision()
            receipt = SnapshotReceipt(
                snapshot_id="snapshot.refresh.cold",
                resource_id=revision.resource_id,
                canonical_store_id=revision.canonical_store_id,
                exported_revision=revision.head_revision,
                jsonl_digest=hashlib.sha256(_NEW_JSONL).hexdigest(),
                record_count=revision.record_count,
            )
            store.register_issued_refresh_receipt(
                receipt,
                expected_generation=store.capture_export_snapshot().revision.generation,
            )

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.HANDOFF_MISSING",),
                    ),
                ),
            )
            self.assertEqual(
                outcome.diagnostics,
                ("RECOVERY.HANDOFF_MISSING",),
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )


class TMRecoveryExportTests(unittest.TestCase):
    def _destination(self, root: Path) -> Path:
        destination = (root / "exports" / "out.jsonl").resolve()
        destination.parent.mkdir(parents=True)
        return destination

    def _register_export(
        self,
        store: SQLiteTMStore,
        destination: Path,
        payload: bytes,
        *,
        jsonl_temp_identity: tuple[int, int] | None = None,
        manifest_temp_identity: tuple[int, int] | None = None,
    ) -> SnapshotReceipt:
        """Register one issued export with a durable handoff journal.

        The deterministic temporaries are created with the exact bytes
        the recovery protocol expects (the exported JSONL payload and
        the deterministic adjacent manifest) so registration's
        descriptor-relative temp proof passes and recovery cleanup can
        prove and remove them by digest plus handoff identity.
        """

        paths = _export_artifact_paths(destination)
        revision = store.capture_export_snapshot().revision
        receipt = SnapshotReceipt(
            snapshot_id=f"snapshot.export.{hashlib.sha256(payload).hexdigest()[:12]}",
            resource_id=revision.resource_id,
            canonical_store_id=revision.canonical_store_id,
            exported_revision=revision.head_revision,
            jsonl_digest=hashlib.sha256(payload).hexdigest(),
            record_count=revision.record_count,
        )
        prior_jsonl = _prior_state(paths.destination)
        prior_manifest = _prior_state(paths.manifest)
        if jsonl_temp_identity is None:
            paths.jsonl_temp.write_bytes(payload)
            jsonl_temp_identity = _identity_of(paths.jsonl_temp)
        if manifest_temp_identity is None:
            paths.manifest_temp.write_bytes(_manifest_bytes_for(receipt))
            manifest_temp_identity = _identity_of(paths.manifest_temp)
        store.register_issued_export_receipt(
            receipt,
            destination_jsonl_path=paths.destination,
            destination_manifest_path=paths.manifest,
            expected_generation=revision.generation,
            jsonl_temp_identity=jsonl_temp_identity,
            manifest_temp_identity=manifest_temp_identity,
            artifact_parent_identity=_identity_of(destination.parent),
            prior_jsonl_identity=prior_jsonl[0],
            prior_jsonl_digest=prior_jsonl[1],
            prior_jsonl_absent=prior_jsonl[2],
            prior_manifest_identity=prior_manifest[0],
            prior_manifest_digest=prior_manifest[1],
            prior_manifest_absent=prior_manifest[2],
        )
        return receipt

    def test_export_full_pair_completes_without_touching_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_pair = _pair(identity)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            _publish_registered_pair(destination, receipt, _NEW_JSONL)


            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.COMPLETED,
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(_pair(identity), prior_pair)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_jsonl_only_reconstructs_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            os.replace(paths.jsonl_temp, paths.destination)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                paths.manifest.read_bytes(),
                _manifest_bytes_for(receipt),
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_old_pair_cancels_issued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            first = self._register_export(store, destination, _NEW_JSONL)
            _publish_registered_pair(destination, first, _NEW_JSONL)

            revision = store.capture_export_snapshot().revision
            store.complete_issued_export_receipt(
                first.snapshot_id,
                expected_generation=revision.generation,
                jsonl_identity=_identity_of(paths.destination),
                manifest_identity=_identity_of(paths.manifest),
            )
            store.recover_configured_refresh()
            second = self._register_export(store, destination, b'{"source":"later"}\n')

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.CANCELLED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        second.snapshot_id,
                        RefreshRecoveryState.CANCELLED,
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, second.snapshot_id),
                "cancelled",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, first.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_binding_tamper_never_cancels_old_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            first = self._register_export(store, destination, _NEW_JSONL)
            _publish_registered_pair(destination, first, _NEW_JSONL)

            revision = store.capture_export_snapshot().revision
            store.complete_issued_export_receipt(
                first.snapshot_id,
                expected_generation=revision.generation,
                jsonl_identity=_identity_of(paths.destination),
                manifest_identity=_identity_of(paths.manifest),
            )
            store.recover_configured_refresh()
            second = self._register_export(
                store,
                destination,
                b'{"source":"pending"}\n',
            )
            prior_pair = _pair(identity)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_snapshot_binding SET snapshot_id = ? "
                    "WHERE binding_id = 1",
                    (second.snapshot_id,),
                )
                connection.commit()
            finally:
                connection.close()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.DIVERGED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        second.snapshot_id,
                        RefreshRecoveryState.DIVERGED,
                        ("RECOVERY.EXPORT_BINDING_INVALID",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.EXPORT_BINDING_INVALID",
                outcome.diagnostics,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, second.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, first.snapshot_id),
                "completed",
            )
            self.assertEqual(_pair(identity), prior_pair)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                second.snapshot_id,
            )

    def test_export_foreign_manifest_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            paths.destination.write_bytes(_NEW_JSONL)
            paths.manifest.write_bytes(b"foreign export manifest\n")

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_MANIFEST_FOREIGN",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_unsafe_destination_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            paths.destination.write_bytes(_NEW_JSONL)
            paths.manifest.write_bytes(b"temporary manifest")
            paths.manifest.unlink()
            paths.manifest.symlink_to(paths.destination)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PAIR_UNSAFE",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_destination_parent_symlink_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            real = (root / "real").resolve()
            real.mkdir()
            link = (root / "link").resolve()
            link.symlink_to(real)
            destination = link / "out.jsonl"
            receipt = _current_receipt(
                store,
                prefix="snapshot.export.",
                payload=_NEW_JSONL,
            )
            _insert_ledger_row(
                stage.staged_db_path,
                receipt,
                destination_jsonl_path=destination,
                destination_manifest_path=(
                    destination.with_name(
                        f"{destination.name}.localcat-snapshot.json"
                    )
                ),
                status="issued",
            )

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PARENT_UNSAFE",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_destination_dotdot_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.export.", payload=_NEW_JSONL)
            _insert_ledger_row(
                stage.staged_db_path,
                receipt,
                destination_jsonl_path=(
                    root / "exports" / ".." / "out.jsonl"
                ),
                destination_manifest_path=(
                    root / "exports" / ".." / "out.jsonl.localcat-snapshot.json"
                ),
                status="issued",
            )

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PATH_INVALID",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_destination_authority_alias_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            fragment = identity.target_identity[:16]
            destination = root / f".localcat-{fragment}.jsonl"
            receipt = self._register_export(store, destination, _NEW_JSONL)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PATH_ALIASED",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_export_same_byte_swap_at_complete_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            _publish_registered_pair(destination, receipt, _NEW_JSONL)

            original_complete = store.complete_issued_export_receipt
            swapped = False

            def hostile_complete(
                snapshot_id: str,
                **kwargs: Any,
            ) -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    foreign = paths.destination.with_name(
                        "foreign-same-bytes.jsonl"
                    )
                    foreign.write_bytes(_NEW_JSONL)
                    os.replace(foreign, paths.destination)
                original_complete(snapshot_id, **kwargs)

            with patch.object(
                store,
                "complete_issued_export_receipt",
                side_effect=hostile_complete,
            ):
                outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertTrue(swapped)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("STORE.RECEIPT_PAIR_INVALID",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )
            self.assertEqual(
                paths.destination.read_bytes(),
                _NEW_JSONL,
            )

    def test_mixed_refresh_and_export_reconcile_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            refresh = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, refresh)
            os.replace(
                _paths(identity).jsonl_temp,
                identity.configured_jsonl_path,
            )
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            export = self._register_export(store, destination, b'{"source":"exported"}\n')
            _publish_registered_pair(
                destination,
                export,
                b'{"source":"exported"}\n',
            )


            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                tuple(item.snapshot_id for item in outcome.receipts),
                (refresh.snapshot_id, export.snapshot_id),
            )
            self.assertEqual(
                tuple(item.state for item in outcome.receipts),
                (
                    RefreshRecoveryState.COMPLETED,
                    RefreshRecoveryState.COMPLETED,
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, refresh.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, export.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                refresh.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )


class TMRecoveryArtifactSafetyTests(unittest.TestCase):
    def test_foreign_temp_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            paths = _paths(identity)
            foreign = b"foreign temp bytes\n"
            paths.jsonl_temp.write_bytes(foreign)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_CONFLICT",),
                    ),
                ),
            )
            self.assertIn("RECOVERY.ARTIFACT_CONFLICT", outcome.diagnostics)
            self.assertEqual(paths.jsonl_temp.read_bytes(), foreign)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + receipt.snapshot_id,
                )
            )
            replay = store.recover_configured_refresh()
            self.assertIs(replay.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(paths.jsonl_temp.read_bytes(), foreign)

    def test_foreign_recovery_copy_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            os.replace(
                _paths(identity).jsonl_temp,
                identity.configured_jsonl_path,
            )
            paths = _paths(identity)
            foreign = b"foreign recovery bytes\n"
            paths.jsonl_recovery.write_bytes(foreign)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_CONFLICT",),
                    ),
                ),
            )
            self.assertIn("RECOVERY.ARTIFACT_CONFLICT", outcome.diagnostics)
            self.assertEqual(paths.jsonl_recovery.read_bytes(), foreign)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + receipt.snapshot_id,
                )
            )
            replay = store.recover_configured_refresh()
            self.assertIs(replay.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(paths.jsonl_recovery.read_bytes(), foreign)

    def test_foreign_manifest_temp_blocks_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            paths = _paths(identity)
            foreign = b"foreign manifest temp\n"
            paths.manifest_temp.write_bytes(foreign)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.MANIFEST_TEMP_CONFLICT",),
                    ),
                ),
            )
            self.assertEqual(paths.manifest_temp.read_bytes(), foreign)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_same_byte_foreign_jsonl_temp_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            paths = _paths(identity)
            foreign = paths.jsonl_temp.with_name("foreign-same-bytes.jsonl")
            foreign.write_bytes(_NEW_JSONL)
            os.replace(foreign, paths.jsonl_temp)
            temp_before = os.lstat(paths.jsonl_temp)
            manifest_before = identity.snapshot_manifest_path.read_bytes()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_UNPROVEN",),
                    ),
                ),
            )
            self.assertEqual(paths.jsonl_temp.read_bytes(), _NEW_JSONL)
            self.assertEqual(
                (
                    os.lstat(paths.jsonl_temp).st_dev,
                    os.lstat(paths.jsonl_temp).st_ino,
                ),
                (temp_before.st_dev, temp_before.st_ino),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                manifest_before,
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_missing_handed_off_manifest_temp_blocks_closed(self) -> None:
        """Absent durable handed-off temp fails closed without recreation.

        A JSONL-only published state whose deterministic manifest temp
        is missing cannot prove the durable handoff inode, so
        reconstruction must fail closed with
        ``RECOVERY.MANIFEST_TEMP_UNPROVEN`` and must never create an
        unjournaled replacement; the destination, receipt and handoff
        stay untouched for a later replay.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(
                store,
                prefix="snapshot.refresh.",
                payload=_NEW_JSONL,
            )
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            paths = _paths(identity)
            paths.manifest_temp.unlink()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.MANIFEST_TEMP_UNPROVEN",),
                    ),
                ),
            )
            self.assertFalse(paths.manifest_temp.exists())
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + receipt.snapshot_id,
                )
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_same_byte_foreign_manifest_temp_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            paths = _paths(identity)
            foreign = _manifest_bytes_for(receipt)
            replacement = paths.manifest_temp.with_name(
                "foreign-same-bytes.manifest"
            )
            replacement.write_bytes(foreign)
            os.replace(replacement, paths.manifest_temp)
            temp_before = os.lstat(paths.manifest_temp)
            manifest_before = identity.snapshot_manifest_path.read_bytes()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_UNPROVEN",),
                    ),
                ),
            )
            self.assertEqual(paths.manifest_temp.read_bytes(), foreign)
            self.assertEqual(
                (
                    os.lstat(paths.manifest_temp).st_dev,
                    os.lstat(paths.manifest_temp).st_ino,
                ),
                (temp_before.st_dev, temp_before.st_ino),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                manifest_before,
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_same_byte_foreign_jsonl_recovery_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            paths = _paths(identity)
            prior_pair = _pair(identity)
            paths.jsonl_recovery.write_bytes(_PRIOR_JSONL)
            recovery_before = os.lstat(paths.jsonl_recovery)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_UNPROVEN",),
                    ),
                ),
            )
            self.assertEqual(paths.jsonl_recovery.read_bytes(), _PRIOR_JSONL)
            self.assertEqual(
                (
                    os.lstat(paths.jsonl_recovery).st_dev,
                    os.lstat(paths.jsonl_recovery).st_ino,
                ),
                (recovery_before.st_dev, recovery_before.st_ino),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(_pair(identity), prior_pair)
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_same_byte_foreign_manifest_recovery_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            paths = _paths(identity)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            paths.manifest_recovery.write_bytes(prior_manifest)
            recovery_before = os.lstat(paths.manifest_recovery)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_UNPROVEN",),
                    ),
                ),
            )
            self.assertEqual(
                paths.manifest_recovery.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                (
                    os.lstat(paths.manifest_recovery).st_dev,
                    os.lstat(paths.manifest_recovery).st_ino,
                ),
                (recovery_before.st_dev, recovery_before.st_ino),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(_pair(identity), (_PRIOR_JSONL, prior_manifest))
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_terminal_replay_different_byte_foreign_temp_blocks_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="manifest")
            snapshot_id = _issued_refresh_snapshot_id(stage.staged_db_path)
            revision = store.capture_export_snapshot().revision
            store.complete_issued_refresh_receipt(
                snapshot_id,
                expected_generation=revision.generation,
                jsonl_identity=_identity_of(identity.configured_jsonl_path),
                manifest_identity=_identity_of(
                    identity.snapshot_manifest_path
                ),
            )
            paths = _paths(identity)
            foreign = b"foreign terminal temp bytes\n"
            paths.jsonl_temp.write_bytes(foreign)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_CONFLICT",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.ARTIFACT_CONFLICT",
                outcome.diagnostics,
            )
            self.assertEqual(paths.jsonl_temp.read_bytes(), foreign)
            self.assertEqual(
                _status_for(stage.staged_db_path, snapshot_id),
                "completed",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + snapshot_id,
                )
            )

    def test_terminal_replay_different_byte_foreign_recovery_blocks_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="manifest")
            snapshot_id = _issued_refresh_snapshot_id(stage.staged_db_path)
            revision = store.capture_export_snapshot().revision
            store.complete_issued_refresh_receipt(
                snapshot_id,
                expected_generation=revision.generation,
                jsonl_identity=_identity_of(identity.configured_jsonl_path),
                manifest_identity=_identity_of(
                    identity.snapshot_manifest_path
                ),
            )
            paths = _paths(identity)
            foreign = b"foreign terminal recovery bytes\n"
            paths.jsonl_recovery.write_bytes(foreign)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_CONFLICT",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.ARTIFACT_CONFLICT",
                outcome.diagnostics,
            )
            self.assertEqual(paths.jsonl_recovery.read_bytes(), foreign)
            self.assertEqual(
                _status_for(stage.staged_db_path, snapshot_id),
                "completed",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + snapshot_id,
                )
            )

    def test_terminal_replay_same_byte_foreign_recovery_blocks_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="manifest")
            snapshot_id = _issued_refresh_snapshot_id(stage.staged_db_path)
            revision = store.capture_export_snapshot().revision
            store.complete_issued_refresh_receipt(
                snapshot_id,
                expected_generation=revision.generation,
                jsonl_identity=_identity_of(identity.configured_jsonl_path),
                manifest_identity=_identity_of(
                    identity.snapshot_manifest_path
                ),
            )
            paths = _paths(identity)
            foreign = paths.jsonl_recovery.with_name("foreign-same-bytes")
            foreign.write_bytes(_PRIOR_JSONL)
            os.replace(foreign, paths.jsonl_recovery)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_UNPROVEN",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.ARTIFACT_UNPROVEN",
                outcome.diagnostics,
            )
            self.assertEqual(paths.jsonl_recovery.read_bytes(), _PRIOR_JSONL)
            self.assertEqual(
                _status_for(stage.staged_db_path, snapshot_id),
                "completed",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + snapshot_id,
                )
            )

    def test_same_byte_foreign_swap_at_complete_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            _write_new_pair(identity, receipt, _NEW_JSONL)
            original_complete = store.complete_issued_refresh_receipt
            swapped = False

            def hostile_complete(
                snapshot_id: str,
                **kwargs: Any,
            ) -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    payload = identity.configured_jsonl_path.read_bytes()
                    foreign = identity.configured_jsonl_path.with_name(
                        "foreign-same-bytes.jsonl"
                    )
                    foreign.write_bytes(payload)
                    os.replace(foreign, identity.configured_jsonl_path)
                original_complete(snapshot_id, **kwargs)

            with patch.object(
                store,
                "complete_issued_refresh_receipt",
                side_effect=hostile_complete,
            ):
                outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertTrue(swapped)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("STORE.REFRESH_PAIR_INVALID",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )


class TMProcessDeathBoundaryTests(unittest.TestCase):
    """True process-death coverage for the shared pair publisher."""

    def test_export_and_refresh_process_death_boundary_catalog(self) -> None:
        for mode in ("refresh", "export"):
            for boundary in SNAPSHOT_PROCESS_DEATH_BOUNDARIES:
                with self.subTest(
                    mode=mode,
                    boundary=boundary.boundary_id,
                ), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    stage, store = _prepared_store(root)
                    identity = stage.resource_identity
                    _bind_current_snapshot(store, stage, _PRIOR_JSONL)
                    configured_before = _pair(identity)
                    binding_before = _binding_row(stage.staged_db_path)
                    canonical_before = store.capture_export_snapshot()
                    if mode == "refresh":
                        destination = identity.configured_jsonl_path
                        prefix = "snapshot.refresh."
                    else:
                        destination = (root / "exports" / "out.jsonl").resolve()
                        destination.parent.mkdir(parents=True)
                        destination.write_bytes(
                            b'{"source":"prior","target":"export"}\n'
                        )
                        destination.with_name(
                            f"{destination.name}.localcat-snapshot.json"
                        ).write_bytes(b'{"manifest":"prior"}\n')
                        prefix = "snapshot.export."
                    paths = _export_artifact_paths(destination)
                    pair_before = (
                        paths.destination.read_bytes(),
                        paths.manifest.read_bytes(),
                    )
                    marker = root / (
                        f"{mode}-{boundary.boundary_id}.marker"
                    )

                    crashed = _run_publication_death_child(
                        stage,
                        mode=mode,
                        seam=boundary.seam,
                        ordinal=boundary.ordinal,
                        destination=destination,
                        marker=marker,
                    )

                    self.assertEqual(
                        crashed.returncode,
                        86,
                        msg=crashed.stdout + crashed.stderr,
                    )
                    self.assertEqual(
                        marker.read_text(encoding="ascii"),
                        boundary.seam,
                    )
                    fresh = _fresh_store(stage)
                    outcome = fresh.recover_configured_refresh()
                    _assert_recovery_outcome_shape(self, outcome)
                    rows = tuple(
                        row
                        for row in _ledger_rows(
                            stage.staged_db_path,
                            destination,
                        )
                        if str(row[0]).startswith(prefix)
                    )
                    artifacts = (
                        paths.jsonl_temp,
                        paths.manifest_temp,
                        paths.jsonl_recovery,
                        paths.manifest_recovery,
                    )

                    if boundary.expected_resolution == "UNJOURNALED":
                        if mode == "refresh":
                            self.assertIs(
                                outcome.state,
                                RefreshRecoveryState.BLOCKED,
                            )
                            self.assertIn(
                                "RECOVERY.HANDOFF_MISSING",
                                outcome.diagnostics,
                            )
                        else:
                            self.assertIs(
                                outcome.state,
                                RefreshRecoveryState.NOOP,
                            )
                        self.assertEqual(rows, ())
                        self.assertTrue(any(path.exists() for path in artifacts))
                        self.assertEqual(
                            (
                                paths.destination.read_bytes(),
                                paths.manifest.read_bytes(),
                            ),
                            pair_before,
                        )
                    else:
                        self.assertEqual(len(rows), 1)
                        snapshot_id = str(rows[0][0])
                        meta_key = "artifact_handoff." + snapshot_id
                        if boundary.expected_resolution == "BLOCKED":
                            self.assertIs(
                                outcome.state,
                                RefreshRecoveryState.BLOCKED,
                            )
                            observed_diagnostics = set(outcome.diagnostics)
                            for receipt_outcome in outcome.receipts:
                                observed_diagnostics.update(
                                    receipt_outcome.diagnostics
                                )
                            self.assertIn(
                                "RECOVERY.ARTIFACT_UNPROVEN",
                                observed_diagnostics,
                            )
                            self.assertEqual(str(rows[0][9]), "issued")
                            self.assertIsNotNone(
                                _meta_value(stage.staged_db_path, meta_key)
                            )
                            self.assertTrue(
                                any(path.exists() for path in artifacts)
                            )
                            self.assertEqual(
                                (
                                    paths.destination.read_bytes(),
                                    paths.manifest.read_bytes(),
                                ),
                                pair_before,
                            )
                        elif boundary.expected_resolution == "CANCELLED":
                            self.assertIs(
                                outcome.state,
                                RefreshRecoveryState.CANCELLED,
                            )
                            self.assertEqual(str(rows[0][9]), "cancelled")
                            self.assertIsNone(
                                _meta_value(stage.staged_db_path, meta_key)
                            )
                            self.assertEqual(
                                (
                                    paths.destination.read_bytes(),
                                    paths.manifest.read_bytes(),
                                ),
                                pair_before,
                            )
                        else:
                            expected_state = (
                                RefreshRecoveryState.NOOP
                                if boundary.expected_resolution
                                == "TERMINAL_NOOP"
                                else RefreshRecoveryState.COMPLETED
                            )
                            self.assertIs(outcome.state, expected_state)
                            self.assertEqual(str(rows[0][9]), "completed")
                            self.assertIsNone(
                                _meta_value(stage.staged_db_path, meta_key)
                            )
                            self.assertNotEqual(
                                (
                                    paths.destination.read_bytes(),
                                    paths.manifest.read_bytes(),
                                ),
                                pair_before,
                            )
                        if boundary.expected_resolution != "BLOCKED":
                            self.assertFalse(
                                any(path.exists() for path in artifacts),
                                tuple(
                                    path.name
                                    for path in artifacts
                                    if path.exists()
                                ),
                            )

                    self.assertEqual(
                        store.capture_export_snapshot(),
                        canonical_before,
                    )
                    binding_after = _binding_row(stage.staged_db_path)
                    if (
                        mode == "refresh"
                        and boundary.expected_resolution
                        in {"COMPLETED", "TERMINAL_NOOP"}
                    ):
                        self.assertTrue(
                            str(binding_after[4]).startswith(prefix)
                        )
                    else:
                        self.assertEqual(binding_after, binding_before)
                    if mode == "export":
                        self.assertEqual(_pair(identity), configured_before)


class TMRecoveryServiceTests(unittest.TestCase):
    def test_service_recovery_delegates_to_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            os.replace(
                _paths(identity).jsonl_temp,
                identity.configured_jsonl_path,
            )

            outcome = service.recover_configured_refresh(store)

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                receipt.snapshot_id,
            )

    def test_service_recovery_normalizes_store_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)

            with patch.object(
                store,
                "recover_configured_refresh",
                side_effect=SQLiteStoreLifecycleError(
                    "STORE.GENERATION_CHANGED",
                    resource_id="tm.primary",
                    generation=7,
                    retryable=True,
                ),
            ):
                outcome = service.recover_configured_refresh(store)

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(outcome.error_code, "STORE.GENERATION_CHANGED")
            self.assertTrue(outcome.retryable)

            with patch.object(
                store,
                "recover_configured_refresh",
                side_effect=SQLiteStoreSchemaError(
                    "STORE.RECEIPT_STALE"
                ),
            ):
                outcome = service.recover_configured_refresh(store)

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(outcome.error_code, "STORE.RECEIPT_STALE")
            self.assertFalse(outcome.retryable)


class TMRecoverySerializationTests(unittest.TestCase):
    def test_recovery_is_reentrant_inside_refresh_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            os.replace(
                _paths(identity).jsonl_temp,
                identity.configured_jsonl_path,
            )

            with store.configured_refresh_reservation():
                outcome = store.recover_configured_refresh()

            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_recovery_serializes_with_held_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            os.replace(
                _paths(identity).jsonl_temp,
                identity.configured_jsonl_path,
            )
            held = threading.Event()
            release = threading.Event()
            holder_error: list[Exception] = []

            def holder() -> None:
                try:
                    with store.configured_refresh_reservation():
                        held.set()
                        if not release.wait(10):
                            raise AssertionError("holder never released")
                except Exception as error:  # pragma: no cover - failure path
                    holder_error.append(error)

            results: list[RefreshRecoveryOutcome] = []
            runner_error: list[Exception] = []

            def runner() -> None:
                try:
                    results.append(store.recover_configured_refresh())
                except Exception as error:  # pragma: no cover - failure path
                    runner_error.append(error)

            holder_thread = threading.Thread(target=holder)
            runner_thread = threading.Thread(target=runner)
            holder_thread.start()
            if not held.wait(10):
                raise AssertionError("reservation never held")
            runner_thread.start()
            time.sleep(0.2)
            self.assertTrue(runner_thread.is_alive(), "recovery bypassed the gate")
            release.set()
            holder_thread.join(30)
            runner_thread.join(30)
            if holder_thread.is_alive() or runner_thread.is_alive():
                raise AssertionError("serialization threads never finished")
            self.assertEqual(holder_error, [])
            self.assertEqual(runner_error, [])
            self.assertEqual(len(results), 1)
            self.assertIs(results[0].state, RefreshRecoveryState.COMPLETED)


class TMRecoveryCanonicalInvariantTests(unittest.TestCase):
    def test_recovery_never_changes_canonical_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(store, prefix="snapshot.refresh.", payload=_NEW_JSONL)
            _register_refresh(store, receipt)
            os.replace(
                _paths(identity).jsonl_temp,
                identity.configured_jsonl_path,
            )
            before_snapshot = store.capture_export_snapshot()
            before_records = _record_dump(stage.staged_db_path)
            before_head = _meta_value(stage.staged_db_path, "head_revision")
            before_generation = store.coordinator.current_generation

            outcome = store.recover_configured_refresh()

            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            after_snapshot = store.capture_export_snapshot()
            self.assertEqual(after_snapshot.revision, before_snapshot.revision)
            self.assertEqual(after_snapshot.records, before_snapshot.records)
            self.assertEqual(_record_dump(stage.staged_db_path), before_records)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "head_revision"),
                before_head,
            )
            self.assertEqual(
                store.coordinator.current_generation,
                before_generation,
            )


class TMClusterFRegressionTests(unittest.TestCase):
    """Cluster F regressions: durable handoff, prior records, closure."""

    def _destination(self, root: Path) -> Path:
        destination = (root / "exports" / "out.jsonl").resolve()
        destination.parent.mkdir(parents=True)
        return destination


    def _register_export(
        self,
        store: SQLiteTMStore,
        destination: Path,
        payload: bytes,
    ) -> SnapshotReceipt:
        """Register one issued export with a durable handoff journal.

        The deterministic temporaries are created with the exact bytes
        the recovery protocol expects (the exported JSONL payload and
        the deterministic adjacent manifest) so registration's
        descriptor-relative temp proof passes and recovery cleanup can
        prove and remove them by digest plus handoff identity.
        """

        paths = _export_artifact_paths(destination)
        revision = store.capture_export_snapshot().revision
        receipt = SnapshotReceipt(
            snapshot_id=(
                "snapshot.export."
                + hashlib.sha256(payload).hexdigest()[:12]
            ),
            resource_id=revision.resource_id,
            canonical_store_id=revision.canonical_store_id,
            exported_revision=revision.head_revision,
            jsonl_digest=hashlib.sha256(payload).hexdigest(),
            record_count=revision.record_count,
        )
        prior_jsonl = _prior_state(paths.destination)
        prior_manifest = _prior_state(paths.manifest)
        paths.jsonl_temp.write_bytes(payload)
        jsonl_temp_identity = _identity_of(paths.jsonl_temp)
        paths.manifest_temp.write_bytes(_manifest_bytes_for(receipt))
        manifest_temp_identity = _identity_of(paths.manifest_temp)
        store.register_issued_export_receipt(
            receipt,
            destination_jsonl_path=paths.destination,
            destination_manifest_path=paths.manifest,
            expected_generation=revision.generation,
            jsonl_temp_identity=jsonl_temp_identity,
            manifest_temp_identity=manifest_temp_identity,
            artifact_parent_identity=_identity_of(destination.parent),
            prior_jsonl_identity=prior_jsonl[0],
            prior_jsonl_digest=prior_jsonl[1],
            prior_jsonl_absent=prior_jsonl[2],
            prior_manifest_identity=prior_manifest[0],
            prior_manifest_digest=prior_manifest[1],
            prior_manifest_absent=prior_manifest[2],
        )
        return receipt

    def _complete_export(
        self,
        store: SQLiteTMStore,
        receipt: SnapshotReceipt,
        destination: Path,
        *,
        expected_generation: int,
    ) -> None:
        """Complete one issued export with the live published pair."""

        paths = _export_artifact_paths(destination)
        store.complete_issued_export_receipt(
            receipt.snapshot_id,
            expected_generation=expected_generation,
            jsonl_identity=_identity_of(paths.destination),
            manifest_identity=_identity_of(paths.manifest),
        )

    def test_corrupt_issued_and_terminal_handoffs_block_without_mutation(
        self,
    ) -> None:
        """Every malformed durable handoff is explicit fail-stop evidence."""

        def corruptions(raw: str) -> dict[str, str]:
            payload = json.loads(raw)
            bool_version = dict(payload)
            bool_version["version"] = True
            unknown_field = dict(payload)
            unknown_field["unexpected"] = None
            missing_field = dict(payload)
            del missing_field["manifest_temp_inode"]
            partial_identity = dict(payload)
            partial_identity["jsonl_temp_inode"] = None
            invalid_prior_digest = dict(payload)
            invalid_prior_digest["prior_jsonl_digest"] = "g" * 64
            nonfinite = dict(payload)
            nonfinite["artifact_parent_device"] = float("nan")
            return {
                "malformed": "{",
                "duplicate": raw.replace(
                    '"version":1',
                    '"version":1,"version":1',
                    1,
                ),
                "bool-version": json.dumps(bool_version),
                "unknown-field": json.dumps(unknown_field),
                "missing-field": json.dumps(missing_field),
                "partial-identity": json.dumps(partial_identity),
                "invalid-prior-digest": json.dumps(invalid_prior_digest),
                "nonfinite": json.dumps(nonfinite),
            }

        for receipt_status in ("issued", "completed"):
            for corruption_name in (
                "malformed",
                "duplicate",
                "bool-version",
                "unknown-field",
                "missing-field",
                "partial-identity",
                "invalid-prior-digest",
                "nonfinite",
            ):
                with self.subTest(
                    status=receipt_status,
                    corruption=corruption_name,
                ), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    stage, store = _prepared_store(root)
                    destination = self._destination(root)
                    receipt = self._register_export(
                        store,
                        destination,
                        _NEW_JSONL,
                    )
                    if receipt_status != "issued":
                        _set_receipt_status(
                            stage.staged_db_path,
                            receipt.snapshot_id,
                            receipt_status,
                        )
                    meta_key = "artifact_handoff." + receipt.snapshot_id
                    original = _meta_value(stage.staged_db_path, meta_key)
                    self.assertIsNotNone(original)
                    assert original is not None
                    _set_meta_value(
                        stage.staged_db_path,
                        meta_key,
                        corruptions(original)[corruption_name],
                    )
                    paths = _export_artifact_paths(destination)
                    assets_before = {
                        path: (path.read_bytes(), _identity_of(path))
                        for path in (
                            paths.destination,
                            paths.manifest,
                            paths.jsonl_temp,
                            paths.manifest_temp,
                            paths.jsonl_recovery,
                            paths.manifest_recovery,
                        )
                        if path.exists()
                    }
                    canonical_before = store.capture_export_snapshot()

                    outcome = _fresh_store(stage).recover_configured_refresh()

                    _assert_recovery_outcome_shape(self, outcome)
                    self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
                    self.assertEqual(
                        outcome.error_code,
                        "STORE.HANDOFF_CORRUPT",
                    )
                    self.assertIn(
                        "STORE.HANDOFF_CORRUPT",
                        outcome.diagnostics,
                    )
                    self.assertEqual(
                        _status_for(
                            stage.staged_db_path,
                            receipt.snapshot_id,
                        ),
                        receipt_status,
                    )
                    self.assertIsNotNone(
                        _meta_value(stage.staged_db_path, meta_key)
                    )
                    for path, expected in assets_before.items():
                        self.assertEqual(
                            (path.read_bytes(), _identity_of(path)),
                            expected,
                            path.name,
                        )
                    self.assertEqual(
                        store.capture_export_snapshot(),
                        canonical_before,
                    )

    def _assert_deleted_or_orphaned_handoff_blocks(
        self,
        receipt_status: str,
    ) -> None:
        for mutation in ("deleted", "orphaned"):
            with self.subTest(
                status=receipt_status,
                mutation=mutation,
            ), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage, store = _prepared_store(root)
                identity = stage.resource_identity
                _bind_current_snapshot(store, stage, _PRIOR_JSONL)
                destination = self._destination(root)
                paths = _export_artifact_paths(destination)
                prior_jsonl = b'{"source":"prior","target":"pair"}\n'
                prior_manifest = b'{"manifest":"prior"}\n'
                destination.write_bytes(prior_jsonl)
                paths.manifest.write_bytes(prior_manifest)
                receipt = self._register_export(
                    store,
                    destination,
                    _NEW_JSONL,
                )
                generation = store.capture_export_snapshot().revision.generation
                paths.jsonl_recovery.write_bytes(prior_jsonl)
                paths.manifest_recovery.write_bytes(prior_manifest)
                store.record_export_recovery_handoff(
                    receipt.snapshot_id,
                    expected_generation=generation,
                    jsonl_recovery_identity=_identity_of(
                        paths.jsonl_recovery
                    ),
                    manifest_recovery_identity=_identity_of(
                        paths.manifest_recovery
                    ),
                )
                if receipt_status == "completed":
                    _publish_registered_pair(
                        destination,
                        receipt,
                        _NEW_JSONL,
                    )
                    self._complete_export(
                        store,
                        receipt,
                        destination,
                        expected_generation=generation,
                    )
                elif receipt_status == "cancelled":
                    store.cancel_issued_export_receipt(
                        receipt.snapshot_id,
                        expected_generation=generation,
                    )
                elif receipt_status != "issued":
                    raise AssertionError("unsupported receipt status")
                meta_key = "artifact_handoff." + receipt.snapshot_id
                connection = sqlite3.connect(stage.staged_db_path)
                try:
                    if mutation == "deleted":
                        changed = connection.execute(
                            "DELETE FROM tm_meta WHERE key = ?",
                            (meta_key,),
                        )
                    else:
                        changed = connection.execute(
                            "UPDATE tm_meta SET key = ? WHERE key = ?",
                            ("artifact_handoff.orphan", meta_key),
                        )
                    self.assertEqual(changed.rowcount, 1)
                    connection.commit()
                finally:
                    connection.close()
                assets_before = {
                    path: (path.read_bytes(), _identity_of(path))
                    for path in (
                        paths.destination,
                        paths.manifest,
                        paths.jsonl_temp,
                        paths.manifest_temp,
                        paths.jsonl_recovery,
                        paths.manifest_recovery,
                    )
                    if path.exists()
                }
                binding_before = _binding_row(stage.staged_db_path)
                canonical_before = store.capture_export_snapshot()

                outcome = _fresh_store(stage).recover_configured_refresh()

                _assert_recovery_outcome_shape(self, outcome)
                self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
                if mutation == "orphaned":
                    self.assertEqual(
                        outcome.error_code,
                        "STORE.HANDOFF_ORPHANED",
                    )
                    self.assertIn(
                        "STORE.HANDOFF_ORPHANED",
                        outcome.diagnostics,
                    )
                    self.assertEqual(outcome.receipts, ())
                    self.assertIsNotNone(
                        _meta_value(
                            stage.staged_db_path,
                            "artifact_handoff.orphan",
                        )
                    )
                else:
                    self.assertIsNone(outcome.error_code)
                    self.assertIn(
                        "RECOVERY.HANDOFF_MISSING",
                        outcome.diagnostics,
                    )
                    self.assertEqual(
                        outcome.receipts,
                        (
                            IssuedReceiptRecovery(
                                receipt.snapshot_id,
                                RefreshRecoveryState.BLOCKED,
                                ("RECOVERY.HANDOFF_MISSING",),
                            ),
                        ),
                    )
                self.assertEqual(
                    _status_for(stage.staged_db_path, receipt.snapshot_id),
                    receipt_status,
                )
                for path, expected in assets_before.items():
                    self.assertEqual(
                        (path.read_bytes(), _identity_of(path)),
                        expected,
                        path.name,
                    )
                self.assertEqual(
                    _binding_row(stage.staged_db_path),
                    binding_before,
                )
                self.assertEqual(
                    store.capture_export_snapshot(),
                    canonical_before,
                )

    def test_issued_deleted_or_orphaned_handoff_blocks(self) -> None:
        self._assert_deleted_or_orphaned_handoff_blocks("issued")

    def test_configured_issued_missing_handoff_and_all_artifacts_absent_blocks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(
                store,
                prefix="snapshot.refresh.",
                payload=_NEW_JSONL,
            )
            _register_refresh(store, receipt)
            paths = _paths(identity)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                artifact.unlink(missing_ok=True)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                changed = connection.execute(
                    "DELETE FROM tm_meta WHERE key = ?",
                    ("artifact_handoff." + receipt.snapshot_id,),
                )
                self.assertEqual(changed.rowcount, 1)
                connection.commit()
            finally:
                connection.close()
            pair_before = {
                path: (path.read_bytes(), _identity_of(path))
                for path in (
                    identity.configured_jsonl_path,
                    identity.snapshot_manifest_path,
                )
            }
            binding_before = _binding_row(stage.staged_db_path)
            canonical_before = store.capture_export_snapshot()

            outcome = _fresh_store(stage).recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertIsNone(outcome.error_code)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.HANDOFF_MISSING",),
                    ),
                ),
            )
            self.assertEqual(
                outcome.diagnostics,
                ("RECOVERY.HANDOFF_MISSING",),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertTrue(
                all(
                    not artifact.exists()
                    for artifact in (
                        paths.jsonl_temp,
                        paths.manifest_temp,
                        paths.jsonl_recovery,
                        paths.manifest_recovery,
                    )
                )
            )
            for path, expected in pair_before.items():
                self.assertEqual(
                    (path.read_bytes(), _identity_of(path)),
                    expected,
                    path.name,
                )
            self.assertEqual(
                _binding_row(stage.staged_db_path),
                binding_before,
            )
            self.assertEqual(
                store.capture_export_snapshot(),
                canonical_before,
            )

    def test_export_issued_missing_handoff_and_all_artifacts_absent_blocks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            destination.write_bytes(b'{"source":"prior","target":"pair"}\n')
            paths.manifest.write_bytes(b'{"manifest":"prior"}\n')
            receipt = self._register_export(store, destination, _NEW_JSONL)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                artifact.unlink(missing_ok=True)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                changed = connection.execute(
                    "DELETE FROM tm_meta WHERE key = ?",
                    ("artifact_handoff." + receipt.snapshot_id,),
                )
                self.assertEqual(changed.rowcount, 1)
                connection.commit()
            finally:
                connection.close()
            pair_before = {
                path: (path.read_bytes(), _identity_of(path))
                for path in (paths.destination, paths.manifest)
            }
            binding_before = _binding_row(stage.staged_db_path)
            canonical_before = store.capture_export_snapshot()

            outcome = _fresh_store(stage).recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertIsNone(outcome.error_code)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.HANDOFF_MISSING",),
                    ),
                ),
            )
            self.assertEqual(
                outcome.diagnostics,
                ("RECOVERY.HANDOFF_MISSING",),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertTrue(
                all(
                    not artifact.exists()
                    for artifact in (
                        paths.jsonl_temp,
                        paths.manifest_temp,
                        paths.jsonl_recovery,
                        paths.manifest_recovery,
                    )
                )
            )
            for path, expected in pair_before.items():
                self.assertEqual(
                    (path.read_bytes(), _identity_of(path)),
                    expected,
                    path.name,
                )
            self.assertEqual(
                _binding_row(stage.staged_db_path),
                binding_before,
            )
            self.assertEqual(
                store.capture_export_snapshot(),
                canonical_before,
            )

    def test_completed_deleted_or_orphaned_handoff_blocks(self) -> None:
        self._assert_deleted_or_orphaned_handoff_blocks("completed")

    def test_cancelled_deleted_or_orphaned_handoff_blocks(self) -> None:
        self._assert_deleted_or_orphaned_handoff_blocks("cancelled")

    def test_terminal_without_handoff_and_artifacts_is_legitimate_noop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)

            exported = _service(identity).export_jsonl(store, destination)

            self.assertIs(type(exported), ExportReport)
            rows = tuple(
                row
                for row in _ledger_rows(stage.staged_db_path, destination)
                if str(row[0]).startswith("snapshot.export.")
            )
            self.assertEqual(len(rows), 1)
            snapshot_id = str(rows[0][0])
            self.assertEqual(str(rows[0][9]), "completed")
            paths = _export_artifact_paths(destination)
            self.assertTrue(
                all(
                    not path.exists()
                    for path in (
                        paths.jsonl_temp,
                        paths.manifest_temp,
                        paths.jsonl_recovery,
                        paths.manifest_recovery,
                    )
                )
            )
            self.assertIsNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + snapshot_id,
                )
            )

            outcome = _fresh_store(stage).recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.NOOP)
            self.assertEqual(outcome.receipts, ())
            self.assertEqual(outcome.diagnostics, ())

    def test_export_issued_to_reserved_authority_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            terminal = _activation_terminal_path(identity)
            configured = _export_artifact_paths(
                identity.configured_jsonl_path
            )
            reserved = (
                terminal,
                _activation_terminal_temp_path(terminal),
                configured.jsonl_temp,
                configured.manifest_temp,
                configured.jsonl_recovery,
                configured.manifest_recovery,
            )
            for destination in reserved:
                with self.subTest(destination=destination.name):
                    manifest = destination.with_name(
                        f"{destination.name}.localcat-snapshot.json"
                    )
                    revision = store.capture_export_snapshot().revision
                    receipt = SnapshotReceipt(
                        snapshot_id=(
                            "snapshot.export."
                            + hashlib.sha256(
                                destination.name.encode("utf-8")
                            ).hexdigest()[:24]
                        ),
                        resource_id=revision.resource_id,
                        canonical_store_id=revision.canonical_store_id,
                        exported_revision=revision.head_revision,
                        jsonl_digest=hashlib.sha256(
                            _NEW_JSONL
                        ).hexdigest(),
                        record_count=revision.record_count,
                    )
                    _insert_ledger_row(
                        stage.staged_db_path,
                        receipt,
                        destination_jsonl_path=destination,
                        destination_manifest_path=manifest,
                        status="issued",
                    )
                    outcome = store.recover_configured_refresh()
                    _assert_recovery_outcome_shape(self, outcome)
                    self.assertIs(
                        outcome.state,
                        RefreshRecoveryState.BLOCKED,
                    )
                    self.assertTrue(
                        any(
                            result.snapshot_id == receipt.snapshot_id
                            and result.state
                            is RefreshRecoveryState.BLOCKED
                            and result.diagnostics
                            == ("RECOVERY.EXPORT_PATH_ALIASED",)
                            for result in outcome.receipts
                        ),
                        outcome.receipts,
                    )
                    self.assertEqual(
                        _status_for(
                            stage.staged_db_path,
                            receipt.snapshot_id,
                        ),
                        "issued",
                    )

    def test_same_byte_foreign_manifest_blocks_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(
                store,
                prefix="snapshot.refresh.",
                payload=_NEW_JSONL,
            )
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            prior_manifest = identity.snapshot_manifest_path.read_bytes()
            swap = identity.snapshot_manifest_path.with_name(
                "swap-manifest.json"
            )
            swap.write_bytes(prior_manifest)
            os.replace(swap, identity.snapshot_manifest_path)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertIn(
                "RECOVERY.MANIFEST_DESTINATION_UNPROVEN",
                outcome.diagnostics,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )

    def test_manifest_slot_absent_without_explicit_absence_never_completes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            receipt = _current_receipt(
                store,
                prefix="snapshot.refresh.",
                payload=_NEW_JSONL,
            )
            _register_refresh(store, receipt)
            identity.configured_jsonl_path.write_bytes(_NEW_JSONL)
            identity.snapshot_manifest_path.unlink()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertIn(
                "RECOVERY.MANIFEST_DESTINATION_UNPROVEN",
                outcome.diagnostics,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                _NEW_JSONL,
            )
            self.assertFalse(
                identity.snapshot_manifest_path.exists()
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    f"artifact_handoff.{receipt.snapshot_id}",
                )
            )

    def test_cancelled_export_prior_never_cancels_later_issued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            revision = store.capture_export_snapshot().revision
            first = SnapshotReceipt(
                snapshot_id="snapshot.export.cancelled-a",
                resource_id=revision.resource_id,
                canonical_store_id=revision.canonical_store_id,
                exported_revision=revision.head_revision,
                jsonl_digest=hashlib.sha256(_NEW_JSONL).hexdigest(),
                record_count=revision.record_count,
            )
            paths.destination.write_bytes(_NEW_JSONL)
            paths.manifest.write_bytes(_manifest_bytes_for(first))
            _insert_ledger_row(
                stage.staged_db_path,
                first,
                destination_jsonl_path=paths.destination,
                destination_manifest_path=paths.manifest,
                status="issued",
            )
            store.cancel_issued_export_receipt(
                first.snapshot_id,
                expected_generation=revision.generation,
            )
            second = SnapshotReceipt(
                snapshot_id="snapshot.export.issued-b",
                resource_id=revision.resource_id,
                canonical_store_id=revision.canonical_store_id,
                exported_revision=revision.head_revision,
                jsonl_digest=hashlib.sha256(
                    b'{"source":"pending"}\n'
                ).hexdigest(),
                record_count=revision.record_count,
            )
            _insert_ledger_row(
                stage.staged_db_path,
                second,
                destination_jsonl_path=paths.destination,
                destination_manifest_path=paths.manifest,
                status="issued",
            )

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        second.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.HANDOFF_MISSING",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, first.snapshot_id),
                "cancelled",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, second.snapshot_id),
                "issued",
            )
            self.assertEqual(paths.destination.read_bytes(), _NEW_JSONL)
            self.assertEqual(
                paths.manifest.read_bytes(),
                _manifest_bytes_for(first),
            )

    def test_terminal_replay_foreign_victim_inode_blocks_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="manifest")
            snapshot_id = _issued_refresh_snapshot_id(stage.staged_db_path)
            revision = store.capture_export_snapshot().revision
            store.complete_issued_refresh_receipt(
                snapshot_id,
                expected_generation=revision.generation,
                jsonl_identity=_identity_of(identity.configured_jsonl_path),
                manifest_identity=_identity_of(
                    identity.snapshot_manifest_path
                ),
            )
            paths = _paths(identity)
            payload = identity.configured_jsonl_path.read_bytes()
            foreign = paths.jsonl_temp.with_name("foreign-victim.jsonl")
            foreign.write_bytes(payload)
            os.replace(foreign, paths.jsonl_temp)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_UNPROVEN",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.ARTIFACT_UNPROVEN",
                outcome.diagnostics,
            )
            self.assertEqual(
                paths.jsonl_temp.read_bytes(),
                payload,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, snapshot_id),
                "completed",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + snapshot_id,
                )
            )

    def test_terminal_replay_parent_symlink_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            _publish_registered_pair(destination, receipt, _NEW_JSONL)

            paths.jsonl_recovery.write_bytes(prior_jsonl)
            paths.manifest_recovery.write_bytes(prior_manifest)
            store.record_export_recovery_handoff(
                receipt.snapshot_id,
                expected_generation=generation,
                jsonl_recovery_identity=_identity_of(paths.jsonl_recovery),
                manifest_recovery_identity=_identity_of(
                    paths.manifest_recovery
                ),
            )
            self._complete_export(
                store,
                receipt,
                destination,
                expected_generation=generation,
            )
            real_parent = (root / "exports-real").resolve()
            destination.parent.rename(real_parent)
            os.symlink(real_parent, destination.parent)

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PARENT_UNSAFE",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.EXPORT_PARENT_UNSAFE",
                outcome.diagnostics,
            )
            self.assertEqual(
                paths.jsonl_recovery.read_bytes(),
                prior_jsonl,
            )
            self.assertEqual(
                paths.manifest_recovery.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(paths.destination.read_bytes(), _NEW_JSONL)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + receipt.snapshot_id,
                )
            )

    def test_terminal_replay_identity_invalid_blocks_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            _publish_registered_pair(destination, receipt, _NEW_JSONL)

            self._complete_export(
                store,
                receipt,
                destination,
                expected_generation=generation,
            )
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_snapshot_receipt SET "
                    "canonical_store_id = ? WHERE snapshot_id = ?",
                    ("store.foreign", receipt.snapshot_id),
                )
                connection.commit()
            finally:
                connection.close()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_LEDGER_IDENTITY_INVALID",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.EXPORT_LEDGER_IDENTITY_INVALID",
                outcome.diagnostics,
            )
            self.assertEqual(paths.destination.read_bytes(), _NEW_JSONL)
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + receipt.snapshot_id,
                )
            )

    def test_terminal_replay_fsync_failure_keeps_handoff_then_replays(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="manifest")
            snapshot_id = _issued_refresh_snapshot_id(stage.staged_db_path)
            revision = store.capture_export_snapshot().revision
            store.complete_issued_refresh_receipt(
                snapshot_id,
                expected_generation=revision.generation,
                jsonl_identity=_identity_of(identity.configured_jsonl_path),
                manifest_identity=_identity_of(
                    identity.snapshot_manifest_path
                ),
            )
            paths = _paths(identity)
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                if artifact.exists():
                    artifact.unlink()
            original_fsync = tm_snapshot_recovery._fsync_artifact_parent
            original_clear = store.clear_issued_receipt_handoff
            events: list[str] = []

            def failing_first_fsync(
                destination: Path,
                expected_identity: tuple[int, int] | None,
                parent: Any = None,
            ) -> None:
                events.append("fsync")
                raise OSError("injected first replay fsync failure")

            def recording_clear(snapshot_id: str, **kwargs: Any) -> None:
                events.append("clear")
                original_clear(snapshot_id, **kwargs)

            with patch(
                "tm_snapshot_recovery._fsync_artifact_parent",
                side_effect=failing_first_fsync,
            ), patch.object(
                store,
                "clear_issued_receipt_handoff",
                side_effect=recording_clear,
            ):
                first = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, first)
            self.assertIs(first.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                first.receipts,
                (
                    IssuedReceiptRecovery(
                        snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.IO_FAILED",),
                    ),
                ),
            )
            self.assertIn("RECOVERY.IO_FAILED", first.diagnostics)
            self.assertEqual(events, ["fsync"])
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)
            self.assertEqual(
                _status_for(stage.staged_db_path, snapshot_id),
                "completed",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + snapshot_id,
                )
            )

            events.clear()
            with patch(
                "tm_snapshot_recovery._fsync_artifact_parent",
                side_effect=lambda destination, expected_identity, parent=None: (
                    events.append("fsync")
                    or original_fsync(
                        destination,
                        expected_identity,
                        parent=parent,
                    )
                ),
            ), patch.object(
                store,
                "clear_issued_receipt_handoff",
                side_effect=recording_clear,
            ):
                second = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, second)
            self.assertIs(second.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                second.receipts,
                (
                    IssuedReceiptRecovery(
                        snapshot_id,
                        RefreshRecoveryState.COMPLETED,
                    ),
                ),
            )
            self.assertEqual(second.diagnostics, ())
            self.assertEqual(events, ["fsync", "clear"])
            self.assertIsNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + snapshot_id,
                )
            )
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)
            follow_up = service.refresh_configured_snapshot(
                _fresh_store(stage)
            )
            self.assertIsInstance(follow_up, ExportReport)

    def test_export_release_fsync_failure_blocks_then_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            _publish_registered_pair(destination, receipt, _NEW_JSONL)

            original_fsync = tm_snapshot_recovery._fsync_artifact_parent
            original_clear = store.clear_issued_receipt_handoff
            events: list[str] = []

            def failing_release_fsync(
                destination_path: Path,
                expected_identity: tuple[int, int] | None,
                parent: Any = None,
            ) -> None:
                events.append("fsync")
                raise OSError("injected release fsync failure")

            def recording_clear(snapshot_id: str, **kwargs: Any) -> None:
                events.append("clear")
                original_clear(snapshot_id, **kwargs)

            with patch(
                "tm_snapshot_recovery._fsync_artifact_parent",
                side_effect=failing_release_fsync,
            ), patch.object(
                store,
                "clear_issued_receipt_handoff",
                side_effect=recording_clear,
            ):
                first = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, first)
            self.assertIs(first.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                first.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_IO_FAILED",),
                    ),
                ),
            )
            self.assertEqual(
                first.receipts[0].diagnostics,
                ("RECOVERY.EXPORT_IO_FAILED",),
            )
            self.assertEqual(events, ["fsync"])
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + receipt.snapshot_id,
                )
            )

            events.clear()
            with patch(
                "tm_snapshot_recovery._fsync_artifact_parent",
                side_effect=lambda destination_path, expected_identity, parent=None: (
                    events.append("fsync")
                    or original_fsync(
                        destination_path,
                        expected_identity,
                        parent=parent,
                    )
                ),
            ), patch.object(
                store,
                "clear_issued_receipt_handoff",
                side_effect=recording_clear,
            ):
                second = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, second)
            self.assertIs(second.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                second.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.COMPLETED,
                    ),
                ),
            )
            self.assertEqual(second.diagnostics, ())
            self.assertEqual(events, ["fsync", "clear"])
            self.assertIsNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + receipt.snapshot_id,
                )
            )
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)

    def test_terminal_handoff_replay_cleans_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            original_complete = store.complete_issued_refresh_receipt

            def commit_then_raise(
                snapshot_id: str,
                **kwargs: Any,
            ) -> None:
                original_complete(snapshot_id, **kwargs)
                raise SQLiteStoreLifecycleError(
                    "STORE.LEDGER_UNAVAILABLE",
                    resource_id="tm.primary",
                    generation=0,
                    retryable=True,
                )

            with patch.object(
                store,
                "complete_issued_refresh_receipt",
                side_effect=commit_then_raise,
            ):
                result = service.refresh_configured_snapshot(store)

            assert isinstance(result, ExportFailure)
            snapshot_id = _refresh_snapshot_id(stage.staged_db_path)
            self.assertEqual(
                _status_for(stage.staged_db_path, snapshot_id),
                "completed",
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    f"artifact_handoff.{snapshot_id}",
                )
            )
            paths = _paths(identity)
            for artifact in (
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertTrue(artifact.exists(), artifact.name)
            fresh = _fresh_store(stage)

            replay = fresh.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, replay)
            self.assertIs(replay.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                replay.receipts,
                (
                    IssuedReceiptRecovery(
                        snapshot_id,
                        RefreshRecoveryState.COMPLETED,
                    ),
                ),
            )
            self.assertEqual(replay.diagnostics, ())
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)
            self.assertIsNone(
                _meta_value(
                    stage.staged_db_path,
                    f"artifact_handoff.{snapshot_id}",
                )
            )
            follow_up = service.refresh_configured_snapshot(fresh)
            self.assertIsInstance(follow_up, ExportReport)

    def test_terminal_replay_parent_replaced_after_blocker_blocks_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            _publish_registered_pair(destination, receipt, _NEW_JSONL)

            paths.jsonl_recovery.write_bytes(prior_jsonl)
            paths.manifest_recovery.write_bytes(prior_manifest)
            store.record_export_recovery_handoff(
                receipt.snapshot_id,
                expected_generation=generation,
                jsonl_recovery_identity=_identity_of(paths.jsonl_recovery),
                manifest_recovery_identity=_identity_of(
                    paths.manifest_recovery
                ),
            )
            self._complete_export(
                store,
                receipt,
                destination,
                expected_generation=generation,
            )
            meta_key = "artifact_handoff." + receipt.snapshot_id
            original_blocker = tm_snapshot_recovery._terminal_handoff_row_blocker
            renamed = (root / "exports-renamed").resolve()

            def blocker_then_replace_parent(
                facts: Any,
                terminal_receipt: Any,
            ) -> Any:
                blocker, parent_handle = original_blocker(
                    facts,
                    terminal_receipt,
                )
                if blocker is None:
                    assert parent_handle is not None
                    old_parent = destination.parent
                    old_parent.rename(renamed)
                    old_parent.mkdir()
                return blocker, parent_handle

            with patch(
                "tm_snapshot_recovery._terminal_handoff_row_blocker",
                side_effect=blocker_then_replace_parent,
            ):
                outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PARENT_REPLACED",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.EXPORT_PARENT_REPLACED",
                outcome.diagnostics,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertIsNotNone(_meta_value(stage.staged_db_path, meta_key))
            stranded = _export_artifact_paths(renamed / "out.jsonl")
            self.assertEqual(
                (renamed / "out.jsonl").read_bytes(),
                _NEW_JSONL,
            )
            self.assertEqual(
                (renamed / "out.jsonl.localcat-snapshot.json").read_bytes(),
                _manifest_bytes_for(receipt),
            )
            self.assertEqual(
                stranded.jsonl_recovery.read_bytes(),
                prior_jsonl,
            )
            self.assertEqual(
                stranded.manifest_recovery.read_bytes(),
                prior_manifest,
            )

    def test_terminal_replay_parent_symlink_replaced_after_blocker_blocks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            _publish_registered_pair(destination, receipt, _NEW_JSONL)

            paths.jsonl_recovery.write_bytes(prior_jsonl)
            paths.manifest_recovery.write_bytes(prior_manifest)
            store.record_export_recovery_handoff(
                receipt.snapshot_id,
                expected_generation=generation,
                jsonl_recovery_identity=_identity_of(paths.jsonl_recovery),
                manifest_recovery_identity=_identity_of(
                    paths.manifest_recovery
                ),
            )
            self._complete_export(
                store,
                receipt,
                destination,
                expected_generation=generation,
            )
            meta_key = "artifact_handoff." + receipt.snapshot_id
            original_blocker = tm_snapshot_recovery._terminal_handoff_row_blocker
            renamed = (root / "exports-real").resolve()

            def blocker_then_symlink_parent(
                facts: Any,
                terminal_receipt: Any,
            ) -> Any:
                blocker, parent_handle = original_blocker(
                    facts,
                    terminal_receipt,
                )
                if blocker is None:
                    assert parent_handle is not None
                    old_parent = destination.parent
                    old_parent.rename(renamed)
                    os.symlink(renamed, old_parent)
                return blocker, parent_handle

            with patch(
                "tm_snapshot_recovery._terminal_handoff_row_blocker",
                side_effect=blocker_then_symlink_parent,
            ):
                outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.CLEANUP_FAILED",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.CLEANUP_FAILED",
                outcome.diagnostics,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertIsNotNone(_meta_value(stage.staged_db_path, meta_key))
            self.assertEqual(
                (renamed / "out.jsonl").read_bytes(),
                _NEW_JSONL,
            )
            self.assertEqual(
                (renamed / "out.jsonl.localcat-snapshot.json").read_bytes(),
                _manifest_bytes_for(receipt),
            )

    def test_terminal_replay_parent_replaced_between_fsync_and_clear_blocks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            _publish_registered_pair(destination, receipt, _NEW_JSONL)

            paths.jsonl_recovery.write_bytes(prior_jsonl)
            paths.manifest_recovery.write_bytes(prior_manifest)
            store.record_export_recovery_handoff(
                receipt.snapshot_id,
                expected_generation=generation,
                jsonl_recovery_identity=_identity_of(paths.jsonl_recovery),
                manifest_recovery_identity=_identity_of(
                    paths.manifest_recovery
                ),
            )
            self._complete_export(
                store,
                receipt,
                destination,
                expected_generation=generation,
            )
            meta_key = "artifact_handoff." + receipt.snapshot_id
            original_fsync = tm_snapshot_recovery._fsync_artifact_parent
            renamed = (root / "exports-renamed").resolve()

            def fsync_then_replace_parent(
                destination_path: Path,
                expected_identity: tuple[int, int] | None,
                parent: Any = None,
            ) -> None:
                original_fsync(
                    destination_path,
                    expected_identity,
                    parent=parent,
                )
                old_parent = destination_path.parent
                old_parent.rename(renamed)
                old_parent.mkdir()

            with patch(
                "tm_snapshot_recovery._fsync_artifact_parent",
                side_effect=fsync_then_replace_parent,
            ):
                outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("STORE.HANDOFF_PARENT_REPLACED",),
                    ),
                ),
            )
            self.assertIn(
                "STORE.HANDOFF_PARENT_REPLACED",
                outcome.diagnostics,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertIsNotNone(_meta_value(stage.staged_db_path, meta_key))
            self.assertEqual(
                (renamed / "out.jsonl").read_bytes(),
                _NEW_JSONL,
            )
            self.assertEqual(
                (renamed / "out.jsonl.localcat-snapshot.json").read_bytes(),
                _manifest_bytes_for(receipt),
            )

    def test_terminal_replay_legacy_handoff_missing_parent_blocks_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            _publish_registered_pair(destination, receipt, _NEW_JSONL)

            self._complete_export(
                store,
                receipt,
                destination,
                expected_generation=generation,
            )
            meta_key = "artifact_handoff." + receipt.snapshot_id
            stored_value = _meta_value(stage.staged_db_path, meta_key)
            self.assertIsNotNone(stored_value)
            assert stored_value is not None
            legacy_value = json.loads(stored_value)
            del legacy_value["artifact_parent_device"]
            del legacy_value["artifact_parent_inode"]
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_meta SET value = ? WHERE key = ?",
                    (
                        json.dumps(
                            legacy_value,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        meta_key,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(outcome.receipts, ())
            self.assertEqual(outcome.error_code, "STORE.HANDOFF_CORRUPT")
            self.assertIn(
                "STORE.HANDOFF_CORRUPT",
                outcome.diagnostics,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertIsNotNone(_meta_value(stage.staged_db_path, meta_key))
            self.assertEqual(paths.destination.read_bytes(), _NEW_JSONL)
            self.assertEqual(
                paths.manifest.read_bytes(),
                _manifest_bytes_for(receipt),
            )

    def test_pre_latched_divergence_replays_arbitrary_terminal_handoff_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            _simulate_crash(service, store, crash_at="manifest")
            configured_id = _issued_refresh_snapshot_id(stage.staged_db_path)
            revision = store.capture_export_snapshot().revision
            store.complete_issued_refresh_receipt(
                configured_id,
                expected_generation=revision.generation,
                jsonl_identity=_identity_of(identity.configured_jsonl_path),
                manifest_identity=_identity_of(
                    identity.snapshot_manifest_path
                ),
            )
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            _publish_registered_pair(destination, receipt, _NEW_JSONL)

            self._complete_export(
                store,
                receipt,
                destination,
                expected_generation=generation,
            )
            binding_before = _binding_row(stage.staged_db_path)[4]
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                connection.execute(
                    "UPDATE tm_meta SET value = '1' "
                    "WHERE key = 'divergence_latched'"
                )
                connection.commit()
            finally:
                connection.close()
            configured_key = "artifact_handoff." + configured_id
            arbitrary_key = "artifact_handoff." + receipt.snapshot_id
            self.assertIsNotNone(
                _meta_value(stage.staged_db_path, configured_key)
            )
            self.assertIsNotNone(
                _meta_value(stage.staged_db_path, arbitrary_key)
            )
            configured_paths = _paths(identity)
            self.assertTrue(configured_paths.jsonl_recovery.exists())

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.COMPLETED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.COMPLETED,
                    ),
                ),
            )
            self.assertEqual(
                outcome.diagnostics,
                ("RECOVERY.DIVERGENCE_PRESERVED",),
            )
            self.assertEqual(outcome.snapshot_id, receipt.snapshot_id)
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "1",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding_before,
            )
            self.assertIsNone(
                _meta_value(stage.staged_db_path, arbitrary_key)
            )
            for artifact in (
                paths.jsonl_temp,
                paths.manifest_temp,
                paths.jsonl_recovery,
                paths.manifest_recovery,
            ):
                self.assertFalse(artifact.exists(), artifact.name)
            self.assertIsNotNone(
                _meta_value(stage.staged_db_path, configured_key)
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, configured_id),
                "completed",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            for artifact in (
                configured_paths.jsonl_recovery,
                configured_paths.manifest_recovery,
            ):
                self.assertTrue(artifact.exists(), artifact.name)




    def test_store_clear_parent_renamed_with_foreign_temp_blocks_closed(
        self,
    ) -> None:
        """Store clear race A: parent renamed after the dirfd is bound.

        The handoff parent is opened and fstat-proven, then the real
        parent A is renamed aside and an empty replacement B is created
        at the advertised pathname while A still holds a foreign
        deterministic temp.  ``clear_issued_receipt_handoff`` must
        inspect the deterministic slots relative to the retained
        descriptor (A), never re-resolve the advertised parent pathname,
        keep the handoff and the foreign file, and fail closed with
        ``STORE.HANDOFF_CLEANUP_PENDING``.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            _publish_registered_pair(destination, receipt, _NEW_JSONL)

            self._complete_export(
                store,
                receipt,
                destination,
                expected_generation=generation,
            )
            meta_key = "artifact_handoff." + receipt.snapshot_id
            self.assertIsNotNone(_meta_value(stage.staged_db_path, meta_key))
            foreign_bytes = b"foreign temp bytes\n"
            paths.jsonl_temp.write_bytes(foreign_bytes)
            original_seam = (
                tm_sqlite_store._after_artifact_parent_dirfd_bound
            )
            renamed = (root / "exports-renamed").resolve()
            armed = False

            def rename_parent_in_seam(
                destination_path: Path,
                parent_identity: Any,
            ) -> None:
                original_seam(destination_path, parent_identity)
                if not armed or destination_path != destination:
                    return
                old_parent = destination_path.parent
                old_parent.rename(renamed)
                old_parent.mkdir()

            armed = True
            with patch(
                "tm_sqlite_store._after_artifact_parent_dirfd_bound",
                side_effect=rename_parent_in_seam,
            ):
                with self.assertRaises(SQLiteStoreSchemaError) as blocked:
                    store.clear_issued_receipt_handoff(
                        receipt.snapshot_id,
                        expected_generation=generation,
                    )

            self.assertEqual(
                str(blocked.exception),
                "STORE.HANDOFF_CLEANUP_PENDING",
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertIsNotNone(_meta_value(stage.staged_db_path, meta_key))
            self.assertEqual(
                (renamed / paths.jsonl_temp.name).read_bytes(),
                foreign_bytes,
            )
            self.assertEqual(list(destination.parent.iterdir()), [])
            self.assertFalse((renamed / paths.jsonl_temp.name).is_symlink())

    def test_export_reconcile_parent_renamed_with_owned_temp_moved_blocks(
        self,
    ) -> None:
        """Rename-only recovery-copy update race C.

        An issued arbitrary export is reconciled with the handoff parent
        bound, then the real parent A is renamed aside, an empty
        replacement B is created at the advertised pathname and the
        exact handed-off JSONL temp inode is moved from A into B under
        its deterministic basename.  The reveal must never re-resolve
        the advertised parent pathname for destructive work: the receipt
        stays issued, the handoff is retained, the moved inode survives
        in B and no unjournaled owned artifact is stranded.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            paths.destination.write_bytes(_NEW_JSONL)
            meta_key = "artifact_handoff." + receipt.snapshot_id
            self.assertIsNotNone(_meta_value(stage.staged_db_path, meta_key))
            temp_identity = _identity_of(paths.jsonl_temp)
            original_seam = tm_snapshot_recovery._after_recovery_parent_bound
            moved = (root / "exports-moved").resolve()

            def seam_rename_and_move_owned_temp(
                destination_path: Path,
                parent_identity: Any,
            ) -> None:
                original_seam(destination_path, parent_identity)
                if destination_path != destination:
                    return
                old_parent = destination_path.parent
                old_parent.rename(moved)
                old_parent.mkdir()
                os.replace(
                    moved / paths.jsonl_temp.name,
                    old_parent / paths.jsonl_temp.name,
                )

            with patch(
                "tm_snapshot_recovery._after_recovery_parent_bound",
                side_effect=seam_rename_and_move_owned_temp,
            ):
                outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PARENT_REPLACED",),
                    ),
                ),
            )
            self.assertEqual(
                outcome.receipts[0].diagnostics,
                ("RECOVERY.EXPORT_PARENT_REPLACED",),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "issued",
            )
            self.assertIsNotNone(_meta_value(stage.staged_db_path, meta_key))
            moved_temp = destination.parent / paths.jsonl_temp.name
            self.assertEqual(moved_temp.read_bytes(), _NEW_JSONL)
            self.assertEqual(_identity_of(moved_temp), temp_identity)
            self.assertEqual(
                sorted(path.name for path in destination.parent.iterdir()),
                [paths.jsonl_temp.name],
            )
            self.assertFalse((moved / paths.jsonl_temp.name).exists())
            self.assertEqual((moved / "out.jsonl").read_bytes(), _NEW_JSONL)
            self.assertFalse((moved / paths.manifest.name).exists())
            self.assertEqual(
                (moved / paths.manifest_temp.name).read_bytes(),
                _manifest_bytes_for(receipt),
            )

    def test_terminal_replay_parent_renamed_with_owned_temp_moved_blocks(
        self,
    ) -> None:
        """Terminal replay moved-owned-inode race.

        After the blocker binds the handoff parent, the real parent A is
        renamed aside, an empty replacement B is created at the
        advertised pathname and the exact handed-off JSONL temp inode is
        moved from A into B under its deterministic basename.  The
        replay must never resolve the advertised parent pathname for
        destructive work: it must not delete the moved inode from B,
        must keep the handoff and fail closed with
        ``RECOVERY.EXPORT_PARENT_REPLACED``.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            _publish_registered_pair(destination, receipt, _NEW_JSONL)

            paths.jsonl_recovery.write_bytes(prior_jsonl)
            paths.manifest_recovery.write_bytes(prior_manifest)
            store.record_export_recovery_handoff(
                receipt.snapshot_id,
                expected_generation=generation,
                jsonl_recovery_identity=_identity_of(paths.jsonl_recovery),
                manifest_recovery_identity=_identity_of(
                    paths.manifest_recovery
                ),
            )
            self._complete_export(
                store,
                receipt,
                destination,
                expected_generation=generation,
            )
            meta_key = "artifact_handoff." + receipt.snapshot_id
            paths.jsonl_temp.write_bytes(_NEW_JSONL)
            temp_identity = _identity_of(paths.jsonl_temp)
            original_blocker = (
                tm_snapshot_recovery._terminal_handoff_row_blocker
            )
            moved = (root / "exports-moved").resolve()

            def blocker_then_move_owned_temp(
                facts: Any,
                terminal_receipt: Any,
            ) -> Any:
                blocker, parent_handle = original_blocker(
                    facts,
                    terminal_receipt,
                )
                if blocker is None:
                    assert parent_handle is not None
                    old_parent = destination.parent
                    old_parent.rename(moved)
                    old_parent.mkdir()
                    os.replace(
                        moved / paths.jsonl_temp.name,
                        old_parent / paths.jsonl_temp.name,
                    )
                return blocker, parent_handle

            with patch(
                "tm_snapshot_recovery._terminal_handoff_row_blocker",
                side_effect=blocker_then_move_owned_temp,
            ):
                outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_PARENT_REPLACED",),
                    ),
                ),
            )
            self.assertIn(
                "RECOVERY.EXPORT_PARENT_REPLACED",
                outcome.diagnostics,
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, receipt.snapshot_id),
                "completed",
            )
            self.assertIsNotNone(_meta_value(stage.staged_db_path, meta_key))
            moved_temp = destination.parent / paths.jsonl_temp.name
            self.assertEqual(moved_temp.read_bytes(), _NEW_JSONL)
            self.assertEqual(_identity_of(moved_temp), temp_identity)
            self.assertEqual(
                sorted(path.name for path in destination.parent.iterdir()),
                [paths.jsonl_temp.name],
            )
            self.assertFalse((moved / paths.jsonl_temp.name).exists())
            self.assertEqual((moved / "out.jsonl").read_bytes(), _NEW_JSONL)
            self.assertEqual(
                (moved / "out.jsonl.localcat-snapshot.json").read_bytes(),
                _manifest_bytes_for(receipt),
            )


    def test_foreign_same_byte_manifest_after_crash_blocks_completion(
        self,
    ) -> None:
        """P1 1 regression: a same-byte foreign final manifest after the
        crashed manifest replace must block recovery completion.

        The completion transaction binds the final inodes to the
        durable handoff temp identities: the foreign manifest inode is
        not the handed-off temp inode, so the receipt stays issued, the
        binding is not advanced, the handoff is retained and the
        foreign manifest inode is preserved.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            identity = stage.resource_identity
            service = _service(identity)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            prior_manifest_bytes = (
                identity.snapshot_manifest_path.read_bytes()
            )
            _simulate_crash(service, store, crash_at="manifest")
            snapshot_id = _issued_refresh_snapshot_id(
                stage.staged_db_path
            )
            paths = _paths(identity)
            manifest_bytes = identity.snapshot_manifest_path.read_bytes()
            foreign = identity.snapshot_manifest_path.with_name(
                "foreign-same-byte-manifest.json"
            )
            foreign.write_bytes(manifest_bytes)
            os.replace(foreign, identity.snapshot_manifest_path)
            foreign_identity = _identity_of(
                identity.snapshot_manifest_path
            )

            outcome = store.recover_configured_refresh()

            _assert_recovery_outcome_shape(self, outcome)
            self.assertIs(outcome.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                outcome.receipts,
                (
                    IssuedReceiptRecovery(
                        snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("STORE.HANDOFF_IDENTITY_MISMATCH",),
                    ),
                ),
            )
            self.assertEqual(
                _status_for(stage.staged_db_path, snapshot_id),
                "issued",
            )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertIsNotNone(
                _meta_value(
                    stage.staged_db_path,
                    "artifact_handoff." + snapshot_id,
                )
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                manifest_bytes,
            )
            self.assertEqual(
                _identity_of(identity.snapshot_manifest_path),
                foreign_identity,
            )
            self.assertEqual(
                paths.manifest_recovery.read_bytes(),
                prior_manifest_bytes,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_reconstructed_manifest_source_same_byte_swap_blocks(
        self,
    ) -> None:
        """P1 2 regression: same-byte foreign source swap immediately
        before the reconstructed-manifest replace.

        The handed-off manifest temp inode is re-proven again after the
        late-bound seam returns; a same-byte foreign inode swapped in
        exactly at the seam is detected before the rename, so the first
        recovery BLOCKs on the pre-rename source proof and never renames
        the foreign temp.  The second recovery BLOCKs on the unprovable
        foreign temp in its slot: the prior manifest recovery copy, the
        foreign temp inode and the handoff are all preserved and never
        cleared or completed.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            os.replace(paths.jsonl_temp, paths.destination)
            paths.jsonl_recovery.write_bytes(prior_jsonl)
            paths.manifest_recovery.write_bytes(prior_manifest)
            store.record_export_recovery_handoff(
                receipt.snapshot_id,
                expected_generation=generation,
                jsonl_recovery_identity=_identity_of(
                    paths.jsonl_recovery
                ),
                manifest_recovery_identity=_identity_of(
                    paths.manifest_recovery
                ),
            )
            original_seam = (
                tm_snapshot_recovery
                ._after_recovery_manifest_source_proved
            )
            swapped = False
            foreign_identity: tuple[int, int] | None = None

            def seam_swap_manifest_source(
                destination_path: Path,
                manifest_temp_name: str,
                manifest_name: str,
                expected_source_identity: tuple[int, int],
            ) -> None:
                nonlocal swapped, foreign_identity
                original_seam(
                    destination_path,
                    manifest_temp_name,
                    manifest_name,
                    expected_source_identity,
                )
                if swapped:
                    return
                swapped = True
                foreign = paths.manifest_temp.with_name(
                    "foreign-same-byte-manifest.tmp"
                )
                foreign.write_bytes(paths.manifest_temp.read_bytes())
                os.replace(foreign, paths.manifest_temp)
                observed = os.lstat(paths.manifest_temp)
                foreign_identity = (observed.st_dev, observed.st_ino)

            with patch(
                "tm_snapshot_recovery."
                "_after_recovery_manifest_source_proved",
                side_effect=seam_swap_manifest_source,
            ):
                first = store.recover_configured_refresh()
            second = store.recover_configured_refresh()

            self.assertTrue(swapped)
            self.assertIsNotNone(foreign_identity)
            _assert_recovery_outcome_shape(self, first)
            self.assertIs(first.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                first.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.MANIFEST_TEMP_INVALID",),
                    ),
                ),
            )
            _assert_recovery_outcome_shape(self, second)
            self.assertIs(second.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                second.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.ARTIFACT_UNPROVEN",),
                    ),
                ),
            )
            for outcome in (first, second):
                self.assertEqual(
                    _status_for(
                        stage.staged_db_path,
                        receipt.snapshot_id,
                    ),
                    "issued",
                )
                self.assertIsNotNone(
                    _meta_value(
                        stage.staged_db_path,
                        "artifact_handoff." + receipt.snapshot_id,
                    )
                )
                self.assertEqual(
                    paths.jsonl_recovery.read_bytes(),
                    prior_jsonl,
                )
                self.assertEqual(
                    paths.manifest_recovery.read_bytes(),
                    prior_manifest,
                )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                paths.destination.read_bytes(),
                _NEW_JSONL,
            )
            self.assertEqual(
                paths.manifest.read_bytes(),
                prior_manifest,
            )
            self.assertEqual(
                paths.manifest_temp.read_bytes(),
                _manifest_bytes_for(receipt),
            )
            self.assertEqual(
                _identity_of(paths.manifest_temp),
                foreign_identity,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_reconstructed_manifest_source_different_byte_swap_blocks(
        self,
    ) -> None:
        """P1 2 regression: different-byte foreign source swap
        immediately before the reconstructed-manifest replace.

        The handed-off manifest temp inode and digest are re-proven
        again after the late-bound seam returns; a foreign inode with
        different bytes swapped in exactly at the seam is detected
        before the rename, so the first recovery BLOCKs on the
        pre-rename source proof.  The second recovery BLOCKs on the
        conflicting foreign temp in its slot: the prior manifest
        recovery copy, the foreign temp inode and the handoff are all
        preserved and never cleared or completed.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            foreign_bytes = b'{"manifest":"foreign-bytes"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            os.replace(paths.jsonl_temp, paths.destination)
            paths.jsonl_recovery.write_bytes(prior_jsonl)
            paths.manifest_recovery.write_bytes(prior_manifest)
            store.record_export_recovery_handoff(
                receipt.snapshot_id,
                expected_generation=generation,
                jsonl_recovery_identity=_identity_of(
                    paths.jsonl_recovery
                ),
                manifest_recovery_identity=_identity_of(
                    paths.manifest_recovery
                ),
            )
            original_seam = (
                tm_snapshot_recovery
                ._after_recovery_manifest_source_proved
            )
            swapped = False
            foreign_identity: tuple[int, int] | None = None

            def seam_swap_manifest_source(
                destination_path: Path,
                manifest_temp_name: str,
                manifest_name: str,
                expected_source_identity: tuple[int, int],
            ) -> None:
                nonlocal swapped, foreign_identity
                original_seam(
                    destination_path,
                    manifest_temp_name,
                    manifest_name,
                    expected_source_identity,
                )
                if swapped:
                    return
                swapped = True
                foreign = paths.manifest_temp.with_name(
                    "foreign-different-byte-manifest.tmp"
                )
                foreign.write_bytes(foreign_bytes)
                os.replace(foreign, paths.manifest_temp)
                observed = os.lstat(paths.manifest_temp)
                foreign_identity = (observed.st_dev, observed.st_ino)

            with patch(
                "tm_snapshot_recovery."
                "_after_recovery_manifest_source_proved",
                side_effect=seam_swap_manifest_source,
            ):
                first = store.recover_configured_refresh()
            second = store.recover_configured_refresh()

            self.assertTrue(swapped)
            self.assertIsNotNone(foreign_identity)
            _assert_recovery_outcome_shape(self, first)
            self.assertIs(first.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                first.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.MANIFEST_TEMP_INVALID",),
                    ),
                ),
            )
            _assert_recovery_outcome_shape(self, second)
            self.assertIs(second.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                second.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.MANIFEST_TEMP_CONFLICT",),
                    ),
                ),
            )
            for outcome in (first, second):
                self.assertEqual(
                    _status_for(
                        stage.staged_db_path,
                        receipt.snapshot_id,
                    ),
                    "issued",
                )
                self.assertIsNotNone(
                    _meta_value(
                        stage.staged_db_path,
                        "artifact_handoff." + receipt.snapshot_id,
                    )
                )
                self.assertEqual(
                    paths.jsonl_recovery.read_bytes(),
                    prior_jsonl,
                )
                self.assertEqual(
                    paths.manifest_recovery.read_bytes(),
                    prior_manifest,
                )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                paths.destination.read_bytes(),
                _NEW_JSONL,
            )
            self.assertEqual(paths.manifest.read_bytes(), prior_manifest)
            self.assertEqual(
                paths.manifest_temp.read_bytes(),
                foreign_bytes,
            )
            self.assertEqual(
                _identity_of(paths.manifest_temp),
                foreign_identity,
            )
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )

    def test_reconstructed_manifest_destination_swap_blocks(
        self,
    ) -> None:
        """P1 2 regression: foreign destination swap immediately before
        the reconstructed-manifest replace.

        The manifest destination is re-proven against the handed-off
        prior digest AND inode after the late-bound seam returns; a
        foreign different-byte inode swapped in exactly at the seam is
        detected before the rename, so the first recovery BLOCKs on the
        pre-rename destination proof and the foreign final inode is
        never overwritten.  The second recovery BLOCKs on the foreign
        final manifest: the durable manifest temp, the prior recovery
        copies and the handoff are all retained and never cleared or
        completed.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, store = _prepared_store(root)
            binding = _bind_current_snapshot(store, stage, _PRIOR_JSONL)
            destination = self._destination(root)
            paths = _export_artifact_paths(destination)
            prior_jsonl = b'{"source":"prior","target":"pair"}\n'
            prior_manifest = b'{"manifest":"prior"}\n'
            foreign_bytes = b'{"manifest":"foreign-bytes"}\n'
            destination.write_bytes(prior_jsonl)
            paths.manifest.write_bytes(prior_manifest)
            receipt = self._register_export(store, destination, _NEW_JSONL)
            generation = store.capture_export_snapshot().revision.generation
            os.replace(paths.jsonl_temp, paths.destination)
            paths.jsonl_recovery.write_bytes(prior_jsonl)
            paths.manifest_recovery.write_bytes(prior_manifest)
            store.record_export_recovery_handoff(
                receipt.snapshot_id,
                expected_generation=generation,
                jsonl_recovery_identity=_identity_of(
                    paths.jsonl_recovery
                ),
                manifest_recovery_identity=_identity_of(
                    paths.manifest_recovery
                ),
            )
            original_seam = (
                tm_snapshot_recovery
                ._after_recovery_manifest_source_proved
            )
            swapped = False
            foreign_identity: tuple[int, int] | None = None

            def seam_swap_manifest_destination(
                destination_path: Path,
                manifest_temp_name: str,
                manifest_name: str,
                expected_source_identity: tuple[int, int],
            ) -> None:
                nonlocal swapped, foreign_identity
                original_seam(
                    destination_path,
                    manifest_temp_name,
                    manifest_name,
                    expected_source_identity,
                )
                if swapped:
                    return
                swapped = True
                foreign = paths.manifest.with_name(
                    "foreign-manifest-destination.tmp"
                )
                foreign.write_bytes(foreign_bytes)
                os.replace(foreign, paths.manifest)
                observed = os.lstat(paths.manifest)
                foreign_identity = (observed.st_dev, observed.st_ino)

            with patch(
                "tm_snapshot_recovery."
                "_after_recovery_manifest_source_proved",
                side_effect=seam_swap_manifest_destination,
            ):
                first = store.recover_configured_refresh()
            second = store.recover_configured_refresh()

            self.assertTrue(swapped)
            self.assertIsNotNone(foreign_identity)
            _assert_recovery_outcome_shape(self, first)
            self.assertIs(first.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                first.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.MANIFEST_DESTINATION_CHANGED",),
                    ),
                ),
            )
            _assert_recovery_outcome_shape(self, second)
            self.assertIs(second.state, RefreshRecoveryState.BLOCKED)
            self.assertEqual(
                second.receipts,
                (
                    IssuedReceiptRecovery(
                        receipt.snapshot_id,
                        RefreshRecoveryState.BLOCKED,
                        ("RECOVERY.EXPORT_MANIFEST_FOREIGN",),
                    ),
                ),
            )
            for outcome in (first, second):
                self.assertEqual(
                    _status_for(
                        stage.staged_db_path,
                        receipt.snapshot_id,
                    ),
                    "issued",
                )
                self.assertIsNotNone(
                    _meta_value(
                        stage.staged_db_path,
                        "artifact_handoff." + receipt.snapshot_id,
                    )
                )
                self.assertEqual(
                    paths.jsonl_recovery.read_bytes(),
                    prior_jsonl,
                )
                self.assertEqual(
                    paths.manifest_recovery.read_bytes(),
                    prior_manifest,
                )
            self.assertEqual(
                _binding_row(stage.staged_db_path)[4],
                binding.receipt.snapshot_id,
            )
            self.assertEqual(
                paths.destination.read_bytes(),
                _NEW_JSONL,
            )
            self.assertEqual(paths.manifest.read_bytes(), foreign_bytes)
            self.assertEqual(
                _identity_of(paths.manifest),
                foreign_identity,
            )
            self.assertEqual(
                paths.manifest_temp.read_bytes(),
                _manifest_bytes_for(receipt),
            )
            self.assertTrue(paths.manifest_temp.exists())
            self.assertFalse(paths.jsonl_temp.exists())
            self.assertEqual(
                _meta_value(stage.staged_db_path, "divergence_latched"),
                "0",
            )


if __name__ == "__main__":
    unittest.main()
