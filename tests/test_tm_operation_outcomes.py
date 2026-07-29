from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
import json
from pathlib import Path
from typing import cast
import unittest

from tm_contracts import (
    AssetKind,
    AssetPreservationEvidence,
    AssetPreservationState,
    DiagnosticDisposition,
    ExportDiagnostic,
    ExportFailure,
    ExportOutcome,
    ExportReport,
    MigrationDiagnostic,
    MigrationFailure,
    MigrationOutcome,
    MigrationPreflight,
    MigrationReport,
    RecoveryLocator,
    SchemaUpgradeFailure,
    SchemaUpgradeOutcome,
    SchemaUpgradeReport,
    SnapshotReceipt,
    contract_from_json,
    contract_to_json,
    snapshot_receipt_digest,
)


_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_DIGEST_D = "d" * 64


def _unchanged(
    asset_kind: AssetKind,
    digest: str,
) -> AssetPreservationEvidence:
    return AssetPreservationEvidence(
        asset_kind=asset_kind,
        state=AssetPreservationState.VERIFIED_UNCHANGED,
        before_digest=digest,
        observed_digest=digest,
    )


def _not_applicable(
    asset_kind: AssetKind,
) -> AssetPreservationEvidence:
    return AssetPreservationEvidence(
        asset_kind=asset_kind,
        state=AssetPreservationState.NOT_APPLICABLE,
        before_digest=None,
        observed_digest=None,
    )


def _migration_diagnostic(
    *,
    line_number: int = 4,
    disposition: DiagnosticDisposition = DiagnosticDisposition.REJECTED,
) -> MigrationDiagnostic:
    return MigrationDiagnostic(
        code="ROW.INVALID_SHAPE",
        stage="PREFLIGHT.PARSE",
        line_number=line_number,
        record_id=None,
        disposition=disposition,
        safe_summary="ROW_SKIPPED_INVALID_SHAPE",
    )


def _export_diagnostic(
    *,
    record_id: int = 7,
    disposition: DiagnosticDisposition = DiagnosticDisposition.REJECTED,
) -> ExportDiagnostic:
    return ExportDiagnostic(
        code="RECORD.INVALID_SHAPE",
        record_id=record_id,
        disposition=disposition,
        safe_summary="RECORD_SKIPPED_INVALID_SHAPE",
    )


def _migration_receipt() -> SnapshotReceipt:
    return SnapshotReceipt(
        snapshot_id="snapshot.migration.1",
        resource_id="tm.primary",
        canonical_store_id="store.primary.v1",
        exported_revision=0,
        jsonl_digest=_DIGEST_A,
        record_count=8,
    )


def _export_receipt() -> SnapshotReceipt:
    return SnapshotReceipt(
        snapshot_id="snapshot.export.4",
        resource_id="tm.primary",
        canonical_store_id="store.primary.v1",
        exported_revision=4,
        jsonl_digest=_DIGEST_B,
        record_count=8,
    )


def _preflight() -> MigrationPreflight:
    return MigrationPreflight(
        source_digest=_DIGEST_A,
        valid_count=8,
        invalid_count=1,
        duplicate_source_count=2,
        variant_count=3,
        diagnostics=(_migration_diagnostic(),),
    )


def _migration_report() -> MigrationReport:
    return MigrationReport(
        resource_id="tm.primary",
        canonical_store_id="store.primary.v1",
        source_digest=_DIGEST_A,
        snapshot_receipt=_migration_receipt(),
        migrated_count=8,
        variant_count=3,
        skipped_count=1,
        diagnostics=(_migration_diagnostic(),),
        activated_generation=0,
        canonical_exact_available=True,
        context_available=False,
        fuzzy_available=False,
    )


def _migration_failure() -> MigrationFailure:
    return MigrationFailure(
        stage="ACTIVATION.RELOAD",
        error_code="TM.ACTIVATION_RELOAD_FAILED",
        retryable=True,
        diagnostics=(_migration_diagnostic(),),
        active_generation=None,
        original_source_preservation=_unchanged(
            AssetKind.ORIGINAL_SOURCE,
            _DIGEST_A,
        ),
        active_store_preservation=_not_applicable(AssetKind.ACTIVE_STORE),
        recovery_locators=(),
    )


