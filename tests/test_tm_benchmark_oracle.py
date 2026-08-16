"""Focused tests for the benchmark-v1 full-scan oracle / recall-gate owner."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any
import unittest
from unittest.mock import patch

import tm_benchmark_oracle
from text_matcher import fold_text_v1
from tm_benchmark import (
    BenchmarkQuery,
    BenchmarkRecord,
    _LANGUAGES,
    benchmark_implementation_fingerprint,
    compute_benchmark_input_plan,
    iter_oracle_queries,
    iter_oracle_subset_records,
    load_benchmark_contract,
    recompute_benchmark_inputs,
)
from tm_benchmark_oracle import (
    ORACLE_EVIDENCE_SCHEMA_VERSION,
    FullScanQueryOracle,
    OraclePathUnavailableError,
    OracleQueryRow,
    OracleRecallEvidence,
    _evidence_payload,
    _parse_strict_json,
    collect_oracle_environment,
    compute_full_scan_oracle,
    evidence_from_json,
    evidence_from_payload,
    evidence_to_json,
    oracle_evidence_digest,
    run_oracle_recall_evidence,
    run_oracle_recall_suite,
    validate_oracle_environment,
)
from tm_contracts import (
    CANDIDATE_PROOF_QUERY_VERSION,
    BenchmarkContract,
    BenchmarkExecutionPath,
    benchmark_contract_digest,
    benchmark_environment_digest,
    candidate_budget_v1,
    contract_to_json,
)
from tm_similarity import SimilarityScorerV1

_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT = load_benchmark_contract(_ROOT / "benchmark_tm_contract.json")
_FTS5 = BenchmarkExecutionPath.FTS5_TRIGRAM
_FALLBACK = BenchmarkExecutionPath.GRAM_FALLBACK
_SCORER = SimilarityScorerV1()
_IMPLEMENTATION_FINGERPRINT = benchmark_implementation_fingerprint(_ROOT)

_IMPORT_RE = re.compile(
    r"^(?:import|from)\s+([A-Za-z0-9_\.]+)",
    re.MULTILINE,
)

_BANNED_RUNTIME_MODULES = {
    "tm_sqlite_store",
    "tm_retrieval",
    "tm_retrieval_capability",
    "tm_retrieval_validation",
    "tm_migration",
    "tm_candidate_index",
    "tm_engine",
    "text_matcher",
    "tm_similarity",
    "matcher_capability",
    "matcher_validation",
    "qt_editor",
    "tm_snapshot_artifacts",
    "tm_snapshot_recovery",
    "tm_gate_a",
    "tm_gate_b",
}


def _environment(fts5_enabled: bool) -> tuple[tuple[str, str], ...]:
    return collect_oracle_environment(fts5_enabled=fts5_enabled)


def _row(
    query_id: int,
    *,
    category: str = "exact",
    reference_record_id: int | None = None,
    candidate_ids: tuple[int, ...] = (),
    above_threshold_ids: tuple[int, ...] | None = None,
    top10_ids: tuple[int, ...] | None = None,
    actual_index_kind: str = "FTS5_TRIGRAM",
    candidate_available: bool = True,
    unavailable_code: str | None = None,
    truncated: bool = False,
) -> OracleQueryRow:
    if reference_record_id is None and category in ("exact", "near-edit"):
        reference_record_id = query_id
    if above_threshold_ids is None:
        above_threshold_ids = (query_id,)
    if top10_ids is None:
        top10_ids = tuple(range(1, 11))
    candidate_set = set(candidate_ids)
    missing_above = tuple(
        record_id
        for record_id in above_threshold_ids
        if record_id not in candidate_set
    )
    missing_top10 = tuple(
        record_id
        for record_id in top10_ids
        if record_id not in candidate_set
    )
    return OracleQueryRow(
        query_id=query_id,
        category=category,
        reference_record_id=reference_record_id,
        candidate_ids=candidate_ids,
        above_threshold_ids=above_threshold_ids,
        top10_ids=top10_ids,
        missing_above_threshold_ids=missing_above,
        missing_top10_ids=missing_top10,
        candidate_count=len(candidate_ids),
        above_count=len(above_threshold_ids),
        top10_count=len(top10_ids),
        actual_index_kind=actual_index_kind,
        candidate_available=candidate_available,
        unavailable_code=unavailable_code,
        truncated=truncated,
    )


def _small_evidence(**overrides: Any) -> OracleRecallEvidence:
    execution_path: BenchmarkExecutionPath = overrides.pop(
        "execution_path", _FTS5
    )
    test_mode: bool = overrides.pop("test_mode", True)
    rows = overrides.pop(
        "rows",
        (
            _row(1, candidate_ids=(1,)),
            _row(2, category="near-edit", candidate_ids=(1, 2)),
        ),
    )
    environment = overrides.pop(
        "environment",
        _environment(fts5_enabled=execution_path is _FTS5),
    )
    record_count: int = overrides.pop("oracle_subset_record_count", 12)
    query_count: int = overrides.pop("oracle_query_count", len(rows))
    contract = overrides.pop("contract", _CONTRACT)
    contract_digest = overrides.pop(
        "contract_digest", benchmark_contract_digest(contract)
    )
    oracle_digest = overrides.pop(
        "oracle_subset_digest",
        "0" * 64 if test_mode else contract.oracle_subset_digest,
    )
    environment_digest = overrides.pop(
        "environment_digest", benchmark_environment_digest(environment)
    )
    evidence_digest = overrides.pop("evidence_digest", None)
    path_config_digest = overrides.pop(
        "path_config_digest",
        contract.fast_path_config_digest
        if execution_path is _FTS5
        else contract.fallback_path_config_digest,
    )
    store_index_kind = overrides.pop(
        "store_index_kind",
        "FTS5_TRIGRAM" if execution_path is _FTS5 else "GRAM_FALLBACK",
    )
    result_limit: int = overrides.pop("result_limit", contract.top_k)
    budget: int = overrides.pop(
        "candidate_budget", candidate_budget_v1(result_limit)
    )
    missing_above_total: int = overrides.pop(
        "missing_above_threshold_total",
        sum(len(row.missing_above_threshold_ids) for row in rows),
    )
    missing_top10_total: int = overrides.pop(
        "missing_top10_total",
        sum(len(row.missing_top10_ids) for row in rows),
    )
    all_available: bool = overrides.pop(
        "all_queries_available",
        all(row.candidate_available for row in rows),
    )
    drift_count: int = overrides.pop(
        "index_kind_drift_count",
        sum(1 for row in rows if row.actual_index_kind != store_index_kind),
    )
    recall_passed: bool = overrides.pop(
        "recall_passed",
        (
            all_available
            and drift_count == 0
            and missing_above_total == 0
            and missing_top10_total == 0
        ),
    )
    if overrides:
        raise TypeError(f"unexpected overrides: {sorted(overrides)}")
    evidence = OracleRecallEvidence(
        schema_version=ORACLE_EVIDENCE_SCHEMA_VERSION,
        proof_query_version=CANDIDATE_PROOF_QUERY_VERSION,
        implementation_fingerprint=_IMPLEMENTATION_FINGERPRINT,
        test_mode=test_mode,
        contract=contract,
        contract_digest=contract_digest,
        oracle_subset_digest=oracle_digest,
        oracle_subset_record_count=record_count,
        oracle_query_count=query_count,
        scorer_config_digest=(
            _CONTRACT.scorer_config_digest
            if contract is _CONTRACT
            else _recompute_scorer_digest(contract)
        ),
        path_config_digest=path_config_digest,
        execution_path=execution_path,
        store_index_kind=store_index_kind,
        resource_id="tm.benchmark",
        canonical_store_id="store.benchmark",
        fixture_digest="b" * 64,
        result_limit=result_limit,
        candidate_budget_version="candidate-budget-v1",
        candidate_budget=budget,
        environment=environment,
        environment_digest=environment_digest,
        rows=rows,
        query_count=query_count,
        missing_above_threshold_total=missing_above_total,
        missing_top10_total=missing_top10_total,
        all_queries_available=all_available,
        index_kind_drift_count=drift_count,
        recall_passed=recall_passed,
    )
    if evidence_digest is not None:
        object.__setattr__(evidence, "evidence_digest", evidence_digest)
    return evidence


def _recompute_scorer_digest(contract: BenchmarkContract) -> str:
    from tm_benchmark_oracle import recompute_scorer_config_digest

    return recompute_scorer_config_digest(contract)


def _make_record(
    record_id: int,
    source: str,
    target: str = "target",
) -> BenchmarkRecord:
    return BenchmarkRecord(
        record_id=record_id,
        source_raw=source,
        target_raw=target,
        language=_LANGUAGES[0],
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        file_source=None,
        provenance=(("source", "test"),),
        origin_batch_id="test.batch",
        origin_ordinal=record_id - 1,
        legacy_line_no=None,
    )


def _make_query(
    query_id: int,
    query_raw: str,
    *,
    category: str,
    reference_record_id: int | None,
) -> BenchmarkQuery:
    return BenchmarkQuery(
        query_id=query_id,
        query_raw=query_raw,
        cohort="oracle",
        category=category,
        reference_record_id=reference_record_id,
    )


class OracleEvidenceConstructorTests(unittest.TestCase):
    def test_evidence_is_frozen_and_digest_is_derived(self) -> None:
        evidence = _small_evidence()
        with self.assertRaises((AttributeError, TypeError)):
            evidence.evidence_digest = "f" * 64  # pyright: ignore[reportAttributeAccessIssue]
        self.assertEqual(
            evidence.recompute_evidence_digest(),
            evidence.evidence_digest,
        )
        self.assertEqual(
            evidence.recompute_environment_digest(),
            evidence.environment_digest,
        )
        self.assertFalse(evidence.final_evidence)

    def test_test_mode_is_never_final_evidence(self) -> None:
        evidence = _small_evidence(test_mode=True)
        self.assertFalse(evidence.final_evidence)

    def test_real_mode_requires_literal_counts_and_contract_digests(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(
                test_mode=False,
                oracle_subset_record_count=12,
                oracle_query_count=2,
            )
        with self.assertRaises(ValueError):
            _small_evidence(
                test_mode=False,
                oracle_subset_digest="1" * 64,
            )

    def test_rejects_contract_digest_drift(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(contract_digest="f" * 64)
        with self.assertRaises((TypeError, ValueError)):
            _small_evidence(contract_digest=123)  # type: ignore[arg-type]

    def test_rejects_path_config_and_store_index_drift(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(
                path_config_digest=_CONTRACT.fallback_path_config_digest
            )
        with self.assertRaises(ValueError):
            _small_evidence(store_index_kind="GRAM_FALLBACK")
        with self.assertRaises(ValueError):
            _small_evidence(store_index_kind="SOMETHING_ELSE")

    def test_rejects_environment_drift_and_path_binding(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(environment_digest="e" * 64)
        fallback_environment = _environment(fts5_enabled=False)
        with self.assertRaises(ValueError):
            _small_evidence(execution_path=_FTS5, environment=fallback_environment)
        fast_environment = _environment(fts5_enabled=True)
        with self.assertRaises(ValueError):
            _small_evidence(
                execution_path=_FALLBACK,
                environment=fast_environment,
            )

    def test_validate_oracle_environment_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            validate_oracle_environment(_environment(True), _FALLBACK)
        with self.assertRaises(ValueError):
            validate_oracle_environment(_environment(False), _FTS5)
        validate_oracle_environment(_environment(True), _FTS5)
        validate_oracle_environment(_environment(False), _FALLBACK)

    def test_rejects_budget_and_result_limit_drift(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(result_limit=5)
        with self.assertRaises(ValueError):
            _small_evidence(candidate_budget=1)
        with self.assertRaises(ValueError):
            _small_evidence(result_limit=10, candidate_budget=4096)

    def test_rejects_missing_set_inconsistency(self) -> None:
        row = _row(1, candidate_ids=(1,))
        with self.assertRaises(ValueError):
            forged = replace(row, missing_top10_ids=())
        with self.assertRaises(ValueError):
            forged = replace(row, missing_top10_ids=())
            _small_evidence(rows=(forged, _row(2, candidate_ids=(1, 2))))

    def test_forged_recall_passed_cannot_self_authorize(self) -> None:
        with self.assertRaises(ValueError):
            _small_evidence(recall_passed=True)

    def test_recall_passed_true_only_when_nothing_missing(self) -> None:
        rows = (
            _row(1, candidate_ids=tuple(range(1, 11))),
            _row(2, category="near-edit", candidate_ids=tuple(range(1, 11))),
        )
        evidence = _small_evidence(rows=rows)
        self.assertTrue(evidence.recall_passed)
        self.assertEqual(evidence.missing_above_threshold_total, 0)
        self.assertEqual(evidence.missing_top10_total, 0)

    def test_rejects_row_id_gaps_duplicates_and_out_of_order(self) -> None:
        rows = (_row(1), _row(2))
        with self.assertRaises(ValueError):
            _small_evidence(rows=(_row(2), _row(1)))
        with self.assertRaises(ValueError):
            _small_evidence(rows=(_row(1), _row(3)))
        with self.assertRaises(ValueError):
            _small_evidence(rows=(_row(1), _row(1)))

    def test_rejects_partial_top10_obligation(self) -> None:
        row = _row(1, top10_ids=tuple(range(1, 10)))
        with self.assertRaises(ValueError):
            _small_evidence(rows=(row, _row(2, candidate_ids=(1, 2))))

    def test_rejects_unknown_category_and_reference_inconsistency(self) -> None:
        with self.assertRaises(ValueError):
            _row(1, category="fuzzy")
        with self.assertRaises(ValueError):
            _row(1, category="miss", reference_record_id=1)
        with self.assertRaises(ValueError):
            OracleQueryRow(
                query_id=1,
                category="exact",
                reference_record_id=None,
                candidate_ids=(1,),
                above_threshold_ids=(1,),
                top10_ids=tuple(range(1, 11)),
                missing_above_threshold_ids=(),
                missing_top10_ids=tuple(range(2, 11)),
                candidate_count=1,
                above_count=1,
                top10_count=10,
                actual_index_kind="FTS5_TRIGRAM",
                candidate_available=True,
                unavailable_code=None,
                truncated=False,
            )

    def test_rejects_unavailable_rows_without_code_and_nonempty(self) -> None:
        with self.assertRaises(ValueError):
            _row(1, candidate_available=False, unavailable_code=None)
        with self.assertRaises(ValueError):
            _row(
                1,
                candidate_available=False,
                unavailable_code="CANDIDATE.FTS5_UNAVAILABLE",
                candidate_ids=(1,),
            )

    def test_rejects_bool_as_int_and_nonbuiltin_scalars(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            _row(1, candidate_ids=(True,))  # type: ignore[arg-type]
        with self.assertRaises((TypeError, ValueError)):
            _row(1, candidate_ids=(1.5,))  # pyright: ignore[reportArgumentType]

    def test_evidence_privately_snapshots_contract(self) -> None:
        evidence = _small_evidence()
        self.assertEqual(evidence.contract, _CONTRACT)
        self.assertEqual(evidence.contract_digest, benchmark_contract_digest(_CONTRACT))


class OraclePayloadTests(unittest.TestCase):
    def _payload(self) -> dict[str, object]:
        return _evidence_payload(_small_evidence())

    def test_payload_round_trips(self) -> None:
        evidence = _small_evidence()
        payload = _evidence_payload(evidence)
        reconstructed = evidence_from_payload(payload)
        self.assertEqual(
            reconstructed.evidence_digest,
            evidence.evidence_digest,
        )
        self.assertEqual(
            oracle_evidence_digest(reconstructed),
            evidence.evidence_digest,
        )
        self.assertEqual(
            evidence_from_json(evidence_to_json(evidence)).evidence_digest,
            evidence.evidence_digest,
        )

    def test_rejects_unknown_and_missing_fields(self) -> None:
        payload = self._payload()
        payload["unknown_field"] = 1
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)
        payload = self._payload()
        del payload["rows"]
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)

    def test_rejects_duplicate_json_keys(self) -> None:
        payload = self._payload()
        raw = json.dumps(payload, sort_keys=True)
        duplicated = raw.replace(
            '"schema_version"',
            '"schema_version","schema_version"',
            1,
        )
        with self.assertRaises(ValueError):
            _parse_strict_json(duplicated)

    def test_rejects_non_finite_json_number(self) -> None:
        payload = self._payload()
        raw = json.dumps(payload, sort_keys=True)
        non_finite = raw.replace('"query_count": 2', '"query_count": NaN', 1)
        with self.assertRaises(ValueError):
            _parse_strict_json(non_finite)

    def test_rejects_bool_for_int_field_in_payload(self) -> None:
        payload = self._payload()
        rows = payload["rows"]
        assert isinstance(rows, list)
        first = rows[0]
        assert isinstance(first, dict)
        first["candidate_count"] = True
        with self.assertRaises((TypeError, ValueError)):
            evidence_from_payload(payload)

    def test_forged_evidence_digest_is_rejected(self) -> None:
        payload = self._payload()
        payload["evidence_digest"] = "f" * 64
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)

    def test_forged_binding_is_rejected(self) -> None:
        payload = self._payload()
        payload["contract_digest"] = "c" * 64
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)
        payload = self._payload()
        payload["path_config_digest"] = _CONTRACT.fallback_path_config_digest
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)
        payload = self._payload()
        payload["environment_digest"] = "e" * 64
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)

    def test_forged_row_ids_are_rejected(self) -> None:
        payload = self._payload()
        rows = payload["rows"]
        assert isinstance(rows, list)
        first = rows[0]
        assert isinstance(first, dict)
        first["above_threshold_ids"] = [3, 2, 1]
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)
        payload = self._payload()
        rows = payload["rows"]
        assert isinstance(rows, list)
        first = rows[0]
        assert isinstance(first, dict)
        first["candidate_ids"] = [1, 1]
        with self.assertRaises(ValueError):
            evidence_from_payload(payload)

    def test_payload_carries_no_query_source_or_record_bodies(self) -> None:
        raw = evidence_to_json(_small_evidence())
        self.assertNotIn("query_raw", raw)
        self.assertNotIn("source_raw", raw)
        self.assertNotIn("target_raw", raw)


class FullScanOracleTests(unittest.TestCase):
    def _padded(
        self,
        records: tuple[BenchmarkRecord, ...],
    ) -> tuple[BenchmarkRecord, ...]:
        used_ids = {record.record_id for record in records}
        padding = [
            _make_record(record_id, "xxxx")
            for record_id in range(1, 13)
            if record_id not in used_ids
        ]
        combined = records + tuple(padding)
        return tuple(
            sorted(combined, key=lambda record: record.record_id)
        )

    def test_threshold_and_top10_are_distinct_obligations(self) -> None:
        records = self._padded(
            (
                _make_record(1, "aabba"),
                _make_record(2, "bbaab"),
                _make_record(3, "zzzzzzzz"),
                _make_record(4, "aabba"),
            )
        )
        queries = (
            _make_query(
                1,
                "aabba",
                category="exact",
                reference_record_id=1,
            ),
        )
        oracle = compute_full_scan_oracle(
            contract=_CONTRACT,
            records=records,
            queries=queries,
        )
        self.assertEqual(len(oracle), 1)
        row = oracle[0]
        # records 1 and 4 are exact 1.0; record 2 scores exactly 0.60 and is
        # above threshold; padded records and record 3 are below threshold.
        self.assertEqual(row.above_threshold_ids, (1, 2, 4))
        # true top-10 over ALL records: 1.0 ids 4,1 then 0.6 id 2, then the
        # 0.25 padded group by record id descending, then the 0.0 record.
        self.assertEqual(row.top10_ids, (4, 1, 2, 12, 11, 10, 9, 8, 7, 6))

    def test_tie_ordering_is_final_desc_then_record_id_desc(self) -> None:
        records = self._padded(
            (
                _make_record(5, "bbaab"),
                _make_record(7, "bbaab"),
                _make_record(3, "aabba"),
            )
        )
        queries = (
            _make_query(
                1,
                "aabba",
                category="exact",
                reference_record_id=3,
            ),
        )
        row = compute_full_scan_oracle(
            contract=_CONTRACT,
            records=records,
            queries=queries,
        )[0]
        # 3 is 1.0; 7 and 5 tie at 0.60 -> record id descending
        self.assertEqual(row.top10_ids[:3], (3, 7, 5))

    def test_miss_query_still_has_both_obligations(self) -> None:
        records = tuple(
            _make_record(record_id, "zzzzz")
            for record_id in range(1, 11)
        )
        queries = (
            _make_query(
                1,
                "qqqqq",
                category="miss",
                reference_record_id=None,
            ),
        )
        row = compute_full_scan_oracle(
            contract=_CONTRACT,
            records=records,
            queries=queries,
        )[0]
        # no record is above 0.60 for the miss query
        self.assertEqual(row.above_threshold_ids, ())
        # the top-10 obligation is still the full ordered top-10 (ties break
        # by record id descending)
        self.assertEqual(row.top10_ids, tuple(range(10, 0, -1)))

    def test_threshold_boundary_is_inclusive(self) -> None:
        records = self._padded(
            (
                _make_record(1, "bbaab"),
                _make_record(2, "zzzz"),
            )
        )
        queries = (
            _make_query(
                1,
                "aabba",
                category="near-edit",
                reference_record_id=1,
            ),
        )
        final = _SCORER.score("aabba", "bbaab").final_similarity
        self.assertEqual(final, 0.6)
        row = compute_full_scan_oracle(
            contract=_CONTRACT,
            records=records,
            queries=queries,
        )[0]
        self.assertEqual(row.above_threshold_ids, (1,))

    def test_rejects_wrong_threshold_or_top_k(self) -> None:
        records = self._padded((_make_record(1, "aabba"),))
        queries = (
            _make_query(1, "aabba", category="exact", reference_record_id=1),
        )
        with self.assertRaises(ValueError):
            compute_full_scan_oracle(
                contract=_CONTRACT,
                records=records,
                queries=queries,
                minimum_similarity=0.5,
            )
        with self.assertRaises(ValueError):
            compute_full_scan_oracle(
                contract=_CONTRACT,
                records=records,
                queries=queries,
                top_k=5,
            )

    def test_rejects_non_ascending_ids_and_reference_outside_subset(self) -> None:
        padded = self._padded(())
        with self.assertRaises(ValueError):
            compute_full_scan_oracle(
                contract=_CONTRACT,
                records=tuple(reversed(padded)),
                queries=(
                    _make_query(
                        1,
                        "aabba",
                        category="exact",
                        reference_record_id=1,
                    ),
                ),
            )
        with self.assertRaises(ValueError):
            compute_full_scan_oracle(
                contract=_CONTRACT,
                records=padded,
                queries=(
                    _make_query(
                        1,
                        "aabba",
                        category="exact",
                        reference_record_id=13,
                    ),
                ),
            )


def _tm_record(
    record: BenchmarkRecord,
    *,
    record_id: int,
    ordinal: int,
) -> Any:
    from tm_contracts import TMRecord

    return TMRecord(
        record_id=record_id,
        source_raw=record.source_raw,
        target_raw=record.target_raw,
        speaker_raw=record.speaker_raw,
        context_prev_raw=record.context_prev_raw,
        context_next_raw=record.context_next_raw,
        file_source=record.file_source,
        provenance=(("source", "legacy-jsonl"),),
        legacy_line_no=record_id,
        origin_batch_id="migration.test",
        origin_ordinal=ordinal,
    )


class PhysicalMappingTests(unittest.TestCase):
    def _expected_records(self) -> tuple[BenchmarkRecord, ...]:
        return tuple(_make_record(i, f"source-{i}") for i in (1, 2, 3))

    def test_valid_mapping_is_closed_one_to_one(self) -> None:
        from tm_benchmark_oracle import _validate_physical_mapping

        records = self._expected_records()

        class StubStore:
            def records_by_id(self, record_ids: tuple[int, ...]) -> tuple[Any, ...]:
                return tuple(
                    _tm_record(record, record_id=record_id, ordinal=record_id - 1)
                    for record_id, record in zip(record_ids, records, strict=True)
                )

        mapping = _validate_physical_mapping(StubStore(), records)
        self.assertEqual(mapping, {1: 1, 2: 2, 3: 3})

    def test_rejects_missing_and_extra_read_back(self) -> None:
        from tm_benchmark_oracle import _validate_physical_mapping

        records = self._expected_records()

        class MissingStore:
            def records_by_id(self, record_ids: tuple[int, ...]) -> tuple[Any, ...]:
                return (_tm_record(records[0], record_id=1, ordinal=0),)

        with self.assertRaises(ValueError):
            _validate_physical_mapping(MissingStore(), records)

    def test_rejects_duplicate_physical_ids(self) -> None:
        from tm_benchmark_oracle import _validate_physical_mapping

        records = self._expected_records()
        duplicate = _tm_record(records[0], record_id=1, ordinal=0)

        class DuplicateStore:
            def records_by_id(self, record_ids: tuple[int, ...]) -> tuple[Any, ...]:
                return (duplicate, duplicate, _tm_record(records[2], record_id=3, ordinal=2))

        with self.assertRaises(ValueError):
            _validate_physical_mapping(DuplicateStore(), records)

    def test_rejects_reordering_ordinal_drift(self) -> None:
        from tm_benchmark_oracle import _validate_physical_mapping

        records = self._expected_records()

        class ReorderedStore:
            def records_by_id(self, record_ids: tuple[int, ...]) -> tuple[Any, ...]:
                return tuple(
                    _tm_record(record, record_id=record_id, ordinal=(record_id + 1) % 3)
                    for record_id, record in zip(record_ids, records, strict=True)
                )

        with self.assertRaises(ValueError):
            _validate_physical_mapping(ReorderedStore(), records)

    def test_rejects_body_drift(self) -> None:
        from tm_benchmark_oracle import _validate_physical_mapping

        records = self._expected_records()

        class BodyDriftStore:
            def records_by_id(self, record_ids: tuple[int, ...]) -> tuple[Any, ...]:
                drifted = _tm_record(records[0], record_id=1, ordinal=0)
                from tm_contracts import TMRecord

                drifted = TMRecord(
                    record_id=1,
                    source_raw="other-source",
                    target_raw=drifted.target_raw,
                    speaker_raw=drifted.speaker_raw,
                    context_prev_raw=drifted.context_prev_raw,
                    context_next_raw=drifted.context_next_raw,
                    file_source=drifted.file_source,
                    provenance=drifted.provenance,
                    legacy_line_no=1,
                    origin_batch_id=drifted.origin_batch_id,
                    origin_ordinal=0,
                )
                return (
                    drifted,
                    _tm_record(records[1], record_id=2, ordinal=1),
                    _tm_record(records[2], record_id=3, ordinal=2),
                )

        with self.assertRaises(ValueError):
            _validate_physical_mapping(BodyDriftStore(), records)


class OracleRunnerTests(unittest.TestCase):
    def _run_mini(
        self,
        execution_path: BenchmarkExecutionPath,
        *,
        record_count: int = 200,
        query_count: int = 40,
    ) -> OracleRecallEvidence:
        with tempfile.TemporaryDirectory() as temporary:
            return run_oracle_recall_evidence(
                contract=_CONTRACT,
                execution_path=execution_path,
                run_root=Path(temporary),
                test_mode=True,
                test_record_count=record_count,
                test_query_count=query_count,
            )

    def test_test_mode_requires_explicit_small_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                run_oracle_recall_evidence(
                    contract=_CONTRACT,
                    execution_path=_FTS5,
                    run_root=Path(temporary),
                    test_mode=True,
                )
            with self.assertRaises(ValueError):
                run_oracle_recall_evidence(
                    contract=_CONTRACT,
                    execution_path=_FTS5,
                    run_root=Path(temporary),
                    test_mode=True,
                    test_record_count=5000,
                    test_query_count=200,
                )

    def test_run_root_must_be_closed_before_fixture_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "foreign").write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                run_oracle_recall_evidence(
                    contract=_CONTRACT,
                    execution_path=_FTS5,
                    run_root=root,
                    test_mode=True,
                    test_record_count=200,
                    test_query_count=40,
                )

    def test_full_scan_and_candidate_share_one_source_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(
                    tm_benchmark_oracle,
                    "benchmark_implementation_fingerprint",
                    side_effect=("a" * 64, "b" * 64),
                ),
                patch.object(
                    tm_benchmark_oracle,
                    "compute_full_scan_oracle",
                    return_value=(),
                ),
                patch.object(
                    tm_benchmark_oracle,
                    "_run_candidate_path",
                    side_effect=AssertionError(
                        "candidate path must not run after source drift"
                    ),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "before candidate"):
                    run_oracle_recall_evidence(
                        contract=_CONTRACT,
                        execution_path=_FTS5,
                        run_root=Path(temporary),
                        test_mode=True,
                        test_record_count=10,
                        test_query_count=1,
                    )

    def test_fts5_path_real_store_integration(self) -> None:
        evidence = self._run_mini(_FTS5)
        self.assertEqual(evidence.store_index_kind, "FTS5_TRIGRAM")
        self.assertEqual(dict(evidence.environment)["fts5_enabled"], "true")
        self.assertEqual(evidence.query_count, 40)
        self.assertEqual(len(evidence.rows), 40)
        self.assertTrue(
            all(row.actual_index_kind == "FTS5_TRIGRAM" for row in evidence.rows)
        )
        self.assertEqual(evidence.index_kind_drift_count, 0)
        self.assertEqual(evidence.result_limit, 10)
        self.assertEqual(evidence.candidate_budget, candidate_budget_v1(10))
        self.assertFalse(evidence.final_evidence)
        self.assertEqual(
            evidence.recompute_evidence_digest(),
            evidence.evidence_digest,
        )

    def test_gram_fallback_path_real_store_integration(self) -> None:
        evidence = self._run_mini(_FALLBACK)
        self.assertEqual(evidence.store_index_kind, "GRAM_FALLBACK")
        self.assertEqual(dict(evidence.environment)["fts5_enabled"], "false")
        self.assertEqual(evidence.query_count, 40)
        self.assertTrue(
            all(row.actual_index_kind == "GRAM_FALLBACK" for row in evidence.rows)
        )
        self.assertEqual(evidence.index_kind_drift_count, 0)
        self.assertEqual(
            evidence.recompute_evidence_digest(),
            evidence.evidence_digest,
        )

    def test_candidate_ids_translate_to_original_identity(self) -> None:
        evidence = self._run_mini(_FTS5)
        original_ids = {
            record.record_id
            for record in iter_oracle_subset_records(
                seed=_CONTRACT.corpus_seed,
                record_count=_CONTRACT.corpus_record_count,
                subset_count=200,
            )
        }
        for row in evidence.rows:
            for candidate_id in row.candidate_ids:
                self.assertIn(candidate_id, original_ids)
        reference_ids = {
            query.reference_record_id
            for query in iter_oracle_queries(
                seed=_CONTRACT.corpus_seed,
                record_count=_CONTRACT.corpus_record_count,
                subset_count=200,
                query_count=40,
            )
            if query.reference_record_id is not None
        }
        for row in evidence.rows:
            if row.category in ("exact", "near-edit"):
                self.assertIn(row.reference_record_id, reference_ids)
                self.assertIn(row.reference_record_id, row.above_threshold_ids)
                self.assertNotIn(
                    row.reference_record_id,
                    row.missing_above_threshold_ids,
                )
                if row.reference_record_id in row.top10_ids:
                    self.assertNotIn(
                        row.reference_record_id,
                        row.missing_top10_ids,
                    )

    def test_precomputed_oracle_path_matches_internal_compute(self) -> None:
        records = tuple(
            iter_oracle_subset_records(
                seed=_CONTRACT.corpus_seed,
                record_count=_CONTRACT.corpus_record_count,
                subset_count=200,
            )
        )
        queries = tuple(
            iter_oracle_queries(
                seed=_CONTRACT.corpus_seed,
                record_count=_CONTRACT.corpus_record_count,
                subset_count=200,
                query_count=40,
            )
        )
        oracle = compute_full_scan_oracle(
            contract=_CONTRACT,
            records=records,
            queries=queries,
        )
        with tempfile.TemporaryDirectory() as internal_root:
            internal = run_oracle_recall_evidence(
                contract=_CONTRACT,
                execution_path=_FTS5,
                run_root=Path(internal_root),
                test_mode=True,
                test_record_count=200,
                test_query_count=40,
            )
        with tempfile.TemporaryDirectory() as passed_root:
            passed = run_oracle_recall_evidence(
                contract=_CONTRACT,
                execution_path=_FTS5,
                run_root=Path(passed_root),
                test_mode=True,
                test_record_count=200,
                test_query_count=40,
                oracle=oracle,
            )
        self.assertTrue(passed.recall_passed)
        self.assertEqual(
            passed.missing_above_threshold_total,
            internal.missing_above_threshold_total,
        )
        self.assertEqual(
            passed.missing_top10_total,
            internal.missing_top10_total,
        )
        self.assertEqual(
            passed.recompute_evidence_digest(),
            passed.evidence_digest,
        )

    def test_real_mode_rejects_caller_supplied_oracle(self) -> None:
        forged = (
            FullScanQueryOracle(
                query_id=1,
                category="exact",
                reference_record_id=1,
                above_threshold_ids=(),
                top10_ids=tuple(range(1, 11)),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                ValueError,
                "derive full-scan oracle inside its owner",
            ):
                run_oracle_recall_evidence(
                    contract=_CONTRACT,
                    execution_path=_FTS5,
                    run_root=Path(temporary),
                    oracle=forged,
                )

    def test_fts5_absence_is_explicit_unavailable_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch("tm_benchmark_oracle._probe_fts5", return_value=False):
                with self.assertRaises(OraclePathUnavailableError) as caught:
                    run_oracle_recall_evidence(
                        contract=_CONTRACT,
                        execution_path=_FTS5,
                        run_root=Path(temporary),
                        test_mode=True,
                        test_record_count=200,
                        test_query_count=40,
                    )
            self.assertEqual(caught.exception.code, "ORACLE.FTS5_UNAVAILABLE")

    def test_literal_frozen_5000_200_evidence_run(self) -> None:
        """Fresh real frozen literal 5000/200 evidence run on both paths."""
        recompute_benchmark_inputs(_ROOT / "benchmark_tm_contract.json")
        started = time.perf_counter()
        with (
            tempfile.TemporaryDirectory() as fts_root,
            tempfile.TemporaryDirectory() as fallback_root,
        ):
            fts_evidence, fallback_evidence = run_oracle_recall_suite(
                contract=_CONTRACT,
                fts5_run_root=Path(fts_root),
                fallback_run_root=Path(fallback_root),
            )
        elapsed_seconds = time.perf_counter() - started
        for evidence in (fts_evidence, fallback_evidence):
            self.assertFalse(evidence.test_mode)
            self.assertTrue(evidence.final_evidence)
            self.assertEqual(evidence.oracle_subset_record_count, 5000)
            self.assertEqual(evidence.oracle_query_count, 200)
            self.assertEqual(
                evidence.oracle_subset_digest,
                _CONTRACT.oracle_subset_digest,
            )
            self.assertEqual(evidence.result_limit, 10)
            self.assertEqual(evidence.candidate_budget, candidate_budget_v1(10))
            self.assertEqual(
                evidence.recompute_evidence_digest(),
                evidence.evidence_digest,
            )
            self.assertEqual(
                evidence.recompute_environment_digest(),
                evidence.environment_digest,
            )
            self.assertEqual(len(evidence.rows), 200)
            self.assertEqual(evidence.index_kind_drift_count, 0)
        self.assertEqual(fts_evidence.store_index_kind, "FTS5_TRIGRAM")
        self.assertEqual(fallback_evidence.store_index_kind, "GRAM_FALLBACK")
        self.assertEqual(
            dict(fts_evidence.environment)["fts5_enabled"],
            "true",
        )
        self.assertEqual(
            dict(fallback_evidence.environment)["fts5_enabled"],
            "false",
        )
        # Honest facts: report the literal outcomes for the record.
        print(
            "literal 5000/200: fts5 missing_above="
            f"{fts_evidence.missing_above_threshold_total} "
            f"missing_top10={fts_evidence.missing_top10_total} "
            f"recall_passed={fts_evidence.recall_passed} | "
            "fallback missing_above="
            f"{fallback_evidence.missing_above_threshold_total} "
            f"missing_top10={fallback_evidence.missing_top10_total} "
            f"recall_passed={fallback_evidence.recall_passed} | "
            f"elapsed={elapsed_seconds:.0f}s"
        )


class ModuleBoundaryTests(unittest.TestCase):
    def test_runtime_modules_never_import_oracle_owner(self) -> None:
        for module_name in sorted(_BANNED_RUNTIME_MODULES):
            with self.subTest(module_name=module_name):
                source_path = _ROOT / f"{module_name}.py"
                if not source_path.is_file():
                    continue
                source = source_path.read_text(encoding="utf-8")
                for match in _IMPORT_RE.finditer(source):
                    self.assertNotEqual(
                        match.group(1).split(".")[0],
                        "tm_benchmark_oracle",
                        f"{module_name} imports the benchmark oracle owner",
                    )

    def test_importing_runtime_modules_loads_no_oracle_owner(self) -> None:
        banned = ", ".join(repr(name) for name in sorted(_BANNED_RUNTIME_MODULES))
        code = (
            "import sys\n"
            f"modules = [{banned}]\n"
            "for name in modules:\n"
            "    __import__(name)\n"
            "loaded = {m.split('.')[0] for m in sys.modules}\n"
            "assert 'tm_benchmark_oracle' not in loaded, sorted(loaded)\n"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_oracle_owner_imports_only_frozen_seams(self) -> None:
        from tm_benchmark_oracle import __file__ as owner_file

        source = Path(owner_file).read_text(encoding="utf-8")
        imported = {
            match.group(1).split(".")[0]
            for match in _IMPORT_RE.finditer(source)
        }
        self.assertTrue(imported)
        stdlib = set(sys.stdlib_module_names)
        allowed = {
            "tm_benchmark",
            "tm_candidate_index",
            "tm_contracts",
            "tm_migration",
            "tm_retrieval",
            "tm_similarity",
            "tm_sqlite_store",
            "tm_stage_sealer",
            "text_matcher",
        }
        for module in sorted(imported):
            self.assertTrue(
                module in stdlib or module in allowed,
                f"unexpected import: {module}",
            )
        forbidden_prefixes = (
            "qt_",
            "glossary",
            "parser",
            "matcher_capability",
            "matcher_validation",
            "tm_retrieval_capability",
            "tm_retrieval_validation",
            "requests",
            "urllib",
            "socket",
            "http",
        )
        for module in sorted(imported):
            self.assertFalse(
                module.startswith(forbidden_prefixes),
                f"forbidden import: {module}",
            )

    def test_no_benchmark_report_or_capability_construction(self) -> None:
        from tm_benchmark_oracle import __file__ as owner_file

        source = Path(owner_file).read_text(encoding="utf-8")
        for name in (
            "BenchmarkReport",
            "BenchmarkSuiteReport",
            "BenchmarkSuiteContract",
            "RetrievalCapability",
            "TextMatcherCapability",
            "GateD",
        ):
            self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
