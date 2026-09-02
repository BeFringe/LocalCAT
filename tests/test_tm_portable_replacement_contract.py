"""Focused WA-06 5.10a replacement codec and namespace contract tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import unittest

from platform_fs_contracts import (
    PrivateProofObjectRole,
    WINDOWS_PRIVATE_PROOF_SCHEMA,
    WINDOWS_PRIVATE_SECURITY_PROFILE_ID,
    WindowsPrivateProof,
)
from tm_activation_journal import (
    ActivationPreparationError,
    _PORTABLE_REPLACEMENT_CURRENT_NAME,
    _PORTABLE_REPLACEMENT_PHASES,
    _PORTABLE_REPLACEMENT_VERSION,
    _PortableReplacementBackupProof,
    _PortableReplacementRecord,
    _PortableReplacementUnsigned,
    _create_portable_replacement_record,
    _parse_portable_replacement_namespace,
    _parse_portable_replacement_record_bytes,
    _portable_replacement_backup_name,
    _portable_replacement_namespace_limits,
    _portable_replacement_owner_context_sha256,
    _portable_replacement_phase_name,
    _serialize_portable_replacement_record,
)
from tm_content_attestation import (
    ContentSemanticFacts,
    LOGICAL_CLOSURE_VERSION,
    PortableContentFileProof,
    _create_portable_active_content_attestation,
    _create_portable_sealed_content_attestation,
)


class PortableReplacementContractTests(unittest.TestCase):
    def _semantic(self) -> ContentSemanticFacts:
        return ContentSemanticFacts(
            schema_version=2,
            schema_digest="1" * 64,
            fold_version="fold-v1",
            index_version="candidate-v1",
            candidate_index_kind="FTS5_TRIGRAM",
            fts5_available=True,
            sqlite_runtime_version="3.0",
            unicode_runtime_version="15.0",
            journal_mode="delete",
            synchronous="FULL",
            foreign_keys=True,
            busy_timeout_ms=5000,
            wal_enabled=False,
            extension_loading_enabled=False,
            record_count=3,
            receipt_boundary_record_count=1,
            origin_batch_count=1,
            origin_batch_id="migration." + "2" * 64,
            origin_batch_kind="migration",
            exported_revision=1,
            fts_count=3,
            receipt_boundary_fts_count=1,
            gram_counts=((1, 1), (2, 0)),
            exact_parity_digest="3" * 64,
            logical_closure_version=LOGICAL_CLOSURE_VERSION,
            logical_closure_digest="4" * 64,
        )

    def _file(self, label: bytes, size: int = 8) -> PortableContentFileProof:
        return PortableContentFileProof(
            size=size,
            sha256=hashlib.sha256(label).hexdigest(),
        )

    def _unsigned(
        self,
        *,
        operation: str = "REPLACEMENT",
        preparation_id: str = "replacement.preparation.1",
        journal_id: str = "replacement.journal.1",
        phase: str = "PREPARED",
        predecessor_digest: str = "0" * 64,
        prior_generation: int = 7,
        prior_store: str = "store.prior",
        candidate_store: str | None = None,
        active: bool = False,
    ) -> _PortableReplacementUnsigned:
        if candidate_store is None:
            candidate_store = (
                prior_store if operation == "SCHEMA_UPGRADE" else "store.candidate"
            )
        prior_database = self._file(b"prior database", 31)
        prior_manifest = self._file(b"prior manifest", 17)
        source = self._file(b"source jsonl", 23)
        candidate_database = self._file(b"candidate database", 37)
        candidate_manifest = self._file(b"candidate manifest", 19)
        semantic = self._semantic()
        prior_active = _create_portable_active_content_attestation(
            sealed_attestation_digest="5" * 64,
            journal_id="prior.journal",
            resource_id="tm.primary",
            target_identity="6" * 64,
            canonical_store_id=prior_store,
            snapshot_receipt_digest="7" * 64,
            generation=prior_generation,
            activation_digest="8" * 64,
            database=prior_database,
            manifest=prior_manifest,
            source=source,
            semantic_facts=semantic,
        )
        sealed = _create_portable_sealed_content_attestation(
            resource_id="tm.primary",
            target_identity="6" * 64,
            canonical_store_id=candidate_store,
            snapshot_receipt_digest="9" * 64,
            expected_prior_generation=prior_generation,
            evidence_digest="a" * 64,
            database=candidate_database,
            manifest=candidate_manifest,
            source=source,
            semantic_facts=semantic,
        )
        active_attestation = (
            _create_portable_active_content_attestation(
                sealed_attestation_digest=sealed.attestation_digest,
                journal_id=journal_id,
                resource_id="tm.primary",
                target_identity="6" * 64,
                canonical_store_id=candidate_store,
                snapshot_receipt_digest=sealed.snapshot_receipt_digest,
                generation=prior_generation + 1,
                activation_digest="b" * 64,
                database=candidate_database,
                manifest=candidate_manifest,
                source=source,
                semantic_facts=semantic,
            )
            if active
            else None
        )
        backups = (
            _PortableReplacementBackupProof(
                asset_kind="DATABASE",
                backup_name=_portable_replacement_backup_name(
                    preparation_id,
                    "DATABASE",
                ),
                content=prior_database,
            ),
            _PortableReplacementBackupProof(
                asset_kind="MANIFEST",
                backup_name=_portable_replacement_backup_name(
                    preparation_id,
                    "MANIFEST",
                ),
                content=prior_manifest,
            ),
        )
        return _PortableReplacementUnsigned(
            replacement_version=_PORTABLE_REPLACEMENT_VERSION,
            operation=operation,
            phase=phase,
            predecessor_digest=predecessor_digest,
            journal_id=journal_id,
            preparation_id=preparation_id,
            registry_namespace="registry.test",
            token_id="token.test",
            token_version="activation-token-v1",
            activation_nonce="nonce.test",
            artifact_id="artifact.test",
            artifact_seal_digest="c" * 64,
            sealed_stage_digest="d" * 64,
            resource_id="tm.primary",
            target_identity="6" * 64,
            prior_authority_digest="0" * 64,
            prior_canonical_store_id=prior_store,
            prior_generation=prior_generation,
            prior_active_content_attestation=prior_active,
            backup_proofs=backups,
            candidate_canonical_store_id=candidate_store,
            next_generation=prior_generation + 1,
            gate_b_grant_digest="e" * 64,
            evidence_digest="a" * 64,
            source_jsonl_digest=source.sha256,
            new_receipt_id="snapshot.new",
            new_manifest_digest=candidate_manifest.sha256,
            candidate_stage_db_name="replacement.sqlite3.stage",
            candidate_manifest_temp_name="replacement.manifest.tmp",
            canonical_database_name="tm.primary.sqlite3",
            canonical_manifest_name="tm.primary.manifest.json",
            private_directory_name=".localcat-activation-private-v1.test",
            device_key_name="device.key",
            lock_payload_digest="f" * 64,
            sealed_content_attestation=sealed,
            active_content_attestation=active_attestation,
        )

    def _proof(self, unsigned: _PortableReplacementUnsigned) -> WindowsPrivateProof:
        return WindowsPrivateProof(
            schema=WINDOWS_PRIVATE_PROOF_SCHEMA,
            object_role=PrivateProofObjectRole.PRIVATE_DIRECTORY,
            security_profile_id=WINDOWS_PRIVATE_SECURITY_PROFILE_ID,
            owner_sid_sha256=b"o" * 32,
            authority_descriptor_sha256=b"d" * 32,
            owner_context_sha256=_portable_replacement_owner_context_sha256(unsigned),
            device_key_id=b"k" * 32,
            device_secret_mac=b"m" * 32,
        )

    def _record(
        self,
        unsigned: _PortableReplacementUnsigned,
    ) -> _PortableReplacementRecord:
        return _create_portable_replacement_record(unsigned, self._proof(unsigned))

    def _chain(self) -> tuple[_PortableReplacementRecord, ...]:
        prepared = self._record(self._unsigned())
        records = [prepared]
        for phase in _PORTABLE_REPLACEMENT_PHASES[1:]:
            predecessor = records[-1]
            unsigned = replace(
                predecessor.unsigned,
                phase=phase,
                predecessor_digest=predecessor.record_digest,
                active_content_attestation=(
                    self._unsigned(
                        phase="MANIFEST_PUBLISHED",
                        active=True,
                    ).active_content_attestation
                    if phase not in {"PREPARED", "DB_REPLACED"}
                    else None
                ),
            )
            records.append(self._record(unsigned))
        return tuple(records)

    def _backup_mapping(
        self,
        record: _PortableReplacementRecord,
    ) -> dict[str, PortableContentFileProof]:
        return {
            proof.backup_name: proof.content
            for proof in record.unsigned.backup_proofs
        }

    def test_round_trip_is_canonical_strict_and_portable(self) -> None:
        record = self._record(self._unsigned())
        serialized = _serialize_portable_replacement_record(record)
        self.assertEqual(_parse_portable_replacement_record_bytes(serialized), record)
        self.assertEqual(
            serialized,
            json.dumps(
                json.loads(serialized),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8") + b"\n",
        )
        forbidden_keys = {
            "device", "dev", "inode", "st_dev", "st_ino", "file_id",
            "fileid", "volume", "volume_id", "volume_serial",
        }

        def inspect_keys(value: object) -> None:
            if type(value) is dict:
                for key, child in value.items():
                    self.assertNotIn(key.lower(), forbidden_keys)
                    inspect_keys(child)
            elif type(value) is list:
                for child in value:
                    inspect_keys(child)

        inspect_keys(json.loads(serialized))

    def test_codec_rejects_tamper_unknown_fields_and_noncanonical_bytes(self) -> None:
        record = self._record(self._unsigned())
        mapping = json.loads(_serialize_portable_replacement_record(record))
        mutations = []
        changed_generation = dict(mapping)
        changed_generation["next_generation"] = 99
        mutations.append(changed_generation)
        changed_backup = json.loads(json.dumps(mapping))
        changed_backup["backup_proofs"][0]["content"]["sha256"] = "1" * 64
        mutations.append(changed_backup)
        unknown = dict(mapping)
        unknown["legacy_generation"] = 7
        mutations.append(unknown)
        for mutation in mutations:
            payload = json.dumps(
                mutation,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8") + b"\n"
            with self.subTest(keys=set(mutation)):
                with self.assertRaises(ActivationPreparationError) as caught:
                    _parse_portable_replacement_record_bytes(payload)
                self.assertEqual(caught.exception.code, "ACTIVATION.REPLACEMENT_PARSE_INVALID")
        with self.assertRaises(ActivationPreparationError):
            _parse_portable_replacement_record_bytes(
                json.dumps(mapping, indent=2, sort_keys=True).encode("utf-8") + b"\n"
            )

    def test_exact_prior_candidate_generation_and_phase_binding(self) -> None:
        unsigned = self._unsigned()
        invalid = (
            {"next_generation": unsigned.prior_generation + 2},
            {"candidate_canonical_store_id": unsigned.prior_canonical_store_id},
            {"prior_generation": unsigned.prior_generation + 1},
            {
                "phase": "DB_REPLACED",
                "active_content_attestation": self._unsigned(
                    phase="MANIFEST_PUBLISHED",
                    active=True,
                ).active_content_attestation,
            },
            {"phase": "READY", "active_content_attestation": None},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises((TypeError, ValueError)):
                    replace(unsigned, **change)

    def test_schema_upgrade_discriminator_preserves_store_id_only(self) -> None:
        upgrade = self._unsigned(operation="SCHEMA_UPGRADE")
        self.assertEqual(
            upgrade.candidate_canonical_store_id,
            upgrade.prior_canonical_store_id,
        )
        self.assertEqual(
            _parse_portable_replacement_record_bytes(
                _serialize_portable_replacement_record(self._record(upgrade))
            ).unsigned.operation,
            "SCHEMA_UPGRADE",
        )
        with self.assertRaises(ValueError):
            replace(upgrade, candidate_canonical_store_id="store.foreign")
        with self.assertRaises(ValueError):
            replace(
                self._unsigned(),
                candidate_canonical_store_id="store.prior",
            )

    def test_active_transaction_may_change_database_and_semantic_closure(self) -> None:
        unsigned = self._unsigned(
            phase="MANIFEST_PUBLISHED",
            active=True,
        )
        active = unsigned.active_content_attestation
        self.assertIsNotNone(active)
        transaction_active = _create_portable_active_content_attestation(
            sealed_attestation_digest=active.sealed_attestation_digest,
            journal_id=active.journal_id,
            resource_id=active.resource_id,
            target_identity=active.target_identity,
            canonical_store_id=active.canonical_store_id,
            snapshot_receipt_digest=active.snapshot_receipt_digest,
            generation=active.generation,
            activation_digest="0" * 64,
            database=self._file(b"database after activation transaction", 53),
            manifest=active.manifest,
            source=active.source,
            semantic_facts=replace(
                active.semantic_facts,
                exact_parity_digest="9" * 64,
            ),
        )
        closed = replace(
            unsigned,
            active_content_attestation=transaction_active,
        )
        self.assertNotEqual(
            closed.active_content_attestation.database,
            closed.sealed_content_attestation.database,
        )
        self.assertNotEqual(
            closed.active_content_attestation.semantic_facts,
            closed.sealed_content_attestation.semantic_facts,
        )

        foreign_manifest = _create_portable_active_content_attestation(
            sealed_attestation_digest=transaction_active.sealed_attestation_digest,
            journal_id=transaction_active.journal_id,
            resource_id=transaction_active.resource_id,
            target_identity=transaction_active.target_identity,
            canonical_store_id=transaction_active.canonical_store_id,
            snapshot_receipt_digest=transaction_active.snapshot_receipt_digest,
            generation=transaction_active.generation,
            activation_digest=transaction_active.activation_digest,
            database=transaction_active.database,
            manifest=self._file(b"foreign manifest", 29),
            source=transaction_active.source,
            semantic_facts=transaction_active.semantic_facts,
        )
        with self.assertRaises(ValueError):
            replace(unsigned, active_content_attestation=foreign_manifest)

    def test_namespace_is_bounded_prefix_and_ready_cleanup(self) -> None:
        chain = self._chain()
        prepared, database, manifest, generation, ready = chain
        backups = self._backup_mapping(prepared)
        empty = _parse_portable_replacement_namespace(
            {},
            {},
            base_authority_digest="0" * 64,
        )
        self.assertEqual(empty.state, "EMPTY")
        pending = _parse_portable_replacement_namespace(
            {
                _portable_replacement_phase_name("PREPARED"): prepared,
                _portable_replacement_phase_name("DB_REPLACED"): database,
            },
            backups,
            base_authority_digest="0" * 64,
        )
        self.assertEqual((pending.state, pending.highest_pending_phase), ("PENDING", "DB_REPLACED"))
        cleanup_records = {
            _portable_replacement_phase_name(record.unsigned.phase): record
            for record in chain
        }
        cleanup = _parse_portable_replacement_namespace(
            cleanup_records,
            backups,
            base_authority_digest="0" * 64,
        )
        self.assertEqual(cleanup.state, "READY_CLEANUP")
        current = _parse_portable_replacement_namespace(
            {_PORTABLE_REPLACEMENT_CURRENT_NAME: ready},
            {},
            base_authority_digest=None,
        )
        self.assertEqual(current.state, "CURRENT")
        limits = _portable_replacement_namespace_limits(prepared.unsigned.backup_proofs)
        self.assertEqual(limits.maximum_entries, 7)

        with self.assertRaises(ValueError):
            _parse_portable_replacement_namespace(
                {_portable_replacement_phase_name("DB_REPLACED"): database},
                backups,
                base_authority_digest="0" * 64,
            )
        with self.assertRaises(ValueError):
            _parse_portable_replacement_namespace(
                {"foreign.json": prepared},
                backups,
                base_authority_digest="0" * 64,
            )
        altered_backups = dict(backups)
        altered_backups[next(iter(altered_backups))] = self._file(b"foreign")
        with self.assertRaises(ValueError):
            _parse_portable_replacement_namespace(
                {_portable_replacement_phase_name("PREPARED"): prepared},
                altered_backups,
                base_authority_digest="0" * 64,
            )

    def test_backup_names_are_strictly_derived_and_do_not_persist_file_ids(self) -> None:
        unsigned = self._unsigned()
        database, manifest = unsigned.backup_proofs
        self.assertNotEqual(database.backup_name, manifest.backup_name)
        self.assertEqual(
            database.backup_name,
            _portable_replacement_backup_name(unsigned.preparation_id, "DATABASE"),
        )
        self.assertEqual(set(database.content.__dataclass_fields__), {"size", "sha256"})
        with self.assertRaises(ValueError):
            replace(
                unsigned,
                backup_proofs=(
                    replace(database, backup_name="foreign.backup"),
                    manifest,
                ),
            )


if __name__ == "__main__":
    unittest.main()