def _export_report() -> ExportReport:
    receipt = _export_receipt()
    return ExportReport(
        exported_count=8,
        skipped_count=1,
        destination_digest=_DIGEST_B,
        canonical_generation=0,
        exported_revision=4,
        snapshot_id=receipt.snapshot_id,
        snapshot_receipt_digest=snapshot_receipt_digest(receipt),
        snapshot_receipt=receipt,
        diagnostics=(_export_diagnostic(),),
    )


def _export_failure() -> ExportFailure:
    return ExportFailure(
        stage="EXPORT.PUBLISH",
        error_code="TM.EXPORT_PUBLISH_FAILED",
        retryable=True,
        diagnostics=(_export_diagnostic(),),
        previous_destination_preservation=_unchanged(
            AssetKind.EXPORT_DESTINATION,
            _DIGEST_B,
        ),
        recovery_locators=(),
    )


def _upgrade_report() -> SchemaUpgradeReport:
    return SchemaUpgradeReport(
        canonical_store_id="store.primary.v1",
        from_version=1,
        to_version=2,
        backup_path=Path("/catalog/tm.sqlite3.schema-v1.backup"),
        backup_digest=_DIGEST_A,
        success_digest=_DIGEST_B,
        activated_generation=0,
    )


def _upgrade_failure() -> SchemaUpgradeFailure:
    return SchemaUpgradeFailure(
        stage="SCHEMA.UPGRADE",
        error_code="TM.SCHEMA_UPGRADE_FAILED",
        retryable=True,
        active_generation=0,
        active_store_preservation=_unchanged(
            AssetKind.ACTIVE_STORE,
            _DIGEST_A,
        ),
        recovery_locators=(),
    )


