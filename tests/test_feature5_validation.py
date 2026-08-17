from __future__ import annotations
# pyright: reportAny=false, reportExplicitAny=false, reportUnusedCallResult=false

from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

from matcher_capability import MatcherCapabilityEvaluator
from matcher_validation import (
    build_validated_matcher_v1,
    recompute_matcher_validation,
)
from tm_contracts import TextMatcherState
from tm_gate_a import (
    GateAComponent,
    GateAComponentEvidence,
    contracts_public_surface_transcript,
    recompute_gate_a,
)
from tools.validate_tm_release_evidence import main as evidence_cli_main
import tm_contracts


_REPOSITORY_ROOT = Path(__file__).parents[1]
_APPROVED_ROOTS = (
    Path(__file__).parent / "fixtures" / "feature5_gate_a_v1.json"
)
_FIXED_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_FIXED_VALID_UNTIL = datetime(2030, 1, 31, tzinfo=timezone.utc)
_FIXED_EVALUATED_AT = datetime(2030, 1, 15, tzinfo=timezone.utc)


def _load_approved_roots() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(_APPROVED_ROOTS.read_text(encoding="utf-8")),
    )


class Feature5GateATests(unittest.TestCase):
    def test_versioned_gate_a_recomputes_three_independent_grants(
        self,
    ) -> None:
        report = recompute_gate_a(
            repository_root=_REPOSITORY_ROOT,
            approved_roots_path=_APPROVED_ROOTS,
        )

        self.assertEqual(report.schema_version, "feature5-gate-a-v1")
        self.assertEqual(
            report.granted_components,
            (
                GateAComponent.CONTRACTS,
                GateAComponent.SIMILARITY,
                GateAComponent.TEXT,
            ),
        )
        self.assertEqual(
            tuple(item.component for item in report.components),
            tuple(GateAComponent),
        )
        for item in report.components:
            self.assertTrue(item.granted)
            self.assertEqual(
                item.observed_transcript_digest,
                item.approved_transcript_digest,
            )
            self.assertIsNone(item.safe_failure_code)

    def test_contract_source_tamper_revokes_only_contract_grant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            path = root / "tm_contracts.py"
            path.write_bytes(path.read_bytes() + b"\n# tampered\n")

            report = recompute_gate_a(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
            )

        grants = {
            item.component: item.granted for item in report.components
        }
        self.assertEqual(
            grants,
            {
                GateAComponent.CONTRACTS: False,
                GateAComponent.SIMILARITY: True,
                GateAComponent.TEXT: True,
            },
        )

    def test_similarity_fixture_tamper_revokes_only_similarity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            path = root / "tests/fixtures/tm_similarity_vectors.json"
            path.write_bytes(path.read_bytes() + b"\n")

            report = recompute_gate_a(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
            )

        grants = {
            item.component: item.granted for item in report.components
        }
        self.assertEqual(
            grants,
            {
                GateAComponent.CONTRACTS: True,
                GateAComponent.SIMILARITY: False,
                GateAComponent.TEXT: True,
            },
        )

    def test_missing_artifact_is_isolated_to_its_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            (root / "tm_similarity.py").unlink()

            report = recompute_gate_a(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
            )

        self.assertTrue(
            report.evidence_for(GateAComponent.CONTRACTS).granted
        )
        self.assertFalse(
            report.evidence_for(GateAComponent.SIMILARITY).granted
        )
        self.assertTrue(report.evidence_for(GateAComponent.TEXT).granted)

    def test_malformed_fixture_is_isolated_to_its_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            path = root / "tests/fixtures/text_matcher_v1_vectors.json"
            path.write_text("{", encoding="utf-8")

            report = recompute_gate_a(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
            )

        self.assertTrue(
            report.evidence_for(GateAComponent.CONTRACTS).granted
        )
        self.assertTrue(
            report.evidence_for(GateAComponent.SIMILARITY).granted
        )
        self.assertFalse(report.evidence_for(GateAComponent.TEXT).granted)

    def test_malformed_contract_surface_is_isolated_to_contracts(self) -> None:
        with patch.object(
            tm_contracts,
            "__all__",
            [*tm_contracts.__all__, "MissingPublicContract"],
        ):
            report = recompute_gate_a(
                repository_root=_REPOSITORY_ROOT,
                approved_roots_path=_APPROVED_ROOTS,
            )

        self.assertFalse(
            report.evidence_for(GateAComponent.CONTRACTS).granted
        )
        self.assertTrue(
            report.evidence_for(GateAComponent.SIMILARITY).granted
        )
        self.assertTrue(report.evidence_for(GateAComponent.TEXT).granted)

    def test_contract_surface_and_codec_union_are_closed(self) -> None:
        transcript = contracts_public_surface_transcript()
        public_items = cast(
            list[dict[str, object]],
            transcript["public_items"],
        )
        self.assertEqual(
            {cast(str, item["name"]) for item in public_items},
            set(tm_contracts.__all__),
        )
        self.assertEqual(
            tuple(
                cast(list[str], transcript["tm_contract_union_members"])
            ),
            (
                "TMRecord",
                "TMRecordDraft",
                "TMQuery",
                "SimilarityEvidence",
                "ContextEvidence",
                "TMResult",
                "CandidateStageMetadata",
                "CandidateProofMetadata",
                "CandidateProofMetadataV2",
                "CandidateRecallMetadata",
                "CandidateEvidence",
                "CandidateRetrievalReport",
                "ResourceQueryMetadata",
                "ResourceQueryFailure",
                "QueryReport",
                "BenchmarkContract",
                "BenchmarkReport",
                "BenchmarkSuiteContract",
                "BenchmarkSuiteReport",
                "AssetPreservationEvidence",
                "RecoveryLocator",
                "MigrationDiagnostic",
                "ExportDiagnostic",
                "MigrationPreflight",
                "MigrationReport",
                "MigrationFailure",
                "ExportReport",
                "ExportFailure",
                "SchemaUpgradeReport",
                "SchemaUpgradeFailure",
                "SearchOptions",
                "SearchHit",
                "MatcherValidationSummary",
                "TextMatcherCapability",
                "TextMatchRequest",
                "TextMatchSuccess",
                "TextMatchRejected",
                "CanonicalResourceIdentity",
                "SnapshotReceipt",
                "SnapshotManifest",
                "SnapshotBinding",
                "StageValidationEvidence",
            ),
        )
        store_health = cast(
            dict[str, object],
            transcript["store_health_probe"],
        )
        self.assertEqual(store_health["valid_exact_available"], True)
        self.assertEqual(
            store_health["invalid_gate_error_type"],
            "ValueError",
        )

    def test_gate_contract_uses_approved_roots_not_self_reported_passes(
        self,
    ) -> None:
        roots = _load_approved_roots()
        self.assertEqual(roots["schema_version"], "feature5-gate-a-v1")
        self.assertNotIn("passed", json.dumps(roots, sort_keys=True))
        self.assertEqual(
            set(cast(dict[str, object], roots["components"])),
            {item.value for item in GateAComponent},
        )
        with self.assertRaisesRegex(TypeError, "Core factory"):
            GateAComponentEvidence(
                component=GateAComponent.CONTRACTS,
                granted=True,
                approved_artifact_digest="0" * 64,
                observed_artifact_digest="0" * 64,
                approved_fixture_digest="0" * 64,
                observed_fixture_digest="0" * 64,
                approved_transcript_digest="0" * 64,
                observed_transcript_digest="0" * 64,
                safe_failure_code=None,
                _factory_key=object(),
            )


