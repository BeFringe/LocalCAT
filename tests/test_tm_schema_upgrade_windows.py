"""Windows current-source schema-upgrade acceptance for WA-06 5.11a.

The fixture deliberately does not reuse the historical POSIX legacy-store
coordinator.  It creates one v1 database under the real Windows W1/rooted
owner, publishes the canonical pair without overwrite, and commits the
PREPARED/publication chain through the production owners with exact portable
content attestations.  A fresh coordinator must rehydrate that completed
portable predecessor before the public upgrade entry point is exercised.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import ctypes
import hashlib
import os
from pathlib import Path, PurePath
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import tm_activation_journal
import tm_activation_recovery
import tm_content_attestation
import tm_migration
import tm_sqlite_store
from platform_fs_contracts import FileObjectIdentity, PublishMode
from text_matcher import fold_text_v1
from tm_contracts import (
    CanonicalResourceIdentity,
    MutableStageRef,
    SchemaUpgradeReport,
    SchemaUpgradeFailure,
    SNAPSHOT_BINDING_VERSION,
    SNAPSHOT_FORMAT_VERSION,
    SNAPSHOT_MANIFEST_VERSION,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    contract_to_json,
    snapshot_receipt_digest,
)
from tm_engine import open_canonical_tm_store
from tm_migration import TMMigrationService
from tm_sqlite_store import (
    ResourceStoreCoordinator,
    TM_LEGACY_SCHEMA_VERSION,
    TM_SCHEMA_VERSION,
    initialize_stage_schema,
    inspect_stage_schema,
    unique_character_ngrams,
)


_SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"other","target":"value"}\n'
)
_STORE_ID = "store.primary"


def _domain_digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _portable_proof(payload: bytes) -> tm_content_attestation.PortableContentFileProof:
    return tm_content_attestation.PortableContentFileProof(
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _exact_parity_digest(rows: tuple[tuple[str, str], ...]) -> str:
    winners: dict[str, str] = {}
    for source, target in rows:
        winners[source] = target
    digest = hashlib.sha256()
    for source in sorted(winners):
        for value in (source, winners[source]):
            encoded = value.encode("utf-8")
            digest.update(str(len(encoded)).encode("ascii"))
            digest.update(b":")
            digest.update(encoded)
            digest.update(b";")
    return digest.hexdigest()


def _tree_bytes(root: Path) -> dict[str, bytes]:
    extended = "\\\\?\\" + str(root)
    result: dict[str, bytes] = {}
    for directory, _children, names in os.walk(extended):
        for name in names:
            path = os.path.join(directory, name)
            key = path[len(extended):].lstrip("\\/").replace("\\", "/")
            with open(path, "rb") as stream:
                result[key] = stream.read()
    return result


def _remove_long_quarantine(root: Path) -> None:
    quarantine = root / ".localcat-activation-quarantine-v1"
    if not quarantine.exists():
        return
    for attempt in quarantine.iterdir():
        for path in attempt.iterdir():
            os.unlink("\\\\?\\" + str(path))
        os.rmdir("\\\\?\\" + str(attempt))
    os.rmdir("\\\\?\\" + str(quarantine))


def _schema_backup_name(fixture: _PortableV1Fixture) -> str:
    return tm_sqlite_store._portable_schema_upgrade_report_backup_name(
        canonical_database_name=fixture.identity.canonical_sidecar_path.name,
        target_identity=fixture.identity.target_identity,
        canonical_store_id=_STORE_ID,
        prior_generation=0,
        prior_database=_portable_proof(fixture.prior_database),
    )


def _publish_sibling(
    fixture: _PortableV1Fixture,
    *,
    name: str,
    payload: bytes,
    private: bool = True,
) -> FileObjectIdentity:
    with fixture.service._acquire_initial_reservation() as reservation:
        _backend, root, _lease, _lock_name, _lock_payload = (
            reservation.bound_family_inputs()
        )
        _publish_rooted_bytes(
            root=root,
            destination_name=name,
            payload=payload,
            private=private,
        )
        published = root.inspect_entry(name)
        if published is None:
            raise AssertionError("private sibling publication disappeared")
        return published.identity


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    terminate = kernel32.TerminateProcess
    terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    terminate.restype = ctypes.c_int32
    if not terminate(int(process._handle), 94):
        raise ctypes.WinError(ctypes.get_last_error())


def _wait_for_marker(marker: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if marker.is_file():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                "schema-upgrade worker exited before the durable phase: "
                f"returncode={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
            )
        time.sleep(0.05)
    raise AssertionError("schema-upgrade worker did not reach its durable phase")


@dataclass(frozen=True)
class _PortableV1Fixture:
    identity: CanonicalResourceIdentity
    coordinator: ResourceStoreCoordinator
    service: TMMigrationService
    prior_database: bytes
    prior_manifest: bytes
    private_directory: Path


def _publish_rooted_bytes(
    *,
    root: object,
    destination_name: str,
    payload: bytes,
    private: bool = True,
) -> tm_content_attestation.PortableContentFileProof:
    candidate_name = destination_name + ".portable-v1-candidate"
    if root.inspect_entry(destination_name) is not None:
        raise AssertionError("portable-v1 fixture never overwrites authority")
    if root.inspect_entry(candidate_name) is not None:
        raise AssertionError("portable-v1 fixture candidate is not fresh")
    candidate = root.create_candidate(candidate_name, private=private)
    pending = None
    try:
        candidate.write_all(payload)
        candidate.flush_content()
        pending = root.begin_publish(
            candidate,
            destination_name,
            mode=PublishMode.CREATE_IF_ABSENT,
            lease=None,
        )
        candidate = None
        retained = pending.retained_destination()
        facts = retained.content_facts()
        expected = _portable_proof(payload)
        if (
            facts.snapshot.identity.kind != "regular"
            or facts.snapshot.identity.link_count != 1
            or root.inspect_entry(destination_name) != facts.snapshot
            or facts.snapshot.byte_count != expected.size
            or facts.content_sha256.hex() != expected.sha256
            or retained.read_all() != payload
            or pending.terminal_reproof() != pending.preliminary_facts()
        ):
            raise AssertionError("portable-v1 rooted publication did not close")
        return expected
    finally:
        if candidate is not None:
            candidate.close()
        if pending is not None:
            pending.close()


def _build_active_v1_stage(
    *,
    identity: CanonicalResourceIdentity,
    stage: MutableStageRef,
    attempt: object,
) -> tuple[
    SnapshotReceipt,
    SnapshotManifest,
    tm_content_attestation.ContentSemanticFacts,
]:
    schema = initialize_stage_schema(
        stage,
        canonical_store_id=_STORE_ID,
        _legacy_schema=True,
        _caller_reservation=attempt.stage_reservation,
        _caller_parent=attempt.platform_parent,
    )
    source_digest = hashlib.sha256(_SOURCE_BYTES).hexdigest()
    batch_id = f"migration.{source_digest}"
    rows = (("same", "first"), ("other", "value"))
    receipt = SnapshotReceipt(
        snapshot_id=f"snapshot.migration.{source_digest[:24]}",
        resource_id=identity.resource_id,
        canonical_store_id=_STORE_ID,
        exported_revision=1,
        jsonl_digest=source_digest,
        record_count=len(rows),
        format_version=SNAPSHOT_FORMAT_VERSION,
    )
    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    connection = sqlite3.connect(stage.staged_db_path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO tm_origin_batch("
            "batch_id, kind, source_digest, source_path, status, valid_count, "
            "invalid_count, duplicate_source_count, created_at) "
            "VALUES (?, 'migration', ?, ?, 'completed', ?, 0, 0, ?)",
            (
                batch_id,
                source_digest,
                str(identity.configured_jsonl_path),
                len(rows),
                "2026-09-02T00:00:00+00:00",
            ),
        )
        required_sizes = (1, 2) if schema.fts5_available else (1, 2, 3)
        for ordinal, (source, target) in enumerate(rows):
            folded = fold_text_v1(source).folded_text
            record_id = ordinal + 1
            connection.execute(
                "INSERT INTO tm_record("
                "record_id, source_raw, target_raw, source_fold_v1, "
                "speaker_raw, context_prev_raw, context_next_raw, file_source, "
                "provenance_json, legacy_line_no, usage_count, last_used, "
                "origin_batch_id, origin_ordinal) "
                "VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, 0, NULL, ?, ?)",
                (
                    record_id,
                    source,
                    target,
                    folded,
                    '[["source","legacy-jsonl"]]',
                    record_id,
                    batch_id,
                    ordinal,
                ),
            )
            for gram_size in required_sizes:
                for gram in unique_character_ngrams(folded, gram_size):
                    connection.execute(
                        "INSERT INTO tm_gram(gram_size, gram, record_id) "
                        "VALUES (?, ?, ?)",
                        (gram_size, gram, record_id),
                    )
            if schema.fts5_available:
                connection.execute(
                    "INSERT INTO tm_fts(source_fold_v1, record_id) VALUES (?, ?)",
                    (folded, record_id),
                )
        connection.execute(
            "INSERT INTO tm_snapshot_receipt("
            "snapshot_id, resource_id, canonical_store_id, exported_revision, "
            "jsonl_digest, record_count, format_version, destination_jsonl_path, "
            "destination_manifest_path, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?)",
            (
                receipt.snapshot_id,
                receipt.resource_id,
                receipt.canonical_store_id,
                receipt.exported_revision,
                receipt.jsonl_digest,
                receipt.record_count,
                receipt.format_version,
                str(identity.configured_jsonl_path),
                str(identity.snapshot_manifest_path),
                "2026-09-02T00:00:00+00:00",
            ),
        )
        connection.execute(
            "UPDATE tm_meta SET value = '1' WHERE key = 'head_revision'"
        )
        connection.execute(
            "UPDATE tm_meta SET value = '0' WHERE key = 'generation'"
        )
        connection.execute(
            "UPDATE tm_meta SET value = 'SEALED' WHERE key = 'activation_status'"
        )
        connection.commit()
    finally:
        connection.close()

    manifest_bytes = contract_to_json(manifest).encode("utf-8")
    tm_migration._write_reserved_initial_manifest(
        stage.manifest_temp_path,
        manifest_bytes,
        attempt.platform_parent,
        attempt.manifest_reservation,
    )
    synchronized = attempt.platform_backend.open_existing_for_synchronization(
        attempt.platform_parent,
        PurePath(stage.staged_db_path.name),
    )
    try:
        facts = synchronized.content_facts()
        synchronized.synchronize_content(facts)
    finally:
        synchronized.close()
    snapshot = inspect_stage_schema(
        stage,
        canonical_store_id=_STORE_ID,
        _allow_legacy_schema=True,
        _allow_sealed=True,
    )
    connection = sqlite3.connect(
        f"{stage.staged_db_path.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        schema_digest = str(
            connection.execute(
                "SELECT value FROM tm_meta WHERE key = 'schema_digest'"
            ).fetchone()[0]
        )
        gram_counts = tuple(
            (int(size), int(count))
            for size, count in connection.execute(
                "SELECT gram_size, COUNT(*) FROM tm_gram "
                "GROUP BY gram_size ORDER BY gram_size"
            )
        )
        fts_count = (
            int(connection.execute("SELECT COUNT(*) FROM tm_fts").fetchone()[0])
            if snapshot.fts5_available
            else 0
        )
    finally:
        connection.close()
    # v1 predates the completed_revision column consumed by the v2 logical
    # closure codec.  Its portable owner still carries an exact semantic anchor:
    # the SHA-256 of the complete synchronized legacy database bytes.  The
    # production cold-entry gate must prove the same bytes before accepting it;
    # this is not a fabricated v2 closure for a shape the v2 codec cannot read.
    closure = hashlib.sha256(stage.staged_db_path.read_bytes()).hexdigest()
    semantic = tm_content_attestation.ContentSemanticFacts(
        schema_version=snapshot.schema_version,
        schema_digest=schema_digest,
        fold_version=snapshot.fold_version,
        index_version=snapshot.candidate_index_version,
        candidate_index_kind=snapshot.candidate_index_kind,
        fts5_available=snapshot.fts5_available,
        sqlite_runtime_version=snapshot.sqlite_runtime_version,
        unicode_runtime_version=snapshot.unicode_runtime_version,
        journal_mode=snapshot.journal_mode,
        synchronous=snapshot.synchronous,
        foreign_keys=snapshot.foreign_keys,
        busy_timeout_ms=snapshot.busy_timeout_ms,
        wal_enabled=snapshot.wal_enabled,
        extension_loading_enabled=snapshot.extension_loading_enabled,
        record_count=len(rows),
        receipt_boundary_record_count=len(rows),
        origin_batch_count=1,
        origin_batch_id=batch_id,
        origin_batch_kind="migration",
        exported_revision=1,
        fts_count=fts_count,
        receipt_boundary_fts_count=fts_count,
        gram_counts=gram_counts,
        exact_parity_digest=_exact_parity_digest(rows),
        logical_closure_version=tm_content_attestation.LOGICAL_CLOSURE_VERSION,
        logical_closure_digest=closure,
    )
    return receipt, manifest, semantic


def _activate_published_v1_database(
    *,
    bootstrap: ResourceStoreCoordinator,
    backend: object,
    rooted: object,
    identity: CanonicalResourceIdentity,
    receipt: SnapshotReceipt,
    activation_digest: str,
) -> tuple[bytes, tm_content_attestation.PortableContentFileProof]:
    """Reproduce the historical SEALED-to-ACTIVE owner transaction."""

    name = identity.canonical_sidecar_path.name
    guard = backend.guard_existing_for_mutation(rooted, PurePath(name))
    synchronized = None
    opened = None
    try:
        identity_before = guard.reprove()
        connection = sqlite3.connect(
            identity.canonical_sidecar_path,
            isolation_level=None,
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            meta = dict(connection.execute("SELECT key, value FROM tm_meta"))
            receipts = connection.execute(
                "SELECT snapshot_id, status FROM tm_snapshot_receipt"
            ).fetchall()
            bindings = connection.execute(
                "SELECT binding_id FROM tm_snapshot_binding"
            ).fetchall()
            if (
                meta.get("activation_status") != "SEALED"
                or meta.get("generation") != "0"
                or "activation_digest" in meta
                or receipts != [(receipt.snapshot_id, "issued")]
                or bindings
            ):
                raise AssertionError("portable-v1 SEALED predecessor is invalid")
            updated = connection.execute(
                "UPDATE tm_snapshot_receipt SET status = 'completed' "
                "WHERE snapshot_id = ? AND status = 'issued'",
                (receipt.snapshot_id,),
            )
            connection.execute(
                "INSERT INTO tm_snapshot_binding("
                "binding_id, configured_jsonl_path, manifest_path, snapshot_kind, "
                "snapshot_id, binding_version) VALUES (1, ?, ?, ?, ?, ?)",
                (
                    str(identity.configured_jsonl_path),
                    str(identity.snapshot_manifest_path),
                    SnapshotKind.MIGRATION_SOURCE.value,
                    receipt.snapshot_id,
                    SNAPSHOT_BINDING_VERSION,
                ),
            )
            status = connection.execute(
                "UPDATE tm_meta SET value = 'ACTIVE' "
                "WHERE key = 'activation_status' AND value = 'SEALED'"
            )
            connection.execute(
                "INSERT INTO tm_meta(key, value) VALUES ('activation_digest', ?)",
                (activation_digest,),
            )
            if updated.rowcount != 1 or status.rowcount != 1:
                raise AssertionError("portable-v1 activation update was incomplete")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        if guard.reprove() != identity_before:
            raise AssertionError("portable-v1 canonical identity drifted")
        synchronized = backend.open_existing_for_synchronization(
            rooted,
            PurePath(name),
        )
        synchronized.synchronize_content(synchronized.content_facts())
        synchronized.close()
        synchronized = None
        if guard.reprove() != identity_before:
            raise AssertionError("portable-v1 canonical identity drifted")
        opened = backend.open_regular(rooted, PurePath(name))
        payload = opened.read_all()
        facts = opened.content_facts()
        proof = bootstrap._portable_content_proof(facts)
        if (
            rooted.inspect_entry(name) != facts.snapshot
            or proof != _portable_proof(payload)
        ):
            raise AssertionError("portable-v1 ACTIVE proof did not close")
        return payload, proof
    finally:
        if opened is not None:
            opened.close()
        if synchronized is not None:
            synchronized.close()
        guard.close()


def _install_portable_v1(root: Path) -> _PortableV1Fixture:
    source = (root / "tm.primary.jsonl").resolve()
    source.write_bytes(_SOURCE_BYTES)
    identity = CanonicalResourceIdentity.from_configured_jsonl("tm.primary", source)
    bootstrap = ResourceStoreCoordinator(
        canonical_store_id=_STORE_ID,
        resource_identity=identity,
    )
    bootstrap_service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id=_STORE_ID,
        coordinator=bootstrap,
    )
    reservation = bootstrap_service._acquire_initial_reservation()
    attempt = None
    try:
        source_digest = hashlib.sha256(_SOURCE_BYTES).hexdigest()
        path_salt = "initial-" + hashlib.sha256(
            str(root).encode("utf-8")
        ).hexdigest()[:32]
        stage = tm_migration._deterministic_stage_ref(
            identity,
            source_digest=source_digest,
            stage_prefix="migration",
            path_salt=path_salt,
        )
        attempt = tm_migration._freeze_initial_stage_attempt(
            stage,
            path_salt=path_salt,
            resource_reservation=reservation,
        )
        receipt, manifest, semantic = _build_active_v1_stage(
            identity=identity,
            stage=stage,
            attempt=attempt,
        )
        sealed_database_bytes = stage.staged_db_path.read_bytes()
        manifest_bytes = contract_to_json(manifest).encode("utf-8")
        source_proof = _portable_proof(_SOURCE_BYTES)
        database_proof = _portable_proof(sealed_database_bytes)
        manifest_proof = _portable_proof(manifest_bytes)
        sealed = tm_content_attestation._create_portable_sealed_content_attestation(
            resource_id=identity.resource_id,
            target_identity=identity.target_identity,
            canonical_store_id=_STORE_ID,
            snapshot_receipt_digest=snapshot_receipt_digest(receipt),
            expected_prior_generation=None,
            evidence_digest=_domain_digest("windows-portable-v1-evidence"),
            database=database_proof,
            manifest=manifest_proof,
            source=source_proof,
            semantic_facts=semantic,
        )
        owner_inputs = reservation.portable_journal_inputs()
        journal_id = "journal.windows-portable-v1"
        unsigned = tm_activation_journal._PortableActivationJournalUnsigned(
            journal_version="activation-journal-v3",
            closure="PENDING",
            phase="PREPARED",
            journal_id=journal_id,
            preparation_id="preparation.windows-portable-v1",
            registry_namespace="registry.windows-portable-v1",
            token_id="token.windows-portable-v1",
            token_version="activation-token-v1",
            activation_nonce="nonce.windows-portable-v1",
            artifact_id="artifact.windows-portable-v1",
            artifact_seal_digest=_domain_digest("windows-portable-v1-artifact-seal"),
            sealed_stage_digest=_domain_digest("windows-portable-v1-stage"),
            resource_id=identity.resource_id,
            target_identity=identity.target_identity,
            canonical_store_id=_STORE_ID,
            expected_prior_generation=None,
            gate_b_grant_digest=_domain_digest("windows-portable-v1-gate-b"),
            evidence_digest=sealed.evidence_digest,
            snapshot_receipt_digest=sealed.snapshot_receipt_digest,
            stage_db_digest=database_proof.sha256,
            manifest_temp_digest=manifest_proof.sha256,
            source_jsonl_digest=source_proof.sha256,
            new_receipt_id=receipt.snapshot_id,
            new_manifest_digest=manifest_proof.sha256,
            candidate_stage_db_name=stage.staged_db_path.name,
            candidate_manifest_temp_name=stage.manifest_temp_path.name,
            private_directory_name=(
                tm_activation_journal._portable_activation_private_directory_name(
                    identity
                )
            ),
            device_key_name="device.key",
            journal_name="activation-journal-v3.json",
            terminal_name="activation-terminal-v3.json",
            lock_payload_digest=owner_inputs["caller_borrow"].lock_payload_digest(),
            sealed_content_attestation=sealed,
            active_content_attestation=None,
        )
        prepared = tm_activation_journal._WindowsPortablePreparedJournalOwner.publish(
            identity=identity,
            backend=owner_inputs["platform"],
            persistent_private=owner_inputs["persistent_private"],
            descendant_inspection=owner_inputs["descendant_inspection"],
            caller_borrow=owner_inputs["caller_borrow"],
            unsigned=unsigned,
            owner_reprove=reservation.reprove,
            owner_commit=lambda _record: None,
        )

        backend, rooted, _lease, _lock_name, _payload = (
            reservation.bound_family_inputs()
        )
        activation_digest = tm_activation_recovery._portable_recovery_activation_digest(
            prepared._record
        )
        active = None
        active_database_proof = None
        canonical_database_bytes = sealed_database_bytes
        predecessor = prepared._record
        published_database = False
        published_manifest = False

        def assert_rooted_active() -> None:
            reservation.reprove()
            if published_database:
                observed = backend.open_regular(
                    rooted, PurePath(identity.canonical_sidecar_path.name)
                )
                try:
                    if observed.read_all() != canonical_database_bytes:
                        raise AssertionError("portable-v1 database publication drifted")
                finally:
                    observed.close()
            if published_manifest:
                observed = backend.open_regular(
                    rooted, PurePath(identity.snapshot_manifest_path.name)
                )
                try:
                    if observed.read_all() != manifest_bytes:
                        raise AssertionError("portable-v1 manifest publication drifted")
                finally:
                    observed.close()

        for phase in tm_activation_journal._PORTABLE_PUBLICATION_PHASES:
            phase_active = None if phase == "DB_REPLACED" else active
            if phase != "DB_REPLACED" and phase_active is None:
                raise AssertionError("portable-v1 ACTIVE proof is missing")
            phase_database = (
                database_proof
                if phase_active is None
                else phase_active.database
            )
            unsigned_phase = tm_activation_journal._PortablePublicationPhaseUnsigned(
                publication_version="activation-publication-v1",
                phase=phase,
                predecessor_digest=predecessor.record_digest,
                journal_id=journal_id,
                preparation_id=unsigned.preparation_id,
                token_id=unsigned.token_id,
                token_version=unsigned.token_version,
                activation_nonce=unsigned.activation_nonce,
                resource_id=identity.resource_id,
                target_identity=identity.target_identity,
                canonical_store_id=_STORE_ID,
                generation=0,
                canonical_database_name=identity.canonical_sidecar_path.name,
                canonical_database_size=phase_database.size,
                canonical_database_sha256=phase_database.sha256,
                canonical_manifest_name=identity.snapshot_manifest_path.name,
                canonical_manifest_size=manifest_proof.size,
                canonical_manifest_sha256=manifest_proof.sha256,
                private_directory_name=unsigned.private_directory_name,
                device_key_name=unsigned.device_key_name,
                sealed_content_attestation=sealed,
                active_content_attestation=phase_active,
            )

            def owner_commit(_record: object, *, current: str = phase) -> None:
                nonlocal published_database, published_manifest
                if current == "DB_REPLACED":
                    proof = _publish_rooted_bytes(
                        root=rooted,
                        destination_name=identity.canonical_sidecar_path.name,
                        payload=sealed_database_bytes,
                    )
                    if proof != database_proof:
                        raise AssertionError("portable-v1 database proof drifted")
                    published_database = True
                elif current == "MANIFEST_PUBLISHED":
                    proof = _publish_rooted_bytes(
                        root=rooted,
                        destination_name=identity.snapshot_manifest_path.name,
                        payload=manifest_bytes,
                    )
                    if proof != manifest_proof:
                        raise AssertionError("portable-v1 manifest proof drifted")
                    published_manifest = True

            handle = tm_activation_journal._WindowsPortablePublicationPhaseOwner.publish(
                identity=identity,
                backend=owner_inputs["platform"],
                persistent_private=owner_inputs["persistent_private"],
                caller_borrow=owner_inputs["caller_borrow"],
                prepared=prepared._record,
                predecessor=predecessor,
                unsigned=unsigned_phase,
                owner_reprove=reservation.reprove,
                owner_commit=owner_commit,
                business_reprove=lambda _record: assert_rooted_active(),
            )
            predecessor = handle._record
            if phase == "DB_REPLACED":
                canonical_database_bytes, active_database_proof = (
                    _activate_published_v1_database(
                        bootstrap=bootstrap,
                        backend=backend,
                        rooted=rooted,
                        identity=identity,
                        receipt=receipt,
                        activation_digest=activation_digest,
                    )
                )
                active_semantic = replace(
                    semantic,
                    logical_closure_digest=active_database_proof.sha256,
                )
                active = tm_content_attestation._create_portable_active_content_attestation(
                    sealed_attestation_digest=sealed.attestation_digest,
                    journal_id=journal_id,
                    resource_id=identity.resource_id,
                    target_identity=identity.target_identity,
                    canonical_store_id=_STORE_ID,
                    snapshot_receipt_digest=sealed.snapshot_receipt_digest,
                    generation=0,
                    activation_digest=activation_digest,
                    database=active_database_proof,
                    manifest=manifest_proof,
                    source=source_proof,
                    semantic_facts=active_semantic,
                )
        if not published_database or not published_manifest:
            raise AssertionError("portable-v1 canonical pair was not published")
        recovery_inputs = reservation.portable_recovery_inputs()
        completed = tm_activation_journal._WindowsPortableFreshRecoveryOwner.inspect(
            identity=identity,
            canonical_store_id=_STORE_ID,
            backend=recovery_inputs["platform"],
            persistent_private=recovery_inputs["persistent_private"],
            descendant_inspection=recovery_inputs["descendant_inspection"],
            caller_borrow=recovery_inputs["caller_borrow"],
        )
        tm_activation_journal._WindowsPortableCompletedStageRetirementOwner.retire(
            identity=identity,
            backend=recovery_inputs["platform"],
            persistent_private=recovery_inputs["persistent_private"],
            descendant_inspection=recovery_inputs["descendant_inspection"],
            existing_retirement=recovery_inputs["existing_retirement"],
            caller_borrow=recovery_inputs["caller_borrow"],
            snapshot=completed,
        )
        bootstrap._ensure_portable_activation_lineage_marker(
            platform=backend,
            root=rooted,
        )
        reservation.reprove()

        tm_migration._close_initial_stage_attempt_authorities(attempt)
        reservation._initial_attempt = None
        attempt = None

        # Deliberately leave the coordinator empty.  The product entry point,
        # not this fixture, must discover and authenticate the completed v1
        # owner chain under a fresh W1 reservation, enter UPGRADE_REQUIRED, and
        # consume that state immediately in upgrade_schema.  Installing a
        # private _view here would bypass the cold-entry behavior under test.
        coordinator = ResourceStoreCoordinator(
            canonical_store_id=_STORE_ID,
            resource_identity=identity,
        )
        service = TMMigrationService(
            resource_identity=identity,
            canonical_store_id=_STORE_ID,
            coordinator=coordinator,
        )
        return _PortableV1Fixture(
            identity=identity,
            coordinator=coordinator,
            service=service,
            prior_database=canonical_database_bytes,
            prior_manifest=manifest_bytes,
            private_directory=root / unsigned.private_directory_name,
        )
    finally:
        if attempt is not None:
            try:
                tm_migration._cleanup_initial_unpublished_stage(attempt)
            finally:
                tm_migration._close_initial_stage_attempt_authorities(attempt)
                reservation._initial_attempt = None
        reservation.release()


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class WindowsPortableSchemaUpgradeTests(unittest.TestCase):
    def test_public_upgrade_schema_publishes_v2_from_portable_v1(self) -> None:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(temporary.name).resolve()
        try:
            fixture = _install_portable_v1(root)
            self.assertIsNone(fixture.coordinator.current_generation)
            connection = sqlite3.connect(
                f"{fixture.identity.canonical_sidecar_path.as_uri()}?mode=ro",
                uri=True,
            )
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM tm_meta WHERE key = 'schema_version'"
                    ).fetchone(),
                    (str(TM_LEGACY_SCHEMA_VERSION),),
                )
            finally:
                connection.close()

            outcome = fixture.service.upgrade_schema(
                fixture.identity.canonical_sidecar_path
            )

            self.assertIs(type(outcome), SchemaUpgradeReport, repr(outcome))
            assert isinstance(outcome, SchemaUpgradeReport)
            self.assertEqual(outcome.from_version, TM_LEGACY_SCHEMA_VERSION)
            self.assertEqual(outcome.to_version, TM_SCHEMA_VERSION)
            self.assertEqual(outcome.canonical_store_id, _STORE_ID)
            self.assertEqual(outcome.activated_generation, 1)
            self.assertEqual(fixture.coordinator.current_generation, 1)
            self.assertEqual(fixture.coordinator.canonical_store_id, _STORE_ID)
            self.assertEqual(outcome.backup_path.read_bytes(), fixture.prior_database)
            self.assertEqual(
                outcome.backup_digest,
                hashlib.sha256(fixture.prior_database).hexdigest(),
            )
            self.assertEqual(
                outcome.success_digest,
                hashlib.sha256(
                    fixture.identity.canonical_sidecar_path.read_bytes()
                ).hexdigest(),
            )
            connection = sqlite3.connect(
                f"{fixture.identity.canonical_sidecar_path.as_uri()}?mode=ro",
                uri=True,
            )
            try:
                meta = dict(connection.execute("SELECT key, value FROM tm_meta"))
                self.assertEqual(meta["schema_version"], str(TM_SCHEMA_VERSION))
                self.assertEqual(meta["generation"], "1")
                self.assertEqual(meta["activation_status"], "ACTIVE")
                self.assertEqual(meta["schema_upgrade_origin"], "schema-upgrade-v1")
                self.assertEqual(
                    connection.execute(
                        "SELECT source_raw, target_raw FROM tm_record ORDER BY record_id"
                    ).fetchall(),
                    [("same", "first"), ("other", "value")],
                )
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchall(),
                    [("ok",)],
                )
                self.assertEqual(
                    connection.execute("PRAGMA foreign_key_check").fetchall(),
                    [],
                )
            finally:
                connection.close()

            reopened = open_canonical_tm_store(
                fixture.identity.configured_jsonl_path,
                expected_resource_id=fixture.identity.resource_id,
            )
            self.assertIsNotNone(reopened)
            assert reopened is not None
            self.assertEqual(reopened.canonical_revision().generation, 1)
            self.assertEqual(
                [record.target_raw for record in reopened.exact_records("same")],
                ["first"],
            )
        finally:
            if root.exists():
                _remove_long_quarantine(root)
            temporary.cleanup()

    def test_exact_interrupted_backup_candidate_is_rebound_without_overwrite(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(temporary.name).resolve()
        try:
            fixture = _install_portable_v1(root)
            backup_name = _schema_backup_name(fixture)
            candidate_name = backup_name + ".candidate"
            candidate_identity = _publish_sibling(
                fixture,
                name=candidate_name,
                payload=fixture.prior_database,
            )

            outcome = fixture.service.upgrade_schema(
                fixture.identity.canonical_sidecar_path
            )

            self.assertIs(type(outcome), SchemaUpgradeReport, repr(outcome))
            assert isinstance(outcome, SchemaUpgradeReport)
            self.assertEqual(outcome.backup_path.name, backup_name)
            self.assertEqual(outcome.backup_path.read_bytes(), fixture.prior_database)
            self.assertFalse((root / candidate_name).exists())
            with fixture.service._acquire_initial_reservation() as reservation:
                _backend, authority, _lease, _lock_name, _lock_payload = (
                    reservation.bound_family_inputs()
                )
                published = authority.inspect_entry(backup_name)
                self.assertIsNotNone(published)
                assert published is not None
                self.assertEqual(published.identity, candidate_identity)
        finally:
            if root.exists():
                _remove_long_quarantine(root)
            temporary.cleanup()

    def test_mismatched_interrupted_candidate_is_preserved_fail_closed(self) -> None:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(temporary.name).resolve()
        try:
            fixture = _install_portable_v1(root)
            candidate_name = _schema_backup_name(fixture) + ".candidate"
            foreign = b"foreign interrupted schema backup candidate\n"
            _publish_sibling(
                fixture,
                name=candidate_name,
                payload=foreign,
            )

            outcome = fixture.service.upgrade_schema(
                fixture.identity.canonical_sidecar_path
            )

            self.assertIs(type(outcome), SchemaUpgradeFailure, repr(outcome))
            self.assertEqual((root / candidate_name).read_bytes(), foreign)
            self.assertEqual(
                fixture.identity.canonical_sidecar_path.read_bytes(),
                fixture.prior_database,
            )
            self.assertEqual(
                fixture.identity.snapshot_manifest_path.read_bytes(),
                fixture.prior_manifest,
            )
        finally:
            if root.exists():
                _remove_long_quarantine(root)
            temporary.cleanup()

    def test_same_bytes_non_private_candidate_is_not_taken_over(self) -> None:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(temporary.name).resolve()
        try:
            fixture = _install_portable_v1(root)
            backup_name = _schema_backup_name(fixture)
            candidate_name = backup_name + ".candidate"
            candidate_identity = _publish_sibling(
                fixture,
                name=candidate_name,
                payload=fixture.prior_database,
                private=False,
            )

            outcome = fixture.service.upgrade_schema(
                fixture.identity.canonical_sidecar_path
            )

            self.assertIs(type(outcome), SchemaUpgradeFailure, repr(outcome))
            self.assertFalse((root / backup_name).exists())
            self.assertEqual(
                fixture.identity.canonical_sidecar_path.read_bytes(),
                fixture.prior_database,
            )
            with fixture.service._acquire_initial_reservation() as reservation:
                _backend, authority, _lease, _lock_name, _lock_payload = (
                    reservation.bound_family_inputs()
                )
                preserved = authority.inspect_entry(candidate_name)
                self.assertIsNotNone(preserved)
                assert preserved is not None
                self.assertEqual(preserved.identity, candidate_identity)
        finally:
            if root.exists():
                _remove_long_quarantine(root)
            temporary.cleanup()

    def test_foreign_stable_backup_is_never_overwritten_or_deleted(self) -> None:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(temporary.name).resolve()
        try:
            fixture = _install_portable_v1(root)
            backup_name = _schema_backup_name(fixture)
            foreign = b"foreign schema backup owner\n"
            _publish_sibling(
                fixture,
                name=backup_name,
                payload=foreign,
            )

            outcome = fixture.service.upgrade_schema(
                fixture.identity.canonical_sidecar_path
            )

            self.assertIs(type(outcome), SchemaUpgradeFailure, repr(outcome))
            self.assertEqual((root / backup_name).read_bytes(), foreign)
            self.assertEqual(
                fixture.identity.canonical_sidecar_path.read_bytes(),
                fixture.prior_database,
            )
            self.assertEqual(
                fixture.identity.snapshot_manifest_path.read_bytes(),
                fixture.prior_manifest,
            )
        finally:
            if root.exists():
                _remove_long_quarantine(root)
            temporary.cleanup()

    def test_process_termination_recovers_prior_or_upgraded_then_retries(self) -> None:
        for phase in ("PREPARED", "DB_REPLACED", "READY"):
            with self.subTest(phase=phase):
                temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                root = Path(temporary.name).resolve()
                try:
                    fixture = _install_portable_v1(root)
                    marker = root / f"schema-upgrade-{phase}.marker"
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            "-m",
                            "tests.windows_tm_schema_upgrade_worker",
                            "--root",
                            str(root),
                            "--phase",
                            phase,
                            "--marker",
                            str(marker),
                        ],
                        cwd=Path(__file__).resolve().parents[1],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    try:
                        _wait_for_marker(marker, process)
                        _terminate_process(process)
                        stdout, stderr = process.communicate(timeout=30.0)
                        self.assertEqual(
                            process.returncode,
                            94,
                            (phase, stdout, stderr),
                        )
                    finally:
                        if process.poll() is None:
                            _terminate_process(process)
                            process.communicate(timeout=30.0)
                    marker.unlink()

                    fresh = ResourceStoreCoordinator(
                        canonical_store_id=_STORE_ID,
                        resource_identity=fixture.identity,
                    )
                    fresh_service = TMMigrationService(
                        resource_identity=fixture.identity,
                        canonical_store_id=_STORE_ID,
                        coordinator=fresh,
                    )
                    recovery_error = None
                    ready_report = None
                    if phase == "READY":
                        ready_report = fresh_service.upgrade_schema(
                            fixture.identity.canonical_sidecar_path
                        )
                        self.assertIs(
                            type(ready_report), SchemaUpgradeReport, repr(ready_report)
                        )
                    else:
                        with fresh_service._acquire_initial_reservation() as reservation:
                            if phase == "DB_REPLACED":
                                with self.assertRaises(
                                    tm_activation_recovery._PortableReplacementRecoveryRequired
                                ) as caught:
                                    fresh.recover_portable_replacement_activation(
                                        **reservation.portable_replacement_recovery_inputs()
                                    )
                                recovery_error = caught.exception
                            else:
                                recovery = fresh.recover_portable_replacement_activation(
                                    **reservation.portable_replacement_recovery_inputs()
                                )
                                self.assertIsNotNone(recovery)
                    connection = sqlite3.connect(
                        fixture.identity.canonical_sidecar_path
                    )
                    try:
                        meta = dict(
                            connection.execute("SELECT key, value FROM tm_meta")
                        )
                    finally:
                        connection.close()
                    schema_version = int(meta["schema_version"])
                    generation = int(meta["generation"])
                    self.assertEqual(
                        (schema_version, generation),
                        {
                            "PREPARED": (1, 0),
                            "DB_REPLACED": (2, 0),
                            "READY": (2, 1),
                        }[phase],
                    )
                    if phase == "DB_REPLACED":
                        assert isinstance(
                            recovery_error,
                            tm_activation_recovery._PortableReplacementRecoveryRequired,
                        )
                        self.assertEqual(
                            recovery_error.code,
                            "ACTIVATION.RECOVERY_REQUIRED",
                        )
                        locator = recovery_error.recovery_locator
                        self.assertEqual(
                            locator.expected_digest,
                            hashlib.sha256(fixture.prior_database).hexdigest(),
                        )
                        self.assertEqual(locator.path.read_bytes(), fixture.prior_database)
                        continue
                    if phase == "PREPARED":
                        self.assertEqual(fresh.state, "UPGRADE_REQUIRED")
                        self.assertEqual(fresh.current_generation, 0)
                        self.assertEqual(
                            fixture.identity.canonical_sidecar_path.read_bytes(),
                            fixture.prior_database,
                        )
                        retry = fresh_service.upgrade_schema(
                            fixture.identity.canonical_sidecar_path
                        )
                        self.assertIs(type(retry), SchemaUpgradeReport, repr(retry))
                    else:
                        self.assertEqual(fresh.state, "READY")
                        self.assertEqual(fresh.current_generation, 1)
                        assert isinstance(ready_report, SchemaUpgradeReport)
                        self.assertEqual(ready_report.activated_generation, 1)

                    reopened = open_canonical_tm_store(
                        fixture.identity.configured_jsonl_path,
                        expected_resource_id=fixture.identity.resource_id,
                    )
                    self.assertIsNotNone(reopened)
                    assert reopened is not None
                    self.assertEqual(reopened.canonical_revision().generation, 1)
                finally:
                    if root.exists():
                        _remove_long_quarantine(root)
                    temporary.cleanup()

    def test_portable_v1_never_opens_as_query_ready(self) -> None:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(temporary.name).resolve()
        try:
            fixture = _install_portable_v1(root)

            with self.assertRaises(ValueError):
                open_canonical_tm_store(
                    fixture.identity.configured_jsonl_path,
                    expected_resource_id=fixture.identity.resource_id,
                )

            self.assertEqual(
                fixture.identity.canonical_sidecar_path.read_bytes(),
                fixture.prior_database,
            )
        finally:
            if root.exists():
                _remove_long_quarantine(root)
            temporary.cleanup()

    def test_missing_portable_predecessor_fails_without_mutation(self) -> None:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(temporary.name).resolve()
        try:
            fixture = _install_portable_v1(root)
            extended_private = "\\\\?\\" + str(fixture.private_directory)
            for name in os.listdir(extended_private):
                os.unlink(os.path.join(extended_private, name))
            os.rmdir(extended_private)
            before = _tree_bytes(root)

            with self.assertRaises(tm_sqlite_store.ActivationPreparationError):
                fixture.service.upgrade_schema(
                    fixture.identity.canonical_sidecar_path
                )

            self.assertEqual(_tree_bytes(root), before)
            self.assertEqual(
                fixture.identity.canonical_sidecar_path.read_bytes(),
                fixture.prior_database,
            )
            self.assertEqual(
                fixture.identity.snapshot_manifest_path.read_bytes(),
                fixture.prior_manifest,
            )
            self.assertIsNone(fixture.coordinator.current_generation)
        finally:
            if root.exists():
                _remove_long_quarantine(root)
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