class TMOperationOutcomeContractTests(unittest.TestCase):
    def test_all_portable_operation_contracts_round_trip_strictly(self) -> None:
        contracts = (
            _unchanged(AssetKind.ORIGINAL_SOURCE, _DIGEST_A),
            RecoveryLocator(
                path=Path("/catalog/recovery/tm.jsonl"),
                asset_kind=AssetKind.ORIGINAL_SOURCE,
                expected_digest=_DIGEST_A,
            ),
            _migration_diagnostic(),
            _export_diagnostic(),
            _preflight(),
            _migration_report(),
            _migration_failure(),
            _export_report(),
            _export_failure(),
            _upgrade_report(),
            _upgrade_failure(),
        )

        for contract in contracts:
            with self.subTest(contract=type(contract).__name__):
                encoded = contract_to_json(contract)
                decoded = contract_from_json(encoded)
                self.assertEqual(decoded, contract)
                self.assertEqual(contract_to_json(decoded), encoded)

                envelope = json.loads(encoded)
                envelope["payload"]["source"] = "must-not-be-accepted"
                with self.assertRaisesRegex(ValueError, "unexpected fields"):
                    contract_from_json(
                        json.dumps(
                            envelope,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    )

    def test_operation_contracts_are_frozen_and_outcomes_are_explicit(
        self,
    ) -> None:
        report = _migration_report()
        with self.assertRaises(FrozenInstanceError):
            report.migrated_count = 9  # pyright: ignore[reportAttributeAccessIssue]

        migration_success: MigrationOutcome = report
        migration_failure: MigrationOutcome = _migration_failure()
        export_success: ExportOutcome = _export_report()
        export_failure: ExportOutcome = _export_failure()
        upgrade_success: SchemaUpgradeOutcome = _upgrade_report()
        upgrade_failure: SchemaUpgradeOutcome = _upgrade_failure()
        self.assertIsInstance(migration_success, MigrationReport)
        self.assertIsInstance(migration_failure, MigrationFailure)
        self.assertIsInstance(export_success, ExportReport)
        self.assertIsInstance(export_failure, ExportFailure)
        self.assertIsInstance(upgrade_success, SchemaUpgradeReport)
        self.assertIsInstance(upgrade_failure, SchemaUpgradeFailure)

    def test_asset_preservation_evidence_is_closed(self) -> None:
        unchanged = _unchanged(AssetKind.ORIGINAL_SOURCE, _DIGEST_A)
        with self.assertRaisesRegex(ValueError, "must match"):
            replace(unchanged, observed_digest=_DIGEST_B)
        with self.assertRaisesRegex(ValueError, "must differ"):
            AssetPreservationEvidence(
                asset_kind=AssetKind.ACTIVE_STORE,
                state=AssetPreservationState.VERIFIED_CHANGED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_A,
            )
        with self.assertRaisesRegex(ValueError, "observed digest"):
            AssetPreservationEvidence(
                asset_kind=AssetKind.ACTIVE_STORE,
                state=AssetPreservationState.UNVERIFIED,
                before_digest=_DIGEST_A,
                observed_digest=_DIGEST_B,
            )
        with self.assertRaisesRegex(ValueError, "must omit digests"):
            replace(
                _not_applicable(AssetKind.ACTIVE_STORE),
                before_digest=_DIGEST_A,
            )

    def test_public_diagnostics_are_safe_locatable_and_dispositioned(
        self,
    ) -> None:
        migration_fields = {item.name for item in fields(MigrationDiagnostic)}
        export_fields = {item.name for item in fields(ExportDiagnostic)}
        forbidden = {"source", "source_raw", "target", "target_raw", "query"}
        self.assertTrue(migration_fields.isdisjoint(forbidden))
        self.assertTrue(export_fields.isdisjoint(forbidden))

        with self.assertRaisesRegex(ValueError, "safe diagnostic identifier"):
            replace(
                _migration_diagnostic(),
                safe_summary="source=customer secret text",
            )
        with self.assertRaisesRegex(ValueError, "safe diagnostic identifier"):
            replace(
                _export_diagnostic(),
                safe_summary="target: translated body",
            )
        with self.assertRaisesRegex(ValueError, "line or record"):
            replace(
                _migration_diagnostic(),
                line_number=None,
                record_id=None,
            )
        with self.assertRaisesRegex(ValueError, "record id"):
            replace(_export_diagnostic(), record_id=None)
        with self.assertRaisesRegex(TypeError, "DiagnosticDisposition"):
            replace(
                _migration_diagnostic(),
                disposition=cast(
                    DiagnosticDisposition,
                    cast(object, "REJECTED"),
                ),
            )

    def test_codec_revalidates_forged_and_mutated_contracts(self) -> None:
        report = _migration_report()
        forged_report = object.__new__(MigrationReport)
        for field in fields(MigrationReport):
            object.__setattr__(
                forged_report,
                field.name,
                getattr(report, field.name),
            )
        object.__setattr__(forged_report, "source_digest", _DIGEST_B)
        with self.assertRaisesRegex(ValueError, "source digest"):
            contract_to_json(forged_report)

        failure = _migration_failure()
        object.__setattr__(
            failure.original_source_preservation,
            "observed_digest",
            _DIGEST_B,
        )
        with self.assertRaisesRegex(ValueError, "must match"):
            contract_to_json(failure)

    def test_rejected_diagnostic_counts_use_unique_locations(self) -> None:
        migration_rejected = _migration_diagnostic()
        migration_same_location = replace(
            migration_rejected,
            code="ROW.INVALID_TYPE",
            safe_summary="ROW_SKIPPED_INVALID_TYPE",
        )
        with self.assertRaisesRegex(ValueError, "rejected.*location"):
            replace(
                _preflight(),
                invalid_count=2,
                diagnostics=(
                    migration_rejected,
                    migration_same_location,
                ),
            )

        migration_warning = replace(
            migration_rejected,
            code="ROW.WARNING",
            disposition=DiagnosticDisposition.WARNING,
            safe_summary="ROW_WARNING",
        )
        warning_at_rejected_location = replace(
            _preflight(),
            diagnostics=(migration_rejected, migration_warning),
        )
        self.assertEqual(warning_at_rejected_location.invalid_count, 1)

        export_rejected = _export_diagnostic()
        export_same_location = replace(
            export_rejected,
            code="RECORD.INVALID_TYPE",
            safe_summary="RECORD_SKIPPED_INVALID_TYPE",
        )
        with self.assertRaisesRegex(ValueError, "rejected.*location"):
            replace(
                _export_report(),
                skipped_count=2,
                diagnostics=(export_rejected, export_same_location),
            )

    def test_preflight_counts_diagnostics_and_order_must_close(self) -> None:
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            replace(_preflight(), source_digest="not-a-digest")
        with self.assertRaisesRegex(TypeError, "must be an integer"):
            replace(_preflight(), valid_count=False)
        with self.assertRaisesRegex(ValueError, "duplicate source"):
            replace(_preflight(), duplicate_source_count=4, variant_count=3)
        with self.assertRaisesRegex(ValueError, "variant"):
            replace(_preflight(), variant_count=9)
        with self.assertRaisesRegex(ValueError, "invalid count"):
            replace(_preflight(), invalid_count=0)
        with self.assertRaisesRegex(ValueError, "must describe at least one"):
            replace(
                _preflight(),
                valid_count=0,
                invalid_count=0,
                duplicate_source_count=0,
                variant_count=0,
                diagnostics=(),
            )
        later = _migration_diagnostic(line_number=5)
        earlier = _migration_diagnostic(line_number=2)
        with self.assertRaisesRegex(ValueError, "stable order"):
            replace(
                _preflight(),
                invalid_count=2,
                diagnostics=(later, earlier),
            )
        with self.assertRaisesRegex(ValueError, "unique"):
            replace(
                _preflight(),
                invalid_count=2,
                diagnostics=(earlier, earlier),
            )

    def test_migration_success_closes_lineage_generation_and_counts(
        self,
    ) -> None:
        report = _migration_report()
        self.assertEqual(report.activated_generation, 0)
        with self.assertRaisesRegex(ValueError, "resource"):
            replace(report, resource_id="tm.other")
        with self.assertRaisesRegex(ValueError, "canonical store"):
            replace(report, canonical_store_id="store.other")
        with self.assertRaisesRegex(ValueError, "source digest"):
            replace(report, source_digest=_DIGEST_B)
        with self.assertRaisesRegex(ValueError, "record count"):
            replace(report, migrated_count=7)
        with self.assertRaisesRegex(ValueError, "skipped count"):
            replace(report, skipped_count=0)
        with self.assertRaisesRegex(ValueError, "canonical exact"):
            replace(report, canonical_exact_available=False)
        with self.assertRaisesRegex(TypeError, "must be a boolean"):
            replace(
                report,
                fuzzy_available=cast(bool, cast(object, 1)),
            )

    def test_migration_failure_requires_evidence_and_fail_stop_locator(
        self,
    ) -> None:
        first_activation_failure = _migration_failure()
        self.assertIsNone(first_activation_failure.active_generation)

        forged_preservation = object.__new__(AssetPreservationEvidence)
        object.__setattr__(
            forged_preservation,
            "asset_kind",
            AssetKind.ORIGINAL_SOURCE,
        )
        object.__setattr__(
            forged_preservation,
            "state",
            AssetPreservationState.VERIFIED_UNCHANGED,
        )
        object.__setattr__(
            forged_preservation,
            "before_digest",
            _DIGEST_A,
        )
        object.__setattr__(
            forged_preservation,
            "observed_digest",
            _DIGEST_B,
        )
        with self.assertRaisesRegex(ValueError, "must match"):
            replace(
                first_activation_failure,
                original_source_preservation=forged_preservation,
            )

        unverified_source = AssetPreservationEvidence(
            asset_kind=AssetKind.ORIGINAL_SOURCE,
            state=AssetPreservationState.UNVERIFIED,
            before_digest=_DIGEST_A,
            observed_digest=None,
        )
        with self.assertRaisesRegex(ValueError, "recovery locator"):
            replace(
                first_activation_failure,
                retryable=False,
                original_source_preservation=unverified_source,
            )
        locator = RecoveryLocator(
            path=Path("/catalog/recovery/tm.jsonl"),
            asset_kind=AssetKind.ORIGINAL_SOURCE,
            expected_digest=_DIGEST_A,
        )
        with self.assertRaisesRegex(ValueError, "must be empty"):
            replace(
                first_activation_failure,
                recovery_locators=(locator,),
            )
        recovered = replace(
            first_activation_failure,
            retryable=False,
            original_source_preservation=unverified_source,
            recovery_locators=(locator,),
        )
        self.assertEqual(recovered.recovery_locators, (locator,))
        with self.assertRaisesRegex(ValueError, "fail-stop"):
            replace(recovered, retryable=True)
        mismatched_locator = replace(
            locator,
            expected_digest=_DIGEST_B,
        )
        with self.assertRaisesRegex(ValueError, "expected digest"):
            replace(
                recovered,
                recovery_locators=(mismatched_locator,),
            )
        with self.assertRaisesRegex(ValueError, "absolute normalized"):
            replace(locator, path=Path("relative/recovery"))

    def test_export_success_and_failure_close_receipt_and_recovery(
        self,
    ) -> None:
        report = _export_report()
        self.assertEqual(report.canonical_generation, 0)
        with self.assertRaisesRegex(ValueError, "destination digest"):
            replace(report, destination_digest=_DIGEST_A)
        with self.assertRaisesRegex(ValueError, "exported count"):
            replace(report, exported_count=7)
        with self.assertRaisesRegex(ValueError, "exported revision"):
            replace(report, exported_revision=3)
        with self.assertRaisesRegex(ValueError, "snapshot id"):
            replace(report, snapshot_id="snapshot.other")
        with self.assertRaisesRegex(ValueError, "receipt digest"):
            replace(report, snapshot_receipt_digest=_DIGEST_D)
        with self.assertRaisesRegex(ValueError, "skipped count"):
            replace(report, skipped_count=0)

        unverified_destination = AssetPreservationEvidence(
            asset_kind=AssetKind.EXPORT_DESTINATION,
            state=AssetPreservationState.UNVERIFIED,
            before_digest=_DIGEST_B,
            observed_digest=None,
        )
        failure = _export_failure()
        with self.assertRaisesRegex(ValueError, "recovery locator"):
            replace(
                failure,
                retryable=False,
                previous_destination_preservation=unverified_destination,
            )

    def test_schema_upgrade_success_and_failure_preserve_lineage(self) -> None:
        report = _upgrade_report()
        with self.assertRaisesRegex(ValueError, "greater than"):
            replace(report, to_version=1)
        with self.assertRaisesRegex(ValueError, "absolute normalized"):
            replace(report, backup_path=Path("relative.backup"))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            replace(report, success_digest="bad")
        with self.assertRaisesRegex(ValueError, "must differ"):
            replace(report, success_digest=_DIGEST_A)
        with self.assertRaisesRegex(TypeError, "must be an integer"):
            replace(report, activated_generation=False)

        unverified_store = AssetPreservationEvidence(
            asset_kind=AssetKind.ACTIVE_STORE,
            state=AssetPreservationState.UNVERIFIED,
            before_digest=_DIGEST_A,
            observed_digest=None,
        )
        locator = RecoveryLocator(
            path=Path("/catalog/tm.sqlite3.backup"),
            asset_kind=AssetKind.ACTIVE_STORE,
            expected_digest=_DIGEST_A,
        )
        failure = replace(
            _upgrade_failure(),
            retryable=False,
            active_store_preservation=unverified_store,
            recovery_locators=(locator,),
        )
        self.assertFalse(failure.retryable)
        with self.assertRaisesRegex(ValueError, "fail-stop"):
            replace(failure, retryable=True)


if __name__ == "__main__":
    unittest.main()
