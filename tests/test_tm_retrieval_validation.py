"""Task 7.5 Gate C retrieval validation leaf focused tests.

The suite proves ``tm_retrieval_validation.py`` recomputes the frozen
context-v1 vectors from approved closed-field roots, reruns the frozen fuzzy
scoring vectors against public ``score_fuzzy_candidates`` into a
deterministic body-safe transcript, executes one fixed real temporary-store
journey against public ``SQLiteTMStore``, ``CandidateRetriever`` and
query-view ports with duplicate-batch rollback into a deterministic
body-safe transcript, mints a short-lived manifest whose ``passed`` flags
derive only from observed digest equality, locks the final fuzzy-core
cohort digest over the fixture plus the scoring, store and service
transcripts while keeping both Gate D benchmark rows empty, proves the
fixed single-snapshot refresh race (in-flight query keeps the old snapshot,
the publisher moves to a closed snapshot, and the next query observes it),
fails closed on tamper, raises on malformed roots, and never persists
output or leaks source/target/speaker/previous/next bodies, provenance,
paths or exception text.
"""

from __future__ import annotations
# pyright: reportAny=false, reportExplicitAny=false, reportUnusedCallResult=false

import copy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, cast
import unittest

from tm_retrieval_capability import (
    RETRIEVAL_CONTEXT_EVIDENCE_FAILED_CODE,
    RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE,
    RETRIEVAL_CONTEXT_IDENTITY_INVALID_CODE,
    RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE,
    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE,
    RetrievalCapabilityEvaluator,
    RetrievalCapabilitySnapshot,
)
from tm_contracts import (
    CandidateEvidence,
    CandidateRecallMetadata,
    CandidateRetrievalReport,
    CandidateStage,
    CandidateStageMetadata,
    StoreHealth,
    TMRecord,
)
from tm_gate_a import aggregate_paths_digest, canonical_digest
from tm_retrieval_validation import (
    RETRIEVAL_GATE_C_ROOTS_SCHEMA_VERSION,
    RETRIEVAL_CONTEXT_COHORT_ID,
    RETRIEVAL_FUZZY_CORE_COHORT_ID,
    RetrievalValidationRelease,
    _IMPLEMENTATION_PENDING,
    _StoreConfig,
    _StoreDraftConfig,
    _observe_context_transcript,
    _observe_fuzzy_scoring_transcript,
    _observe_service_transcript,
    _observe_store_transcript,
    _store_rollback_facts,
    recompute_retrieval_validation,
)
from tm_sqlite_store import CanonicalRevisionSnapshot


_REPOSITORY_ROOT = Path(__file__).parents[1]
_APPROVED_ROOTS = (
    Path(__file__).parent
    / "fixtures"
    / "retrieval_gate_c_roots_v1.json"
)
_VECTORS_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "retrieval_gate_c_vectors_v1.json"
)
_FIXED_GENERATED_AT = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
_FIXED_VALID_UNTIL = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
_FIXED_EVALUATED_AT = datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STRICT_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_VECTOR_BODIES = (
    "Open the door.",
    "Alice",
    "Wait.",
    "Then.",
    "alice",
    " Alice",
    "Wait. ",
    "Really?",
    "target-100",
    "target-9",
    "target-7",
    "target-5",
    "target-2",
    "Nope.",
    "Close the window.",
    "Close the window",
    "Close the window now",
    "OPEN THE DOOR.",
    "Please close the window.",
    "Please close the window",
    "Please close the window now",
    "xyz",
    "abc",
    "Turn off the light.",
    "Turn off the light",
    "Turn on the light",
    "turn off the light",
    "target-3",
    "target-4",
    "target-6",
    "target-8",
    "target-11",
    "target-12",
    "target-13",
    "target-exact",
    "target-near-1",
    "target-near-2",
    "target-unrelated",
    "target-duplicate",
    "Open the door",
    "Open the window.",
    "xyzzy",
    "A different body.",
    "target-service-primary-context",
    "target-service-primary-exact",
    "target-service-secondary-context",
    "target-service-secondary-exact",
)


def _release() -> RetrievalValidationRelease:
    return recompute_retrieval_validation(
        repository_root=_REPOSITORY_ROOT,
        approved_roots_path=_APPROVED_ROOTS,
        generated_at_utc=_FIXED_GENERATED_AT,
        valid_until_utc=_FIXED_VALID_UNTIL,
    )


def _snapshot(
    release: RetrievalValidationRelease,
) -> RetrievalCapabilitySnapshot:
    manifest = release.manifest
    if manifest is None:
        raise AssertionError("expected a manifest")
    return RetrievalCapabilityEvaluator(release.expectation).evaluate(
        manifest,
        evaluated_at_utc=_FIXED_EVALUATED_AT,
    )


def _load_json(path: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(path.read_text(encoding="utf-8")),
    )


def _copy_validation_inputs(
    destination: Path,
    *,
    include_fixture: bool = True,
) -> None:
    roots = _load_json(_APPROVED_ROOTS)
    relative_paths: set[str] = set()
    relative_paths.update(cast(list[str], roots["artifact_paths"]))
    relative_paths.update(cast(list[str], roots["build_paths"]))
    relative_paths.update(cast(list[str], roots["fixture_paths"]))
    relative_paths.add(cast(str, roots["evaluator_path"]))
    if not include_fixture:
        relative_paths.difference_update(
            cast(list[str], roots["fixture_paths"])
        )
    for relative in sorted(relative_paths):
        source = _REPOSITORY_ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _iter_strings(value: object):
    if isinstance(value, str):
        yield value
    elif is_dataclass(value):
        for field_value in asdict(cast(Any, value)).values():
            yield from _iter_strings(field_value)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _snapshot_file_set(root: Path) -> frozenset[str]:
    result: set[str] = set()
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            result.add(str(path.relative_to(root)))
    return frozenset(result)


def _fuzzy_by_id() -> dict[str, dict[str, Any]]:
    return {
        cast("str", entry["id"]): cast("dict[str, Any]", entry)
        for entry in _observe_fuzzy_scoring_transcript(_VECTORS_FIXTURE)
    }


def _store_entry() -> dict[str, Any]:
    entries = _observe_store_transcript(_VECTORS_FIXTURE)
    if len(entries) != 1:
        raise AssertionError("expected exactly one store transcript entry")
    return cast("dict[str, Any]", entries[0])


def _service_digests() -> tuple[str, str, str]:
    fixture_digest = aggregate_paths_digest(
        _REPOSITORY_ROOT,
        (str(_VECTORS_FIXTURE.relative_to(_REPOSITORY_ROOT)),),
    )
    context_transcript = _observe_context_transcript(_VECTORS_FIXTURE)
    context_digest = canonical_digest(
        {
            "fixture_digest": fixture_digest,
            "transcript": context_transcript,
        }
    )
    harness_fuzzy_digest = canonical_digest(
        {"implementation": _IMPLEMENTATION_PENDING}
    )
    return fixture_digest, context_digest, harness_fuzzy_digest


def _service_entries() -> list[dict[str, Any]]:
    _fixture_digest, context_digest, harness_fuzzy_digest = _service_digests()
    entries = _observe_service_transcript(
        _VECTORS_FIXTURE,
        expectation=_release().expectation,
        observed_context_digest=context_digest,
        harness_fuzzy_core_digest=harness_fuzzy_digest,
        generated_at_utc=_FIXED_GENERATED_AT,
        valid_until_utc=_FIXED_VALID_UNTIL,
    )
    if len(entries) != 3:
        raise AssertionError("expected exactly three service transcript entries")
    return entries


def _service_entry(scenario_id: str) -> dict[str, Any]:
    for entry in _service_entries():
        if cast("str", entry["id"]) == scenario_id:
            return cast("dict[str, Any]", entry)
    raise AssertionError(f"missing service transcript entry {scenario_id}")


def _store_runtime_branch(payload: dict[str, Any]) -> dict[str, Any]:
    index_kind = cast("str", _store_entry()["runtime"]["index_kind"])
    return cast(
        "dict[str, Any]",
        payload["store"]["expected"]["by_runtime"][index_kind],
    )


def _store_rollback_fields() -> tuple[str, ...]:
    return (
        "revision_unchanged",
        "exported_unchanged",
        "health_unchanged",
        "candidates_unchanged",
        "absent",
    )


def _rollback_record(record_id: int) -> TMRecord:
    return TMRecord(
        record_id=record_id,
        source_raw="Open the door.",
        target_raw=f"target-{record_id}",
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        file_source=None,
        provenance=(("origin", "import.gate-c.store"),),
        legacy_line_no=None,
        origin_batch_id="import.gate-c.store",
        origin_ordinal=record_id - 1,
    )