class MatcherReleaseEvidenceTests(unittest.TestCase):
    def test_full_recomputation_is_decided_by_existing_evaluator(self) -> None:
        release = recompute_matcher_validation(
            repository_root=_REPOSITORY_ROOT,
            approved_roots_path=_APPROVED_ROOTS,
            generated_at_utc=_FIXED_GENERATED_AT,
            valid_until_utc=_FIXED_VALID_UNTIL,
            include_full=True,
        )
        capability = MatcherCapabilityEvaluator(
            release.expectation
        ).evaluate(
            release.manifest,
            evaluated_at_utc=_FIXED_EVALUATED_AT,
        )

        self.assertEqual(
            capability.state,
            TextMatcherState.TEXT_V1_VALIDATED,
        )
        manifest = release.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertTrue(
            all(item.passed for item in manifest.cohort_evidence)
        )

    def test_basic_only_recomputation_does_not_claim_full_validation(
        self,
    ) -> None:
        release = recompute_matcher_validation(
            repository_root=_REPOSITORY_ROOT,
            approved_roots_path=_APPROVED_ROOTS,
            generated_at_utc=_FIXED_GENERATED_AT,
            valid_until_utc=_FIXED_VALID_UNTIL,
            include_full=False,
        )
        capability = MatcherCapabilityEvaluator(
            release.expectation
        ).evaluate(
            release.manifest,
            evaluated_at_utc=_FIXED_EVALUATED_AT,
        )

        self.assertEqual(
            capability.state,
            TextMatcherState.BASIC_VALIDATED,
        )

    def test_source_tamper_fails_closed_instead_of_self_signing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            path = root / "text_matcher.py"
            path.write_bytes(path.read_bytes() + b"\n# tampered\n")
            release = recompute_matcher_validation(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
                include_full=True,
            )
        capability = MatcherCapabilityEvaluator(
            release.expectation
        ).evaluate(
            release.manifest,
            evaluated_at_utc=_FIXED_EVALUATED_AT,
        )

        self.assertEqual(capability.state, TextMatcherState.UNAVAILABLE)

    def test_missing_matcher_input_publishes_unavailable_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            (
                root / "tests/fixtures/text_matcher_v1_vectors.json"
            ).unlink()
            release = recompute_matcher_validation(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
                include_full=True,
            )
            matcher = build_validated_matcher_v1(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
                evaluated_at_utc=_FIXED_EVALUATED_AT,
                include_full=True,
            )
        capability = MatcherCapabilityEvaluator(
            release.expectation
        ).evaluate(
            release.manifest,
            evaluated_at_utc=_FIXED_EVALUATED_AT,
        )
        self.assertEqual(capability.state, TextMatcherState.UNAVAILABLE)
        self.assertEqual(
            matcher.capability().state,
            TextMatcherState.UNAVAILABLE,
        )

    def test_malformed_full_fixture_downgrades_to_valid_basic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            (
                root / "tests/fixtures/text_matcher_unicode_vectors.json"
            ).write_text("{", encoding="utf-8")
            release = recompute_matcher_validation(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
                include_full=True,
            )
        capability = MatcherCapabilityEvaluator(
            release.expectation
        ).evaluate(
            release.manifest,
            evaluated_at_utc=_FIXED_EVALUATED_AT,
        )
        self.assertEqual(capability.state, TextMatcherState.BASIC_VALIDATED)

    def test_basic_only_does_not_observe_full_only_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            (
                root / "tests/fixtures/text_matcher_unicode_vectors.json"
            ).unlink()
            (
                root / "tests/fixtures/unicode-16.0.0-WordBreakTest.txt"
            ).unlink()
            release = recompute_matcher_validation(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
                include_full=False,
            )
        capability = MatcherCapabilityEvaluator(
            release.expectation
        ).evaluate(
            release.manifest,
            evaluated_at_utc=_FIXED_EVALUATED_AT,
        )
        self.assertEqual(capability.state, TextMatcherState.BASIC_VALIDATED)

    def test_missing_full_fixture_downgrades_to_valid_basic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            (
                root / "tests/fixtures/text_matcher_unicode_vectors.json"
            ).unlink()
            release = recompute_matcher_validation(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
                include_full=True,
            )
        capability = MatcherCapabilityEvaluator(
            release.expectation
        ).evaluate(
            release.manifest,
            evaluated_at_utc=_FIXED_EVALUATED_AT,
        )
        self.assertEqual(capability.state, TextMatcherState.BASIC_VALIDATED)

    def test_full_fixture_byte_tamper_downgrades_to_valid_basic(self) -> None:
        for relative in (
            "tests/fixtures/text_matcher_unicode_vectors.json",
            "tests/fixtures/unicode-16.0.0-WordBreakTest.txt",
        ):
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _copy_validation_inputs(root)
                    path = root / relative
                    path.write_bytes(path.read_bytes() + b"\n")
                    release = recompute_matcher_validation(
                        repository_root=root,
                        approved_roots_path=_APPROVED_ROOTS,
                        generated_at_utc=_FIXED_GENERATED_AT,
                        valid_until_utc=_FIXED_VALID_UNTIL,
                        include_full=True,
                    )
                capability = MatcherCapabilityEvaluator(
                    release.expectation
                ).evaluate(
                    release.manifest,
                    evaluated_at_utc=_FIXED_EVALUATED_AT,
                )
                self.assertEqual(
                    capability.state,
                    TextMatcherState.BASIC_VALIDATED,
                )

    def test_full_failure_downgrades_without_revoking_valid_basic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            altered_roots = Path(temporary) / "approved.json"
            roots = _load_approved_roots()
            matcher = cast(dict[str, object], roots["matcher"])
            full = cast(dict[str, str], matcher["full_cohorts"])
            full["matcher-text-v1"] = "f" * 64
            altered_roots.write_text(
                json.dumps(roots, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            release = recompute_matcher_validation(
                repository_root=_REPOSITORY_ROOT,
                approved_roots_path=altered_roots,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
                include_full=True,
            )
        capability = MatcherCapabilityEvaluator(
            release.expectation
        ).evaluate(
            release.manifest,
            evaluated_at_utc=_FIXED_EVALUATED_AT,
        )

        self.assertEqual(
            capability.state,
            TextMatcherState.BASIC_VALIDATED,
        )

    def test_basic_failure_revokes_all_matcher_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            altered_roots = Path(temporary) / "approved.json"
            roots = _load_approved_roots()
            matcher = cast(dict[str, object], roots["matcher"])
            basic = cast(dict[str, str], matcher["basic_cohorts"])
            basic["matcher-basic-v1"] = "f" * 64
            altered_roots.write_text(
                json.dumps(roots, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            release = recompute_matcher_validation(
                repository_root=_REPOSITORY_ROOT,
                approved_roots_path=altered_roots,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
                include_full=True,
            )
        capability = MatcherCapabilityEvaluator(
            release.expectation
        ).evaluate(
            release.manifest,
            evaluated_at_utc=_FIXED_EVALUATED_AT,
        )

        self.assertEqual(capability.state, TextMatcherState.UNAVAILABLE)

    def test_release_window_requires_explicit_utc_and_caps_ttl(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware UTC"):
            recompute_matcher_validation(
                repository_root=_REPOSITORY_ROOT,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=datetime(2030, 1, 1),
                valid_until_utc=_FIXED_VALID_UNTIL,
                include_full=True,
            )
        with self.assertRaisesRegex(ValueError, "30 days"):
            recompute_matcher_validation(
                repository_root=_REPOSITORY_ROOT,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=(
                    _FIXED_GENERATED_AT + timedelta(days=30, seconds=1)
                ),
                include_full=True,
            )

    def test_factory_recomputes_evidence_before_constructing_matcher(
        self,
    ) -> None:
        matcher = build_validated_matcher_v1(
            repository_root=_REPOSITORY_ROOT,
            approved_roots_path=_APPROVED_ROOTS,
            generated_at_utc=_FIXED_GENERATED_AT,
            valid_until_utc=_FIXED_VALID_UNTIL,
            evaluated_at_utc=_FIXED_EVALUATED_AT,
            include_full=True,
        )
        self.assertEqual(
            matcher.capability().state,
            TextMatcherState.TEXT_V1_VALIDATED,
        )

    def test_cli_exit_code_matches_requested_release_level(self) -> None:
        common = [
            "--repository-root",
            str(_REPOSITORY_ROOT),
            "--approved-roots",
            str(_APPROVED_ROOTS),
            "--generated-at",
            "2030-01-01T00:00:00Z",
            "--valid-until",
            "2030-01-31T00:00:00Z",
            "--evaluated-at",
            "2030-01-15T00:00:00Z",
        ]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(evidence_cli_main(common), 0)
            self.assertEqual(
                evidence_cli_main(common + ["--basic-only"]),
                0,
            )

    def test_cli_full_mode_rejects_basic_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            altered_roots = Path(temporary) / "approved.json"
            roots = _load_approved_roots()
            matcher = cast(dict[str, object], roots["matcher"])
            full = cast(dict[str, str], matcher["full_cohorts"])
            full["matcher-text-v1"] = "f" * 64
            altered_roots.write_text(
                json.dumps(roots, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            arguments = [
                "--repository-root",
                str(_REPOSITORY_ROOT),
                "--approved-roots",
                str(altered_roots),
                "--generated-at",
                "2030-01-01T00:00:00Z",
                "--valid-until",
                "2030-01-31T00:00:00Z",
                "--evaluated-at",
                "2030-01-15T00:00:00Z",
            ]
            with redirect_stdout(io.StringIO()):
                self.assertNotEqual(evidence_cli_main(arguments), 0)


def _copy_validation_inputs(destination: Path) -> None:
    roots = _load_approved_roots()
    relative_paths: set[str] = set()
    for component in cast(
        dict[str, dict[str, object]],
        roots["components"],
    ).values():
        relative_paths.update(cast(list[str], component["artifact_paths"]))
        relative_paths.update(cast(list[str], component["fixture_paths"]))
    matcher = cast(dict[str, object], roots["matcher"])
    relative_paths.update(cast(list[str], matcher["artifact_paths"]))
    relative_paths.update(cast(list[str], matcher["build_paths"]))
    relative_paths.update(cast(list[str], matcher["fixture_paths"]))
    relative_paths.add(cast(str, matcher["evaluator_path"]))
    for relative in sorted(relative_paths):
        source = _REPOSITORY_ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


if __name__ == "__main__":
    unittest.main()