class RetrievalValidationReleaseTests(unittest.TestCase):
    def test_recompute_is_deterministic_and_repeatable(self) -> None:
        first = _release()
        second = _release()
        self.assertIsInstance(first, RetrievalValidationRelease)
        self.assertEqual(first.expectation, second.expectation)
        self.assertEqual(first.manifest, second.manifest)
        self.assertIsNotNone(first.manifest)
        manifest = first.manifest
        assert manifest is not None
        self.assertEqual(manifest.generated_at_utc, "2026-08-12T00:00:00Z")
        self.assertEqual(manifest.valid_until_utc, "2026-08-12T12:00:00Z")
        self.assertEqual(
            manifest.context_cohorts[0].cohort_digest,
            first.expectation.context_cohorts[0].cohort_digest,
        )

    def test_recompute_signature_has_no_caller_passed_input(self) -> None:
        signature = inspect.signature(recompute_retrieval_validation)
        self.assertEqual(
            tuple(signature.parameters),
            (
                "repository_root",
                "approved_roots_path",
                "generated_at_utc",
                "valid_until_utc",
            ),
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in signature.parameters.values()
            )
        )

    def test_context_and_fuzzy_core_open_while_gate_d_paths_close(
        self,
    ) -> None:
        release = _release()
        manifest = release.manifest
        self.assertIsNotNone(manifest)
        assert manifest is not None
        self.assertEqual(len(manifest.context_cohorts), 1)
        self.assertEqual(
            manifest.context_cohorts[0].cohort_id,
            RETRIEVAL_CONTEXT_COHORT_ID,
        )
        self.assertIs(manifest.context_cohorts[0].passed, True)
        self.assertEqual(len(manifest.fuzzy_core_cohorts), 1)
        self.assertEqual(
            manifest.fuzzy_core_cohorts[0].cohort_id,
            RETRIEVAL_FUZZY_CORE_COHORT_ID,
        )
        self.assertIs(manifest.fuzzy_core_cohorts[0].passed, True)
        self.assertIsNone(manifest.fts5_trigram_benchmark)
        self.assertIsNone(manifest.gram_fallback_benchmark)

        snapshot = _snapshot(release)
        self.assertTrue(snapshot.context.available)
        self.assertIsNone(snapshot.context.unavailable_code)
        self.assertTrue(snapshot.fuzzy_core.available)
        self.assertIsNone(snapshot.fuzzy_core.unavailable_code)
        self.assertFalse(snapshot.fts5_trigram.available)
        self.assertEqual(
            snapshot.fts5_trigram.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
        )
        self.assertFalse(snapshot.gram_fallback.available)
        self.assertEqual(
            snapshot.gram_fallback.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
        )
        self.assertEqual(
            snapshot.summary.unavailable_codes,
            (RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,),
        )
        self.assertEqual(
            snapshot.fuzzy_available_for("FTS5_TRIGRAM"),
            (False, RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE),
        )
        self.assertEqual(
            snapshot.fuzzy_available_for("GRAM_FALLBACK"),
            (False, RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE),
        )

    def test_fuzzy_core_opens_only_as_gate_c_prerequisite(self) -> None:
        release = _release()
        manifest = release.manifest
        assert manifest is not None
        self.assertEqual(len(manifest.fuzzy_core_cohorts), 1)
        row = manifest.fuzzy_core_cohorts[0]
        self.assertEqual(row.cohort_id, RETRIEVAL_FUZZY_CORE_COHORT_ID)
        self.assertIs(row.passed, True)
        self.assertIsNotNone(_DIGEST.fullmatch(row.cohort_digest))
        approved = release.expectation.fuzzy_core_cohorts[0].cohort_digest
        self.assertEqual(row.cohort_digest, approved)
        snapshot = _snapshot(release)
        self.assertTrue(snapshot.fuzzy_core.available)
        self.assertIsNone(snapshot.fuzzy_core.unavailable_code)
        self.assertFalse(snapshot.fts5_trigram.available)
        self.assertFalse(snapshot.gram_fallback.available)
        self.assertFalse(snapshot.fuzzy_available_for("FTS5_TRIGRAM")[0])
        self.assertFalse(snapshot.fuzzy_available_for("GRAM_FALLBACK")[0])

    def test_gate_d_benchmark_evidence_rows_are_missing(self) -> None:
        release = _release()
        manifest = release.manifest
        assert manifest is not None
        self.assertIsNone(manifest.fts5_trigram_benchmark)
        self.assertIsNone(manifest.gram_fallback_benchmark)
        snapshot = _snapshot(release)
        self.assertFalse(snapshot.fts5_trigram.available)
        self.assertFalse(snapshot.gram_fallback.available)
        self.assertEqual(
            snapshot.fts5_trigram.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
        )
        self.assertEqual(
            snapshot.gram_fallback.unavailable_code,
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
        )

    def test_release_window_requires_explicit_utc_and_caps_ttl(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware UTC"):
            recompute_retrieval_validation(
                repository_root=_REPOSITORY_ROOT,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=datetime(2026, 8, 12),
                valid_until_utc=_FIXED_VALID_UNTIL,
            )
        with self.assertRaisesRegex(ValueError, "whole-second"):
            recompute_retrieval_validation(
                repository_root=_REPOSITORY_ROOT,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=datetime(
                    2026, 8, 12, 0, 0, 0, 500, tzinfo=timezone.utc
                ),
                valid_until_utc=_FIXED_VALID_UNTIL,
            )
        with self.assertRaisesRegex(ValueError, "timezone-aware UTC"):
            recompute_retrieval_validation(
                repository_root=_REPOSITORY_ROOT,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=datetime(
                    2026, 8, 12, 0, 0, tzinfo=timezone(timedelta(hours=8))
                ),
                valid_until_utc=_FIXED_VALID_UNTIL,
            )
        with self.assertRaisesRegex(ValueError, "later than generated"):
            recompute_retrieval_validation(
                repository_root=_REPOSITORY_ROOT,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_VALID_UNTIL,
                valid_until_utc=_FIXED_VALID_UNTIL,
            )
        with self.assertRaisesRegex(ValueError, "30 days"):
            recompute_retrieval_validation(
                repository_root=_REPOSITORY_ROOT,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=(
                    _FIXED_GENERATED_AT + timedelta(days=30, seconds=1)
                ),
            )
        release = recompute_retrieval_validation(
            repository_root=_REPOSITORY_ROOT,
            approved_roots_path=_APPROVED_ROOTS,
            generated_at_utc=_FIXED_GENERATED_AT,
            valid_until_utc=(_FIXED_GENERATED_AT + timedelta(days=30)),
        )
        self.assertIsNotNone(release.manifest)

    def test_malformed_approved_roots_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            missing = directory / "missing.json"
            with self.assertRaisesRegex(ValueError, "existing file"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=missing,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            invalid = directory / "invalid.json"
            invalid.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON is invalid"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=invalid,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            non_object = directory / "non_object.json"
            non_object.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "root must be an object"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=non_object,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            non_finite = directory / "non_finite.json"
            non_finite.write_text(
                '{"schema_version": NaN}', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "non-finite"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=non_finite,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            duplicate = directory / "duplicate.json"
            duplicate.write_text(
                '{"a": 1, "a": 2}', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=duplicate,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

    def test_roots_schema_fields_and_values_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            roots = _load_json(_APPROVED_ROOTS)

            wrong_schema = directory / "wrong_schema.json"
            altered = dict(roots)
            altered["schema_version"] = "retrieval-gate-c-roots-v2"
            wrong_schema.write_text(
                json.dumps(altered, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsupported"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=wrong_schema,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            extra_field = directory / "extra_field.json"
            altered = dict(roots)
            altered["extra"] = True
            extra_field.write_text(
                json.dumps(altered, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "not closed"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=extra_field,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            missing_field = directory / "missing_field.json"
            altered = dict(roots)
            del altered["evaluator_digest"]
            missing_field.write_text(
                json.dumps(altered, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "not closed"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=missing_field,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            bad_digest = directory / "bad_digest.json"
            altered = dict(roots)
            altered["artifact_digest"] = "not-a-digest"
            bad_digest.write_text(
                json.dumps(altered, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "SHA-256 digest"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=bad_digest,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            bad_cohort = directory / "bad_cohort.json"
            altered = dict(roots)
            altered["context_cohorts"] = {
                "unknown.cohort.v1": "f" * 64
            }
            bad_cohort.write_text(
                json.dumps(altered, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "exactly"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=bad_cohort,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            bad_path = directory / "bad_path.json"
            altered = dict(roots)
            altered["fixture_paths"] = [
                "tests/fixtures/retrieval_gate_c_roots_v1.json",
                "tests/fixtures/retrieval_gate_c_vectors_v1.json",
            ]
            bad_path.write_text(
                json.dumps(altered, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "not closed"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=bad_path,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            bad_fts5 = directory / "bad_fts5.json"
            altered = dict(roots)
            altered["fts5_trigram"] = {
                "path": "GRAM_FALLBACK",
                "contract_digest": "f" * 64,
            }
            bad_fts5.write_text(
                json.dumps(altered, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "path is unsupported"):
                recompute_retrieval_validation(
                    repository_root=_REPOSITORY_ROOT,
                    approved_roots_path=bad_fts5,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

    def test_observation_failure_returns_expectation_with_manifest_none(
        self,
    ) -> None:
        approved_expectation = _release().expectation
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root, include_fixture=False)
            release = recompute_retrieval_validation(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
            )
            self.assertEqual(release.expectation, approved_expectation)
            self.assertIsNone(release.manifest)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            fixture = root / "tests/fixtures/retrieval_gate_c_vectors_v1.json"
            fixture.write_text("{broken", encoding="utf-8")
            release = recompute_retrieval_validation(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
            )
            self.assertEqual(release.expectation, approved_expectation)
            self.assertIsNone(release.manifest)

    def test_roots_context_digest_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            altered_roots = Path(temporary) / "roots.json"
            roots = _load_json(_APPROVED_ROOTS)
            roots["context_cohorts"][RETRIEVAL_CONTEXT_COHORT_ID] = (
                hashlib.sha256(b"tampered-context").hexdigest()
            )
            altered_roots.write_text(
                json.dumps(roots, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            release = recompute_retrieval_validation(
                repository_root=_REPOSITORY_ROOT,
                approved_roots_path=altered_roots,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
            )
            # A tampered approved CONTEXT digest closes the harness service
            # cohort, so no evidence can be minted and the release fails
            # closed with no manifest at all.
            self.assertIsNone(release.manifest)

    def test_fixture_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            fixture = root / "tests/fixtures/retrieval_gate_c_vectors_v1.json"
            payload = _load_json(fixture)
            for vector in cast(
                "list[dict[str, Any]]", payload["vectors"]
            ):
                if vector["id"] != "positive-context":
                    continue
                for record in cast(
                    "list[dict[str, Any]]", vector["records"]
                ):
                    if record["record_id"] == 7:
                        record["speaker_raw"] = "alice"
                expected_context = cast(
                    "list[dict[str, Any]]",
                    vector["expected"]["context"],
                )
                expected_context[0]["matched_fields"] = [
                    "context_prev_raw"
                ]
                expected_context[0]["mismatched_fields"] = [
                    "speaker_raw"
                ]
                expected_context[0]["strength_v1"] = [1, -1, 0, 1, 0]
            fixture.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            release = recompute_retrieval_validation(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
            )
            # The tampered CONTEXT vector changes the observed CONTEXT
            # digest and closes the harness service cohort, so the release
            # fails closed with no manifest.
            self.assertIsNone(release.manifest)

    def test_fuzzy_scoring_transcript_is_deterministic_and_repeatable(
        self,
    ) -> None:
        first = _observe_fuzzy_scoring_transcript(_VECTORS_FIXTURE)
        second = _observe_fuzzy_scoring_transcript(_VECTORS_FIXTURE)
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertEqual(len(first), 7)

    def test_fuzzy_threshold_equality_and_below_threshold(self) -> None:
        transcript = _fuzzy_by_id()
        entry = cast(
            "dict[str, Any]",
            transcript["fuzzy-threshold-boundary"],
        )
        self.assertEqual(entry["scored_count"], 2)
        self.assertEqual(entry["accepted_count"], 1)
        accepted = cast("list[dict[str, Any]]", entry["accepted"])
        self.assertEqual(
            [accepted_entry["record_id"] for accepted_entry in accepted],
            [7],
        )
        self.assertEqual(
            accepted[0]["similarity"],
            0.9544592030360531,
        )
        self.assertEqual(
            accepted[0]["similarity_evidence"]["final_similarity"],
            0.9544592030360531,
        )

    def test_fuzzy_zero_and_one_threshold_boundaries(self) -> None:
        transcript = _fuzzy_by_id()
        zero = cast(
            "dict[str, Any]",
            transcript["fuzzy-threshold-zero"],
        )
        self.assertEqual(zero["scored_count"], 1)
        self.assertEqual(
            [
                accepted["record_id"]
                for accepted in zero["accepted"]
            ],
            [4],
        )
        self.assertEqual(zero["accepted"][0]["similarity"], 0.0)
        one = cast(
            "dict[str, Any]",
            transcript["fuzzy-threshold-one"],
        )
        self.assertEqual(one["scored_count"], 2)
        self.assertEqual(
            [
                accepted["record_id"]
                for accepted in one["accepted"]
            ],
            [7],
        )
        self.assertEqual(one["accepted"][0]["similarity"], 1.0)

    def test_fuzzy_order_descending_similarity_then_record_id(self) -> None:
        transcript = _fuzzy_by_id()
        entry = cast("dict[str, Any]", transcript["fuzzy-tie-break"])
        accepted = cast("list[dict[str, Any]]", entry["accepted"])
        self.assertEqual(
            [accepted_entry["record_id"] for accepted_entry in accepted],
            [9, 4, 2],
        )
        similarities = [
            accepted_entry["similarity"] for accepted_entry in accepted
        ]
        self.assertEqual(
            similarities,
            sorted(similarities, reverse=True),
        )
        self.assertEqual(accepted[0]["similarity"], accepted[1]["similarity"])
        self.assertGreater(accepted[0]["record_id"], accepted[1]["record_id"])

    def test_fuzzy_same_source_excluded_without_scoring(self) -> None:
        transcript = _fuzzy_by_id()
        entry = cast(
            "dict[str, Any]",
            transcript["fuzzy-same-source-exclusion"],
        )
        self.assertEqual(entry["scored_count"], 1)
        self.assertEqual(
            [
                accepted["record_id"]
                for accepted in entry["accepted"]
            ],
            [6],
        )

    def test_fuzzy_empty_accepted_case(self) -> None:
        transcript = _fuzzy_by_id()
        entry = cast(
            "dict[str, Any]",
            transcript["fuzzy-all-below-threshold"],
        )
        self.assertEqual(entry["scored_count"], 2)
        self.assertEqual(entry["accepted_count"], 0)
        self.assertEqual(entry["accepted"], [])

    def test_fuzzy_gram_fallback_stage_pipeline(self) -> None:
        transcript = _fuzzy_by_id()
        entry = cast(
            "dict[str, Any]",
            transcript["fuzzy-gram-fallback"],
        )
        self.assertEqual(entry["index_kind"], "GRAM_FALLBACK")
        self.assertEqual(
            [stage["stage"] for stage in entry["stages"]],
            ["GRAM_3", "GRAM_2", "UNION", "DEDUPLICATE"],
        )
        self.assertEqual(
            [
                accepted["record_id"]
                for accepted in entry["accepted"]
            ],
            [13, 12, 11],
        )

    def test_fuzzy_stage_counts_are_conserved_and_faithful(self) -> None:
        fixture = _load_json(_VECTORS_FIXTURE)
        expected_vectors = {
            cast("str", vector["id"]): cast("dict[str, Any]", vector)
            for vector in cast(
                "list[dict[str, Any]]",
                fixture["fuzzy"]["vectors"],
            )
        }
        for entry in _observe_fuzzy_scoring_transcript(_VECTORS_FIXTURE):
            vector_id = cast("str", entry["id"])
            expected = expected_vectors[vector_id]["expected"]
            stages = cast("list[dict[str, Any]]", entry["stages"])
            self.assertTrue(stages)
            self.assertEqual(stages[0]["input_count"], 0)
            for stage in stages:
                self.assertEqual(
                    stage["input_count"]
                    + stage["added_unique_count"]
                    - stage["dropped_count"],
                    stage["output_unique_count"],
                )
            self.assertEqual(stages, expected["stages"])
            self.assertEqual(
                entry["scored_count"],
                expected["scored_count"],
            )
            self.assertEqual(
                entry["accepted_count"],
                expected["accepted_count"],
            )
            self.assertEqual(
                [
                    accepted["record_id"]
                    for accepted in cast(
                        "list[dict[str, Any]]",
                        entry["accepted"],
                    )
                ],
                [
                    accepted_entry["record_id"]
                    for accepted_entry in expected["accepted"]
                ],
            )

    def test_fuzzy_fixture_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            fixture = (
                root
                / "tests/fixtures/retrieval_gate_c_vectors_v1.json"
            )
            payload = _load_json(fixture)
            for vector in cast(
                "list[dict[str, Any]]",
                payload["fuzzy"]["vectors"],
            ):
                if vector["id"] != "fuzzy-tie-break":
                    continue
                accepted = vector["expected"]["accepted"]
                accepted[0], accepted[2] = accepted[2], accepted[0]
            fixture.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            release = recompute_retrieval_validation(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
            )
            self.assertEqual(release.expectation, _release().expectation)
            self.assertIsNone(release.manifest)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _copy_validation_inputs(root)
            fixture = (
                root
                / "tests/fixtures/retrieval_gate_c_vectors_v1.json"
            )
            payload = _load_json(fixture)
            for vector in cast(
                "list[dict[str, Any]]",
                payload["fuzzy"]["vectors"],
            ):
                if vector["id"] != "fuzzy-threshold-boundary":
                    continue
                vector["expected"]["stages"][-1][
                    "output_unique_count"
                ] = 4
            fixture.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            release = recompute_retrieval_validation(
                repository_root=root,
                approved_roots_path=_APPROVED_ROOTS,
                generated_at_utc=_FIXED_GENERATED_AT,
                valid_until_utc=_FIXED_VALID_UNTIL,
            )
            self.assertIsNone(release.manifest)

    def test_final_fuzzy_digest_covers_all_four_inputs_and_fails_closed(
        self,
    ) -> None:
        fixture_digest = aggregate_paths_digest(
            _REPOSITORY_ROOT,
            (
                str(
                    _VECTORS_FIXTURE.relative_to(
                        _REPOSITORY_ROOT
                    )
                ),
            ),
        )
        context_transcript = _observe_context_transcript(
            _VECTORS_FIXTURE
        )
        scoring_transcript = _observe_fuzzy_scoring_transcript(
            _VECTORS_FIXTURE
        )
        store_transcript = _observe_store_transcript(_VECTORS_FIXTURE)
        service_transcript = _observe_service_transcript(
            _VECTORS_FIXTURE,
            expectation=_release().expectation,
            observed_context_digest=canonical_digest(
                {
                    "fixture_digest": fixture_digest,
                    "transcript": context_transcript,
                }
            ),
            harness_fuzzy_core_digest=canonical_digest(
                {"implementation": _IMPLEMENTATION_PENDING}
            ),
            generated_at_utc=_FIXED_GENERATED_AT,
            valid_until_utc=_FIXED_VALID_UNTIL,
        )
        final_digest = canonical_digest(
            {
                "fixture_digest": fixture_digest,
                "scoring": scoring_transcript,
                "store": store_transcript,
                "service": service_transcript,
            }
        )
        approved = _release().expectation.fuzzy_core_cohorts[0].cohort_digest
        self.assertEqual(final_digest, approved)
        manifest = _release().manifest
        assert manifest is not None
        self.assertEqual(
            manifest.fuzzy_core_cohorts[0].cohort_digest,
            final_digest,
        )
        # Every one of the four inputs alone must move the digest.
        self.assertNotEqual(
            final_digest,
            canonical_digest(
                {
                    "fixture_digest": hashlib.sha256(
                        b"tampered-fixture"
                    ).hexdigest(),
                    "scoring": scoring_transcript,
                    "store": store_transcript,
                    "service": service_transcript,
                }
            ),
        )
        tampered_scoring: list[dict[str, Any]] = copy.deepcopy(
            scoring_transcript
        )
        tampered_scoring[0]["scored_count"] = (
            cast("int", tampered_scoring[0]["scored_count"]) + 1
        )
        self.assertNotEqual(
            final_digest,
            canonical_digest(
                {
                    "fixture_digest": fixture_digest,
                    "scoring": tampered_scoring,
                    "store": store_transcript,
                    "service": service_transcript,
                }
            ),
        )
        tampered_store: list[dict[str, Any]] = copy.deepcopy(
            store_transcript
        )
        tampered_store[0]["rollback"]["absent"] = not cast(
            "bool", tampered_store[0]["rollback"]["absent"]
        )
        self.assertNotEqual(
            final_digest,
            canonical_digest(
                {
                    "fixture_digest": fixture_digest,
                    "scoring": scoring_transcript,
                    "store": tampered_store,
                    "service": service_transcript,
                }
            ),
        )
        tampered_service: list[dict[str, Any]] = copy.deepcopy(
            service_transcript
        )
        tampered_service[0]["aggregation"]["result_count"] = (
            cast("int", tampered_service[0]["aggregation"]["result_count"])
            + 1
        )
        self.assertNotEqual(
            final_digest,
            canonical_digest(
                {
                    "fixture_digest": fixture_digest,
                    "scoring": scoring_transcript,
                    "store": store_transcript,
                    "service": tampered_service,
                }
            ),
        )

    def test_fuzzy_duplicate_unknown_unsorted_and_closed_fields_fail(
        self,
    ) -> None:
        base_payload = _load_json(_VECTORS_FIXTURE)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def tampered(payload: dict[str, Any]) -> Path:
                path = directory / "tampered.json"
                path.write_text(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                return path

            duplicate_vector = json.loads(json.dumps(base_payload))
            vectors = cast(
                "list[dict[str, Any]]",
                duplicate_vector["fuzzy"]["vectors"],
            )
            vectors[1]["id"] = vectors[0]["id"]
            with self.assertRaisesRegex(ValueError, "must be unique"):
                _observe_fuzzy_scoring_transcript(
                    tampered(duplicate_vector)
                )

            duplicate_accepted = json.loads(json.dumps(base_payload))
            for vector in cast(
                "list[dict[str, Any]]",
                duplicate_accepted["fuzzy"]["vectors"],
            ):
                if vector["id"] != "fuzzy-tie-break":
                    continue
                vector["expected"]["accepted"].append(
                    vector["expected"]["accepted"][0]
                )
                vector["expected"]["accepted_count"] += 1
            with self.assertRaisesRegex(ValueError, "must be unique"):
                _observe_fuzzy_scoring_transcript(
                    tampered(duplicate_accepted)
                )

            unknown_accepted = json.loads(json.dumps(base_payload))
            for vector in cast(
                "list[dict[str, Any]]",
                unknown_accepted["fuzzy"]["vectors"],
            ):
                if vector["id"] != "fuzzy-threshold-boundary":
                    continue
                vector["expected"]["accepted"][0]["record_id"] = 999
            with self.assertRaisesRegex(ValueError, "is unknown"):
                _observe_fuzzy_scoring_transcript(
                    tampered(unknown_accepted)
                )

            unsorted = json.loads(json.dumps(base_payload))
            for vector in cast(
                "list[dict[str, Any]]",
                unsorted["fuzzy"]["vectors"],
            ):
                if vector["id"] != "fuzzy-gram-fallback":
                    continue
                accepted = vector["expected"]["accepted"]
                accepted[0], accepted[2] = accepted[2], accepted[0]
            with self.assertRaisesRegex(ValueError, "diverged"):
                _observe_fuzzy_scoring_transcript(tampered(unsorted))

            closed_fields = json.loads(json.dumps(base_payload))
            cast(
                "dict[str, Any]",
                closed_fields["fuzzy"]["vectors"][0],
            )["extra"] = True
            with self.assertRaisesRegex(ValueError, "not closed"):
                _observe_fuzzy_scoring_transcript(
                    tampered(closed_fields)
                )

    def test_fuzzy_transcript_is_body_safe_and_shape_closed(self) -> None:
        transcript = _observe_fuzzy_scoring_transcript(_VECTORS_FIXTURE)
        self.assertTrue(transcript)
        serialized = json.dumps(
            transcript,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for body in _VECTOR_BODIES:
            self.assertNotIn(body, serialized)
        for entry in transcript:
            self.assertEqual(
                set(entry),
                {
                    "id",
                    "index_kind",
                    "stages",
                    "scored_count",
                    "accepted_count",
                    "accepted",
                },
            )
            self.assertIn(
                entry["index_kind"],
                ("FTS5_TRIGRAM", "GRAM_FALLBACK"),
            )
            stages = cast("list[dict[str, Any]]", entry["stages"])
            self.assertTrue(stages)
            for stage in stages:
                self.assertEqual(
                    set(stage),
                    {
                        "stage",
                        "input_count",
                        "added_unique_count",
                        "output_unique_count",
                        "dropped_count",
                    },
                )
            accepted = cast("list[dict[str, Any]]", entry["accepted"])
            self.assertEqual(entry["accepted_count"], len(accepted))
            self.assertLessEqual(
                len(accepted),
                cast("int", entry["scored_count"]),
            )
            for accepted_entry in accepted:
                self.assertEqual(
                    set(accepted_entry),
                    {
                        "record_id",
                        "similarity",
                        "similarity_evidence",
                        "stable_tie_key",
                    },
                )
                evidence = cast(
                    "dict[str, Any]",
                    accepted_entry["similarity_evidence"],
                )
                self.assertEqual(
                    set(evidence),
                    {
                        "levenshtein_ratio",
                        "dice_bigram",
                        "final_similarity",
                        "scorer_version",
                    },
                )
                self.assertEqual(evidence["scorer_version"], "scorer-v1")
                self.assertEqual(
                    evidence["final_similarity"],
                    accepted_entry["similarity"],
                )
                self.assertEqual(
                    accepted_entry["stable_tie_key"],
                    [0, accepted_entry["record_id"]],
                )

    def test_store_transcript_is_deterministic_and_repeatable(
        self,
    ) -> None:
        first = _observe_store_transcript(_VECTORS_FIXTURE)
        second = _observe_store_transcript(_VECTORS_FIXTURE)
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertEqual(len(first), 1)
        entry = first[0]
        self.assertEqual(entry["id"], "store-gate-c-candidate-rollback")
        self.assertEqual(entry["resource_id"], "tm.gate-c.store")
        self.assertEqual(entry["canonical_store_id"], "store.gate-c")
        self.assertEqual(entry["batch_id"], "import.gate-c.store")
        self.assertEqual(
            cast("dict[str, Any]", entry["runtime"])["index_kind"],
            cast(
                "dict[str, Any]",
                entry["candidate_report"],
            )["index_kind"],
        )

    def test_store_candidate_public_view_parity_and_stage_conservation(
        self,
    ) -> None:
        entry = _store_entry()
        parity = cast("dict[str, Any]", entry["query_view_parity"])
        self.assertTrue(all(parity.values()))
        report = cast("dict[str, Any]", entry["candidate_report"])
        candidates = cast(
            "list[dict[str, Any]]",
            report["candidates"],
        )
        exported = cast("list[object]", entry["exported_record_ids"])
        candidate_ids = [
            candidate["record_id"] for candidate in candidates
        ]
        self.assertEqual(candidate_ids, [1, 2, 3])
        self.assertTrue(set(candidate_ids).issubset(set(exported)))
        self.assertNotIn(4, candidate_ids)
        stages = cast("list[dict[str, Any]]", report["stages"])
        self.assertTrue(stages)
        self.assertEqual(stages[0]["input_count"], 0)
        for stage in stages:
            self.assertEqual(
                stage["input_count"]
                + stage["added_unique_count"]
                - stage["dropped_count"],
                stage["output_unique_count"],
            )
        self.assertEqual(stages[-1]["output_unique_count"], len(candidates))
        self.assertEqual(
            report["union_unique_count"],
            report["deduplicated_count"],
        )
        self.assertIs(report["truncated"], False)
        fixture = _load_json(_VECTORS_FIXTURE)
        expected_branch = _store_runtime_branch(fixture)
        self.assertEqual(stages, expected_branch["stages"])
        self.assertEqual(
            candidate_ids,
            [
                expected["record_id"]
                for expected in expected_branch["candidates"]
            ],
        )

    def test_store_rollback_preserves_revision_records_and_candidates(
        self,
    ) -> None:
        entry = _store_entry()
        rollback = cast("dict[str, Any]", entry["rollback"])
        self.assertEqual(
            rollback["exception_type"],
            "sqlite3.IntegrityError",
        )
        for field_name in _store_rollback_fields():
            self.assertIs(rollback[field_name], True)
        self.assertEqual(
            rollback["record_count_before"],
            rollback["record_count_after"],
        )
        self.assertEqual(
            rollback["exported_count_before"],
            rollback["exported_count_after"],
        )
        self.assertEqual(
            rollback["candidate_count_before"],
            rollback["candidate_count_after"],
        )
        self.assertEqual(rollback["absent_record_id"], 5)
        self.assertEqual(
            rollback["record_count_after"],
            len(cast("list[object]", entry["exported_record_ids"])),
        )
        self.assertEqual(
            rollback["candidate_count_after"],
            len(
                cast(
                    "list[dict[str, Any]]",
                    entry["candidate_report"]["candidates"],
                )
            ),
        )
        serialized = json.dumps(
            entry,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.assertNotIn("UNIQUE constraint", serialized)

    def test_store_rollback_absent_membership_uses_exported_record_ids(
        self,
    ) -> None:
        exported = tuple(
            _rollback_record(record_id) for record_id in (1, 2, 3, 4)
        )
        revision = CanonicalRevisionSnapshot(
            resource_id="tm.gate-c.store",
            canonical_store_id="store.gate-c",
            generation=0,
            head_revision=1,
            record_count=4,
        )
        health = StoreHealth(
            healthy=True,
            schema_version=2,
            generation=0,
            record_count=4,
            index_kind="FTS5_TRIGRAM",
            snapshot_binding_digest=None,
            source_binding_state=None,
            exact_available=True,
            context_available=False,
            fuzzy_available=False,
            diagnostic_codes=(),
        )
        metadata = CandidateRecallMetadata(
            resource_id="tm.gate-c.store",
            index_kind="FTS5_TRIGRAM",
            fuzzy_available=False,
            fuzzy_unavailable_code="RETRIEVAL.FUZZY_CORRECTNESS_EVIDENCE_FAILED",
            stages=(),
            union_unique_count=0,
            deduplicated_count=0,
            result_limit=10,
            candidate_budget_version="candidate-budget-v1",
            candidate_budget=2048,
            truncated=False,
        )
        report = CandidateRetrievalReport(candidates=(), metadata=metadata)
        config = _StoreConfig(
            id="store-gate-c-candidate-rollback",
            resource_id="tm.gate-c.store",
            canonical_store_id="store.gate-c",
            stage_id="stage.gate-c.store",
            batch_id="import.gate-c.store",
            batch_kind="import",
            source_digest="f" * 64,
            source_name="gate-c.store.jsonl",
            duplicate_source_digest="e" * 64,
            duplicate_source_name="duplicate.jsonl",
            query_source="Open the door.",
            folded_query="openthedoor",
            minimum_similarity=0.7,
            result_limit=10,
            drafts=(),
            duplicate_draft=_StoreDraftConfig(
                ordinal=0,
                source_raw="Open the door.",
                target_raw="target-5",
            ),
        )

        def facts(
            *,
            after_exported: tuple[TMRecord, ...],
            exact_absent: bool = True,
        ) -> dict[str, Any]:
            return _store_rollback_facts(
                config=config,
                exception_type="sqlite3.IntegrityError",
                before_revision=revision,
                before_exported=exported,
                before_health=health,
                before_report=report,
                after_revision=revision,
                after_exported=after_exported,
                after_health=health,
                after_report=report,
                exact_absent=exact_absent,
            )

        # An after-exported TMRecord carrying the would-be new id must be
        # detected by the exported record id membership check: absent is
        # False even though the query-view exact probe stayed empty.
        present = facts(after_exported=exported + (_rollback_record(5),))
        self.assertEqual(present["absent_record_id"], 5)
        self.assertIs(present["absent"], False)

        # The real rollback state (no such exported record) stays absent.
        absent = facts(after_exported=exported)
        self.assertEqual(absent["absent_record_id"], 5)
        self.assertIs(absent["absent"], True)

        # A negative exact probe closes the absent claim regardless.
        negative = facts(
            after_exported=exported,
            exact_absent=False,
        )
        self.assertIs(negative["absent"], False)

    def test_store_transcript_is_body_safe_and_shape_closed(self) -> None:
        entry = _store_entry()
        serialized = json.dumps(
            entry,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for body in _VECTOR_BODIES:
            self.assertNotIn(body, serialized)
        self.assertNotIn("UNIQUE constraint", serialized)
        self.assertNotIn(".sqlite3", serialized)
        self.assertNotIn(".jsonl", serialized)
        self.assertEqual(
            set(entry),
            {
                "id",
                "resource_id",
                "canonical_store_id",
                "batch_id",
                "runtime",
                "revision",
                "exported_record_ids",
                "candidate_report",
                "query_view_parity",
                "scoring",
                "rollback",
            },
        )
        self.assertEqual(
            set(cast("dict[str, Any]", entry["runtime"])),
            {"fts5_available", "index_kind", "schema_version"},
        )
        self.assertEqual(
            set(cast("dict[str, Any]", entry["revision"])),
            {"generation", "head_revision", "record_count"},
        )
        report = cast("dict[str, Any]", entry["candidate_report"])
        self.assertEqual(
            set(report),
            {
                "index_kind",
                "result_limit",
                "candidate_budget",
                "union_unique_count",
                "deduplicated_count",
                "truncated",
                "stages",
                "candidates",
            },
        )
        for stage in cast("list[dict[str, Any]]", report["stages"]):
            self.assertEqual(
                set(stage),
                {
                    "stage",
                    "input_count",
                    "added_unique_count",
                    "output_unique_count",
                    "dropped_count",
                },
            )
        for candidate in cast(
            "list[dict[str, Any]]", report["candidates"]
        ):
            self.assertEqual(
                set(candidate),
                {
                    "record_id",
                    "recall_stages",
                    "matched_grams",
                    "query_grams",
                    "overlap_ratio",
                    "pretruncate_rank",
                },
            )
        self.assertEqual(
            set(cast("dict[str, Any]", entry["query_view_parity"])),
            {
                "candidate_report_equal",
                "metadata_equal",
                "candidate_identities_equal",
                "stages_equal",
            },
        )
        scoring = cast("dict[str, Any]", entry["scoring"])
        self.assertEqual(
            set(scoring),
            {"scored_count", "accepted_count", "accepted"},
        )
        for accepted in cast("list[dict[str, Any]]", scoring["accepted"]):
            self.assertEqual(
                set(accepted),
                {"record_id", "similarity", "similarity_evidence"},
            )
            self.assertEqual(
                set(
                    cast(
                        "dict[str, Any]",
                        accepted["similarity_evidence"],
                    )
                ),
                {"levenshtein_ratio", "dice_bigram", "final_similarity"},
            )
        rollback = cast("dict[str, Any]", entry["rollback"])
        self.assertEqual(
            set(rollback),
            {
                "batch_id",
                "exception_type",
                "revision_unchanged",
                "exported_unchanged",
                "health_unchanged",
                "candidates_unchanged",
                "record_count_before",
                "record_count_after",
                "exported_count_before",
                "exported_count_after",
                "candidate_count_before",
                "candidate_count_after",
                "absent_record_id",
                "absent",
            },
        )

    def test_store_fixture_tamper_fails_closed(self) -> None:
        for tamper in ("candidate", "stage", "exception", "absent"):
            with self.subTest(tamper=tamper):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _copy_validation_inputs(root)
                    fixture = (
                        root
                        / "tests/fixtures/retrieval_gate_c_vectors_v1.json"
                    )
                    payload = _load_json(fixture)
                    expected = cast(
                        "dict[str, Any]",
                        payload["store"]["expected"],
                    )
                    if tamper == "candidate":
                        for branch in expected["by_runtime"].values():
                            branch["candidates"][0]["record_id"] = 99
                    elif tamper == "stage":
                        for branch in expected["by_runtime"].values():
                            branch["stages"][-1][
                                "output_unique_count"
                            ] = 2
                    elif tamper == "exception":
                        expected["rollback"]["exception_type"] = (
                            "sqlite3.OperationalError"
                        )
                    else:
                        expected["rollback"]["absent"] = False
                    fixture.write_text(
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        encoding="utf-8",
                    )
                    release = recompute_retrieval_validation(
                        repository_root=root,
                        approved_roots_path=_APPROVED_ROOTS,
                        generated_at_utc=_FIXED_GENERATED_AT,
                        valid_until_utc=_FIXED_VALID_UNTIL,
                    )
                    self.assertIsNone(release.manifest)

    def test_store_closed_fields_and_unknown_values_fail(self) -> None:
        base_payload = _load_json(_VECTORS_FIXTURE)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def tampered(payload: dict[str, Any]) -> Path:
                path = directory / "tampered.json"
                path.write_text(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                return path

            missing_store = json.loads(json.dumps(base_payload))
            del missing_store["store"]
            with self.assertRaisesRegex(ValueError, "not closed"):
                _observe_store_transcript(tampered(missing_store))

            extra_field = json.loads(json.dumps(base_payload))
            cast("dict[str, Any]", extra_field["store"])["extra"] = True
            with self.assertRaisesRegex(ValueError, "not closed"):
                _observe_store_transcript(tampered(extra_field))

            extra_draft = json.loads(json.dumps(base_payload))
            cast(
                "list[dict[str, Any]]",
                extra_draft["store"]["drafts"],
            )[0]["extra"] = True
            with self.assertRaisesRegex(ValueError, "not closed"):
                _observe_store_transcript(tampered(extra_draft))

            duplicate_ordinal = json.loads(json.dumps(base_payload))
            drafts = cast(
                "list[dict[str, Any]]",
                duplicate_ordinal["store"]["drafts"],
            )
            drafts[1]["ordinal"] = drafts[0]["ordinal"]
            with self.assertRaisesRegex(ValueError, "must be unique"):
                _observe_store_transcript(tampered(duplicate_ordinal))

            unknown_candidate = json.loads(json.dumps(base_payload))
            for branch in cast(
                "dict[str, Any]",
                unknown_candidate["store"]["expected"]["by_runtime"],
            ).values():
                branch["candidates"][0]["record_id"] = 99
            with self.assertRaisesRegex(ValueError, "diverged"):
                _observe_store_transcript(tampered(unknown_candidate))

            wrong_absent_id = json.loads(json.dumps(base_payload))
            cast(
                "dict[str, Any]",
                wrong_absent_id["store"]["expected"]["rollback"],
            )["absent_record_id"] = 7
            with self.assertRaisesRegex(ValueError, "absent record id"):
                _observe_store_transcript(tampered(wrong_absent_id))

            unsupported_stage = json.loads(json.dumps(base_payload))
            for branch in cast(
                "dict[str, Any]",
                unsupported_stage["store"]["expected"]["by_runtime"],
            ).values():
                branch["stages"][0]["stage"] = "NOPE"
            with self.assertRaisesRegex(ValueError, "unsupported"):
                _observe_store_transcript(tampered(unsupported_stage))

            wrong_version = json.loads(json.dumps(base_payload))
            cast("dict[str, Any]", wrong_version["store"])[
                "version"
            ] = "retrieval-gate-c-store-v2"
            with self.assertRaisesRegex(ValueError, "unsupported store"):
                _observe_store_transcript(tampered(wrong_version))

    def test_store_observation_leaves_no_residual_files(self) -> None:
        before = _snapshot_file_set(_REPOSITORY_ROOT)
        _observe_store_transcript(_VECTORS_FIXTURE)
        after = _snapshot_file_set(_REPOSITORY_ROOT)
        self.assertEqual(before, after)

    def test_service_transcript_is_deterministic_and_repeatable(
        self,
    ) -> None:
        first = _service_entries()
        second = _service_entries()
        self.assertEqual(first, second)
        self.assertEqual(
            [cast("str", entry["id"]) for entry in first],
            [
                "service-gate-c-global-limit",
                "service-gate-c-partial-failure",
                "service-gate-c-refresh-snapshot",
            ],
        )
        for entry in first:
            self.assertEqual(
                entry["version"],
                "retrieval-gate-c-service-v1",
            )
            self.assertEqual(
                set(cast("dict[str, Any]", entry["query"])),
                {"limit", "minimum_similarity", "resource_order"},
            )
            self.assertEqual(
                cast("dict[str, Any]", entry["query"])["resource_order"],
                [
                    "tm.gate-c.service.primary",
                    "tm.gate-c.service.secondary",
                ],
            )

    def test_service_global_limit_aggregates_before_limit(self) -> None:
        entry = _service_entry("service-gate-c-global-limit")
        capability = cast("dict[str, Any]", entry["capability"])
        self.assertIs(capability["context"]["available"], True)
        self.assertIsNone(capability["context"]["unavailable_code"])
        self.assertEqual(
            cast("dict[str, Any]", entry["aggregation"]),
            {
                "result_count": 2,
                "result_record_ids": [2, 2],
                "result_resource_ids": [
                    "tm.gate-c.service.primary",
                    "tm.gate-c.service.secondary",
                ],
                "returned_count_by_resource": {
                    "tm.gate-c.service.primary": 1,
                    "tm.gate-c.service.secondary": 1,
                },
                "context_observed_count": 2,
                "context_returned_count": 0,
                "scored_count_total": 0,
            },
        )
        self.assertEqual(entry["failures"], [])
        resources = cast("list[dict[str, Any]]", entry["resources"])
        self.assertEqual(len(resources), 2)
        for resource in resources:
            context = cast("dict[str, Any]", resource["context"])
            self.assertIs(context["available"], True)
            self.assertIsNone(context["unavailable_code"])
            self.assertEqual(context["observed_count"], 1)
            self.assertEqual(context["returned_count"], 0)
            results = cast("list[dict[str, Any]]", resource["results"])
            self.assertEqual(
                [(result["record_id"], result["match_type"]) for result in results],
                [(2, "EXACT")],
            )
            recall = cast("dict[str, Any]", resource["recall"])
            self.assertIs(recall["fuzzy_available"], False)
            self.assertEqual(
                recall["fuzzy_unavailable_code"],
                RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE,
            )
            self.assertEqual(recall["stages"], [])
            self.assertEqual(resource["scored_count"], 0)

    def test_service_partial_failure_preserves_healthy_resource(
        self,
    ) -> None:
        entry = _service_entry("service-gate-c-partial-failure")
        self.assertEqual(
            entry["failures"],
            [
                {
                    "resource_id": "tm.gate-c.service.secondary",
                    "stage": "LEASE",
                    "error_code": "STORE.GENERATION_CHANGED",
                    "retryable": True,
                }
            ],
        )
        self.assertEqual(
            cast("dict[str, Any]", entry["aggregation"]),
            {
                "result_count": 2,
                "result_record_ids": [2, 1],
                "result_resource_ids": [
                    "tm.gate-c.service.primary",
                    "tm.gate-c.service.primary",
                ],
                "returned_count_by_resource": {
                    "tm.gate-c.service.primary": 2,
                },
                "context_observed_count": 1,
                "context_returned_count": 1,
                "scored_count_total": 0,
            },
        )
        resources = cast("list[dict[str, Any]]", entry["resources"])
        self.assertEqual(len(resources), 1)
        resource = resources[0]
        self.assertEqual(
            resource["resource_id"],
            "tm.gate-c.service.primary",
        )
        self.assertEqual(
            [
                (result["record_id"], result["match_type"])
                for result in cast(
                    "list[dict[str, Any]]", resource["results"]
                )
            ],
            [(2, "EXACT"), (1, "CONTEXT")],
        )
        context = cast("dict[str, Any]", resource["context"])
        self.assertEqual(context["observed_count"], 1)
        self.assertEqual(context["returned_count"], 1)

    def test_service_harness_keeps_fuzzy_and_gate_d_closed(self) -> None:
        for entry in _service_entries():
            capability = cast("dict[str, Any]", entry["capability"])
            self.assertIs(capability["fuzzy_core"]["available"], False)
            self.assertEqual(
                capability["fuzzy_core"]["unavailable_code"],
                RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE,
            )
            self.assertIs(capability["fts5_trigram"]["available"], False)
            self.assertIs(capability["gram_fallback"]["available"], False)
            self.assertEqual(
                capability["fts5_trigram"]["unavailable_code"],
                RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
            )
            self.assertEqual(
                capability["gram_fallback"]["unavailable_code"],
                RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
            )
            for resource in cast(
                "list[dict[str, Any]]", entry["resources"]
            ):
                recall = cast("dict[str, Any]", resource["recall"])
                self.assertIs(recall["fuzzy_available"], False)
                self.assertEqual(recall["stages"], [])
                self.assertEqual(
                    recall["union_unique_count"],
                    0,
                )
                self.assertEqual(
                    recall["deduplicated_count"],
                    0,
                )
                self.assertEqual(resource["scored_count"], 0)
            self.assertEqual(
                cast("dict[str, Any]", entry["aggregation"])[
                    "scored_count_total"
                ],
                0,
            )

    def test_service_refresh_keeps_inflight_snapshot_and_closes_next(
        self,
    ) -> None:
        entry = _service_entry("service-gate-c-refresh-snapshot")
        self.assertEqual(entry["kind"], "refresh_snapshot")
        self.assertEqual(entry["failures"], [])
        capability = cast("dict[str, Any]", entry["capability"])
        self.assertIs(capability["context"]["available"], True)
        self.assertIsNone(capability["context"]["unavailable_code"])
        self.assertIs(capability["fuzzy_core"]["available"], False)
        self.assertEqual(
            capability["fuzzy_core"]["unavailable_code"],
            RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE,
        )
        self.assertIs(capability["fts5_trigram"]["available"], False)
        self.assertIs(capability["gram_fallback"]["available"], False)
        self.assertEqual(
            capability["fts5_trigram"]["unavailable_code"],
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
        )
        self.assertEqual(
            capability["gram_fallback"]["unavailable_code"],
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
        )
        # The in-flight query kept the pre-refresh snapshot on both
        # resources even though the first resource's health step already
        # fired the publisher refresh.
        for resource in cast(
            "list[dict[str, Any]]", entry["resources"]
        ):
            context = cast("dict[str, Any]", resource["context"])
            self.assertIs(context["available"], True)
            self.assertIsNone(context["unavailable_code"])
            recall = cast("dict[str, Any]", resource["recall"])
            self.assertIs(recall["fuzzy_available"], False)
            self.assertEqual(
                recall["fuzzy_unavailable_code"],
                RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_FAILED_CODE,
            )
            self.assertEqual(recall["stages"], [])
            self.assertEqual(resource["scored_count"], 0)
        self.assertEqual(
            cast("dict[str, Any]", entry["aggregation"]),
            {
                "result_count": 2,
                "result_record_ids": [2, 2],
                "result_resource_ids": [
                    "tm.gate-c.service.primary",
                    "tm.gate-c.service.secondary",
                ],
                "returned_count_by_resource": {
                    "tm.gate-c.service.primary": 1,
                    "tm.gate-c.service.secondary": 1,
                },
                "context_observed_count": 2,
                "context_returned_count": 0,
                "scored_count_total": 0,
            },
        )
        # The publisher snapshot after the in-flight query is closed.
        closed = cast("dict[str, Any]", entry["capability_after_refresh"])
        self.assertIs(closed["context"]["available"], False)
        self.assertEqual(
            closed["context"]["unavailable_code"],
            RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE,
        )
        self.assertIs(closed["fuzzy_core"]["available"], False)
        self.assertEqual(
            closed["fuzzy_core"]["unavailable_code"],
            RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE,
        )
        self.assertIs(closed["fts5_trigram"]["available"], False)
        self.assertIs(closed["gram_fallback"]["available"], False)
        self.assertEqual(
            closed["fts5_trigram"]["unavailable_code"],
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
        )
        self.assertEqual(
            closed["gram_fallback"]["unavailable_code"],
            RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
        )
        # The next query observes the closed snapshot: context unavailable
        # on every successful resource and no fuzzy execution.
        second = cast("dict[str, Any]", entry["second_query"])
        self.assertEqual(second["capability"], closed)
        self.assertEqual(second["failures"], [])
        self.assertEqual(
            cast("dict[str, Any]", second["aggregation"]),
            {
                "result_count": 2,
                "result_record_ids": [2, 2],
                "result_resource_ids": [
                    "tm.gate-c.service.primary",
                    "tm.gate-c.service.secondary",
                ],
                "returned_count_by_resource": {
                    "tm.gate-c.service.primary": 1,
                    "tm.gate-c.service.secondary": 1,
                },
                "context_observed_count": 2,
                "context_returned_count": 0,
                "scored_count_total": 0,
            },
        )
        for resource in cast(
            "list[dict[str, Any]]", second["resources"]
        ):
            context = cast("dict[str, Any]", resource["context"])
            self.assertIs(context["available"], False)
            self.assertEqual(
                context["unavailable_code"],
                RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE,
            )
            self.assertEqual(
                [
                    (result["record_id"], result["match_type"])
                    for result in cast(
                        "list[dict[str, Any]]", resource["results"]
                    )
                ],
                [(2, "EXACT")],
            )
            recall = cast("dict[str, Any]", resource["recall"])
            self.assertIs(recall["fuzzy_available"], False)
            self.assertEqual(
                recall["fuzzy_unavailable_code"],
                RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE,
            )
            self.assertEqual(recall["stages"], [])
            self.assertEqual(resource["scored_count"], 0)

    def test_service_transcript_is_body_safe_and_shape_closed(self) -> None:
        for entry in _service_entries():
            serialized = json.dumps(
                entry,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for body in _VECTOR_BODIES:
                self.assertNotIn(body, serialized)
            self.assertNotIn(".sqlite3", serialized)
            self.assertNotIn(".jsonl", serialized)
            is_refresh = cast("str", entry["kind"]) == "refresh_snapshot"
            if is_refresh:
                self.assertEqual(
                    set(entry),
                    {
                        "id",
                        "kind",
                        "version",
                        "query",
                        "capability",
                        "capability_after_refresh",
                        "resources",
                        "failures",
                        "aggregation",
                        "second_query",
                    },
                )
            else:
                self.assertEqual(
                    set(entry),
                    {
                        "id",
                        "kind",
                        "version",
                        "query",
                        "capability",
                        "resources",
                        "failures",
                        "aggregation",
                    },
                )
            capability_payloads = [entry["capability"]]
            if is_refresh:
                capability_payloads.append(entry["capability_after_refresh"])
                capability_payloads.append(
                    cast(
                        "dict[str, Any]",
                        entry["second_query"],
                    )["capability"]
                )
            for capability in capability_payloads:
                self.assertEqual(
                    set(cast("dict[str, Any]", capability)),
                    {
                        "context",
                        "fuzzy_core",
                        "fts5_trigram",
                        "gram_fallback",
                        "summary_unavailable_codes",
                    },
                )
                for decision in (
                    "context",
                    "fuzzy_core",
                    "fts5_trigram",
                    "gram_fallback",
                ):
                    self.assertEqual(
                        set(
                            cast(
                                "dict[str, Any]",
                                capability[decision],
                            )
                        ),
                        {"available", "unavailable_code"},
                    )
            blocks: list[dict[str, Any]] = [entry]
            if is_refresh:
                blocks.append(
                    cast("dict[str, Any]", entry["second_query"])
                )
            for block in blocks:
                for resource in cast(
                    "list[dict[str, Any]]", block["resources"]
                ):
                    self.assertEqual(
                        set(resource),
                        {
                            "resource_id",
                            "generation",
                            "context",
                            "context_variant",
                            "recall",
                            "scored_count",
                            "returned_count",
                            "results",
                        },
                    )
                    self.assertEqual(
                        set(cast("dict[str, Any]", resource["context"])),
                        {
                            "available",
                            "unavailable_code",
                            "observed_count",
                            "returned_count",
                        },
                    )
                    self.assertEqual(
                        set(cast("dict[str, Any]", resource["recall"])),
                        {
                            "index_kind",
                            "fuzzy_available",
                            "fuzzy_unavailable_code",
                            "stages",
                            "union_unique_count",
                            "deduplicated_count",
                            "result_limit",
                            "candidate_budget",
                            "candidate_budget_version",
                            "truncated",
                        },
                    )
                    for result in cast(
                        "list[dict[str, Any]]", resource["results"]
                    ):
                        self.assertEqual(
                            set(result),
                            {"record_id", "match_type"},
                        )
                for failure in cast(
                    "list[dict[str, Any]]", block["failures"]
                ):
                    self.assertEqual(
                        set(failure),
                        {
                            "resource_id",
                            "stage",
                            "error_code",
                            "retryable",
                        },
                    )
                self.assertEqual(
                    set(cast("dict[str, Any]", block["aggregation"])),
                    {
                        "result_count",
                        "result_record_ids",
                        "result_resource_ids",
                        "returned_count_by_resource",
                        "context_observed_count",
                        "context_returned_count",
                        "scored_count_total",
                    },
                )
    def test_service_observation_leaves_no_residual_files(self) -> None:
        before = _snapshot_file_set(_REPOSITORY_ROOT)
        _service_entries()
        after = _snapshot_file_set(_REPOSITORY_ROOT)
        self.assertEqual(before, after)

    def test_service_fixture_tamper_fails_closed(self) -> None:
        for tamper in ("aggregation", "failure_facts", "refresh"):
            with self.subTest(tamper=tamper):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _copy_validation_inputs(root)
                    fixture = (
                        root
                        / "tests/fixtures/retrieval_gate_c_vectors_v1.json"
                    )
                    payload = _load_json(fixture)
                    scenarios = cast(
                        "list[dict[str, Any]]",
                        payload["service"]["scenarios"],
                    )
                    if tamper == "aggregation":
                        scenarios[0]["expected"]["result_record_ids"] = [
                            9,
                            9,
                        ]
                    elif tamper == "failure_facts":
                        scenarios[1]["failure"]["code"] = (
                            "STORE.SOMETHING_ELSE"
                        )
                    else:
                        scenarios[2]["expected_after_refresh"][
                            "result_record_ids"
                        ] = [9, 9]
                    fixture.write_text(
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        encoding="utf-8",
                    )
                    fixture_relative = str(
                        fixture.relative_to(root)
                    )
                    fixture_digest = aggregate_paths_digest(
                        root,
                        (fixture_relative,),
                    )
                    context_digest = canonical_digest(
                        {
                            "fixture_digest": fixture_digest,
                            "transcript": (
                                _observe_context_transcript(fixture)
                            ),
                        }
                    )
                    roots = _load_json(_APPROVED_ROOTS)
                    roots["fixture_digest"] = fixture_digest
                    roots["context_cohorts"][
                        RETRIEVAL_CONTEXT_COHORT_ID
                    ] = context_digest
                    roots_path = (
                        root
                        / "tests/fixtures/retrieval_gate_c_roots_v1.json"
                    )
                    roots_path.parent.mkdir(parents=True, exist_ok=True)
                    roots_path.write_text(
                        json.dumps(
                            roots,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        encoding="utf-8",
                    )
                    release = recompute_retrieval_validation(
                        repository_root=root,
                        approved_roots_path=roots_path,
                        generated_at_utc=_FIXED_GENERATED_AT,
                        valid_until_utc=_FIXED_VALID_UNTIL,
                    )
                    self.assertIsNone(release.manifest)

    def test_service_closed_fields_and_unknown_values_fail(self) -> None:
        base_payload = _load_json(_VECTORS_FIXTURE)
        expectation = _release().expectation
        fixture_digest, context_digest, harness_fuzzy_digest = (
            _service_digests()
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def tampered(payload: dict[str, Any]) -> Path:
                path = directory / "tampered.json"
                path.write_text(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                return path

            missing_service = json.loads(json.dumps(base_payload))
            del missing_service["service"]
            with self.assertRaisesRegex(ValueError, "not closed"):
                _observe_service_transcript(
                    tampered(missing_service),
                    expectation=expectation,
                    observed_context_digest=context_digest,
                    harness_fuzzy_core_digest=harness_fuzzy_digest,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            extra_field = json.loads(json.dumps(base_payload))
            cast("dict[str, Any]", extra_field["service"])["extra"] = True
            with self.assertRaisesRegex(ValueError, "not closed"):
                _observe_service_transcript(
                    tampered(extra_field),
                    expectation=expectation,
                    observed_context_digest=context_digest,
                    harness_fuzzy_core_digest=harness_fuzzy_digest,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            wrong_version = json.loads(json.dumps(base_payload))
            cast("dict[str, Any]", wrong_version["service"])[
                "version"
            ] = "retrieval-gate-c-service-v2"
            with self.assertRaisesRegex(ValueError, "unsupported service"):
                _observe_service_transcript(
                    tampered(wrong_version),
                    expectation=expectation,
                    observed_context_digest=context_digest,
                    harness_fuzzy_core_digest=harness_fuzzy_digest,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            duplicate_resource = json.loads(json.dumps(base_payload))
            resources = cast(
                "list[dict[str, Any]]",
                duplicate_resource["service"]["resources"],
            )
            resources.append(dict(resources[0]))
            with self.assertRaisesRegex(ValueError, "resource ids must be unique"):
                _observe_service_transcript(
                    tampered(duplicate_resource),
                    expectation=expectation,
                    observed_context_digest=context_digest,
                    harness_fuzzy_core_digest=harness_fuzzy_digest,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

            duplicate_scenario = json.loads(json.dumps(base_payload))
            scenarios = cast(
                "list[dict[str, Any]]",
                duplicate_scenario["service"]["scenarios"],
            )
            scenarios.append(dict(scenarios[0]))
            with self.assertRaisesRegex(ValueError, "scenario ids must be unique"):
                _observe_service_transcript(
                    tampered(duplicate_scenario),
                    expectation=expectation,
                    observed_context_digest=context_digest,
                    harness_fuzzy_core_digest=harness_fuzzy_digest,
                    generated_at_utc=_FIXED_GENERATED_AT,
                    valid_until_utc=_FIXED_VALID_UNTIL,
                )

    def test_transcript_is_body_safe_and_shape_closed(self) -> None:
        transcript = _observe_context_transcript(_VECTORS_FIXTURE)
        self.assertTrue(transcript)
        serialized = json.dumps(
            transcript,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for body in _VECTOR_BODIES:
            self.assertNotIn(body, serialized)
        for entry in transcript:
            self.assertEqual(
                set(entry),
                {
                    "id",
                    "winner_record_id",
                    "winner_match_type",
                    "context",
                    "context_count",
                    "retained_record_ids",
                    "retained_count",
                },
            )
            self.assertEqual(entry["winner_match_type"], "EXACT")
            self.assertEqual(
                entry["context_count"],
                len(cast("list[object]", entry["context"])),
            )
            self.assertEqual(
                entry["retained_count"],
                len(cast("list[object]", entry["retained_record_ids"])),
            )
            for context_entry in cast(
                "list[dict[str, Any]]", entry["context"]
            ):
                self.assertEqual(
                    set(context_entry),
                    {
                        "record_id",
                        "match_type",
                        "comparable_fields",
                        "matched_fields",
                        "mismatched_fields",
                        "strength_v1",
                    },
                )
                self.assertEqual(context_entry["match_type"], "CONTEXT")
                self.assertEqual(
                    len(context_entry["strength_v1"]), 5
                )
                self.assertTrue(
                    set(context_entry["comparable_fields"]).issubset(
                        {
                            "speaker_raw",
                            "context_prev_raw",
                            "context_next_raw",
                        }
                    )
                )

    def test_manifest_and_summary_are_body_safe(self) -> None:
        release = _release()
        manifest = release.manifest
        assert manifest is not None
        strings = tuple(_iter_strings(manifest))
        for body in _VECTOR_BODIES:
            self.assertNotIn(body, strings)
        for row in manifest.context_cohorts + manifest.fuzzy_core_cohorts:
            self.assertIsNotNone(_DIGEST.fullmatch(row.cohort_digest))
            self.assertIsNotNone(
                _STRICT_UTC.fullmatch(row.generated_at_utc)
            )
            self.assertIsNotNone(_STRICT_UTC.fullmatch(row.valid_until_utc))
        snapshot = _snapshot(release)
        summary = snapshot.summary
        self.assertIsNotNone(_DIGEST.fullmatch(summary.evidence_digest))
        self.assertEqual(
            summary.summary_version,
            "retrieval-capability-summary-v1",
        )
        summary_strings = tuple(_iter_strings(summary))
        for body in _VECTOR_BODIES:
            self.assertNotIn(body, summary_strings)

    def test_recompute_persists_no_output(self) -> None:
        before = _snapshot_file_set(_REPOSITORY_ROOT)
        _release()
        after = _snapshot_file_set(_REPOSITORY_ROOT)
        self.assertEqual(before, after)

    def test_roots_fixture_is_frozen_schema(self) -> None:
        roots = _load_json(_APPROVED_ROOTS)
        self.assertEqual(
            roots["schema_version"],
            RETRIEVAL_GATE_C_ROOTS_SCHEMA_VERSION,
        )
        self.assertEqual(roots["semantics_version"], "retrieval-v1")
        self.assertEqual(
            set(roots),
            {
                "artifact_digest",
                "artifact_paths",
                "build_digest",
                "build_paths",
                "context_cohorts",
                "evaluator_digest",
                "evaluator_path",
                "fixture_digest",
                "fixture_paths",
                "fts5_trigram",
                "fuzzy_core_cohorts",
                "gram_fallback",
                "schema_version",
                "semantics_version",
            },
        )


if __name__ == "__main__":
    unittest.main()
