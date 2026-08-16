"""Focused tests for the deterministic benchmark-v1 corpus/cohort/oracle owner."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tm_benchmark
from tm_benchmark import (
    TM_BENCHMARK_COMPOSITION_VERSION,
    TM_BENCHMARK_CORPUS_RECORD_COUNT,
    TM_BENCHMARK_CORPUS_VERSION,
    TM_BENCHMARK_DEFAULT_SEED,
    TM_BENCHMARK_DIGEST_SCHEMA,
    TM_BENCHMARK_EXACT_COHORT_COUNT,
    TM_BENCHMARK_FUZZY_COHORT_COUNT,
    TM_BENCHMARK_MINIMUM_SIMILARITY,
    TM_BENCHMARK_MISS_PREFIX,
    TM_BENCHMARK_ORACLE_QUERY_COUNT,
    TM_BENCHMARK_ORACLE_SUBSET_RECORD_COUNT,
    TM_BENCHMARK_TOP_K,
    BenchmarkInputPlan,
    BenchmarkQuery,
    BenchmarkRecord,
    compute_benchmark_contract,
    compute_benchmark_input_plan,
    iter_corpus_records,
    iter_exact_queries,
    iter_fuzzy_queries,
    iter_oracle_queries,
    iter_oracle_subset_records,
    load_benchmark_contract,
    recompute_benchmark_inputs,
)
from typing import Any, cast

from tm_contracts import (
    BENCHMARK_CONTRACT_VERSION,
    CANDIDATE_BUDGET_VERSION,
    BenchmarkContract,
    contract_from_json,
    contract_to_json,
)

_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_PATH = _ROOT / "benchmark_tm_contract.json"

_NEAR_EDIT_START = 13_001
_NEAR_EDIT_END = 15_000

_SCRIPT_PATTERNS = (
    ("latin_extended", re.compile(r"[\u00C0-\u024F]")),
    ("cyrillic", re.compile(r"[\u0400-\u04FF]")),
    ("han", re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")),
    ("hiragana", re.compile(r"[\u3040-\u309F]")),
    ("katakana", re.compile(r"[\u30A0-\u30FF]")),
    ("hangul", re.compile(r"[\uAC00-\uD7AF]")),
)
_CJK_PATTERN = re.compile(
    r"[\u3400-\u4DBF\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF\uF900-\uFAFF]"
)
_IMPORT_RE = re.compile(
    r"^(?:import|from)\s+([A-Za-z0-9_\.]+)",
    re.MULTILINE,
)

_RUNTIME_MODULES = (
    "tm_contracts.py",
    "tm_similarity.py",
    "text_matcher.py",
    "tm_engine.py",
    "tm_candidate_index.py",
    "tm_retrieval.py",
    "tm_retrieval_capability.py",
    "tm_retrieval_validation.py",
    "tm_sqlite_store.py",
    "tm_migration.py",
    "tm_snapshot_artifacts.py",
    "tm_snapshot_recovery.py",
    "tm_gate_a.py",
    "tm_gate_b.py",
    "matcher_capability.py",
    "matcher_validation.py",
    "tm_stage_sealer.py",
    "tm_schema_upgrade.py",
    "tm_activation_journal.py",
    "tm_activation_recovery.py",
    "tm_json_importer.py",
)
_BANNED_RUNTIME_MODULES = {
    "tm_sqlite_store",
    "tm_retrieval",
    "tm_migration",
    "tm_candidate_index",
    "tm_engine",
    "qt_editor",
    "deterministic_workload",
    "validate_benchmark_contract",
    "stress_runner",
    "backend_throughput_harness",
    "backend_scaling_gate",
    "tm_similarity",
    "text_matcher",
    "matcher_capability",
    "matcher_validation",
}


class BenchmarkImplementationFingerprintTests(unittest.TestCase):
    def test_two_pass_snapshot_rejects_earlier_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with (
                patch.object(
                    tm_benchmark,
                    "BENCHMARK_IMPLEMENTATION_SOURCE_PATHS",
                    ("a.py", "b.py"),
                ),
                patch.object(
                    tm_benchmark,
                    "_stable_benchmark_source_digest",
                    side_effect=("a" * 64, "b" * 64, "c" * 64, "b" * 64),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "during snapshot"):
                    tm_benchmark.benchmark_implementation_fingerprint(root)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _independent_digest(
    generator_version: str,
    kind: str,
    items: Iterator[dict[str, object]],
) -> str:
    """Independent reimplementation of the documented canonical digest framing."""
    hasher = hashlib.sha256()
    header = _canonical_json(
        {
            "digest_schema": TM_BENCHMARK_DIGEST_SCHEMA,
            "kind": kind,
            "generator_version": generator_version,
        }
    )
    hasher.update(header.encode("utf-8"))
    hasher.update(b"\n")
    for item in items:
        hasher.update(_canonical_json(item).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _record_item(record: BenchmarkRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "source_raw": record.source_raw,
        "target_raw": record.target_raw,
        "language": record.language,
        "speaker_raw": record.speaker_raw,
        "context_prev_raw": record.context_prev_raw,
        "context_next_raw": record.context_next_raw,
        "file_source": record.file_source,
        "legacy_line_no": record.legacy_line_no,
        "origin_batch_id": record.origin_batch_id,
        "origin_ordinal": record.origin_ordinal,
        "provenance": [[key, value] for key, value in record.provenance],
    }


def _query_item(query: BenchmarkQuery) -> dict[str, object]:
    return {
        "query_id": query.query_id,
        "query_raw": query.query_raw,
        "cohort": query.cohort,
        "category": query.category,
        "reference_record_id": query.reference_record_id,
    }


def _is_near_edit_base(record_id: int) -> bool:
    return (
        _NEAR_EDIT_START <= record_id <= _NEAR_EDIT_END
        and (record_id - _NEAR_EDIT_START) % 2 == 0
    )


@dataclass(frozen=True)
class _CorpusPass:
    record_count: int
    ids: tuple[int, ...]
    sources: dict[int, str]
    language_counts: dict[str, int]
    cjk_count: int
    short_count: int
    context_count: int
    duplicate_source_count: int
    multi_target_count: int
    near_edit_pairs_ok: int
    near_edit_pairs_broken: int
    miss_marker_absent: bool
    scripts: frozenset[str]
    corpus_digest: str


_full_pass_cache: dict[tuple[int, int], _CorpusPass] = {}


def _full_pass(
    seed: int = TM_BENCHMARK_DEFAULT_SEED,
    record_count: int = TM_BENCHMARK_CORPUS_RECORD_COUNT,
) -> _CorpusPass:
    """One full independent pass: facts + corpus digest, memoized per params."""
    key = (seed, record_count)
    cached = _full_pass_cache.get(key)
    if cached is not None:
        return cached
    hasher = hashlib.sha256()
    hasher.update(
        _canonical_json(
            {
                "digest_schema": TM_BENCHMARK_DIGEST_SCHEMA,
                "kind": "corpus",
                "generator_version": TM_BENCHMARK_CORPUS_VERSION,
            }
        ).encode("utf-8")
    )
    hasher.update(b"\n")
    ids: list[int] = []
    sources: dict[int, str] = {}
    language_counts: dict[str, int] = {}
    cjk_count = 0
    short_count = 0
    context_count = 0
    near_edit_pairs_ok = 0
    near_edit_pairs_broken = 0
    scripts: set[str] = set()
    miss_absent = True
    groups: dict[str, tuple[int, set[str]]] = {}
    previous: BenchmarkRecord | None = None
    for record in iter_corpus_records(seed=seed, record_count=record_count):
        ids.append(record.record_id)
        sources[record.record_id] = record.source_raw
        hasher.update(_canonical_json(_record_item(record)).encode("utf-8"))
        hasher.update(b"\n")
        language_counts[record.language] = (
            language_counts.get(record.language, 0) + 1
        )
        if _CJK_PATTERN.search(record.source_raw) or _CJK_PATTERN.search(
            record.target_raw
        ):
            cjk_count += 1
        if len(record.source_raw) <= 12 and len(record.target_raw) <= 12:
            short_count += 1
        if (
            record.speaker_raw is not None
            or record.context_prev_raw is not None
            or record.context_next_raw is not None
        ):
            context_count += 1
        if TM_BENCHMARK_MISS_PREFIX in record.source_raw or (
            TM_BENCHMARK_MISS_PREFIX in record.target_raw
        ):
            miss_absent = False
        for script_name, pattern in _SCRIPT_PATTERNS:
            if pattern.search(record.source_raw) or pattern.search(
                record.target_raw
            ):
                scripts.add(script_name)
        entry = groups.get(record.source_raw)
        if entry is None:
            groups[record.source_raw] = (1, {record.target_raw})
        else:
            groups[record.source_raw] = (
                entry[0] + 1,
                entry[1] | {record.target_raw},
            )
        if (
            previous is not None
            and _is_near_edit_base(previous.record_id)
            and record.record_id == previous.record_id + 1
        ):
            first = previous.source_raw
            second = record.source_raw
            if len(first) == len(second) and sum(
                a != b for a, b in zip(first, second)
            ) == 1:
                near_edit_pairs_ok += 1
            else:
                near_edit_pairs_broken += 1
        previous = record
    duplicate_source_count = sum(
        count
        for count, _targets in groups.values()
        if count >= 2
    )
    multi_target_count = sum(
        count
        for count, targets in groups.values()
        if len(targets) >= 2
    )
    result = _CorpusPass(
        record_count=record_count,
        ids=tuple(ids),
        sources=sources,
        language_counts=language_counts,
        cjk_count=cjk_count,
        short_count=short_count,
        context_count=context_count,
        duplicate_source_count=duplicate_source_count,
        multi_target_count=multi_target_count,
        near_edit_pairs_ok=near_edit_pairs_ok,
        near_edit_pairs_broken=near_edit_pairs_broken,
        miss_marker_absent=miss_absent,
        scripts=frozenset(scripts),
        corpus_digest=hasher.hexdigest(),
    )
    _full_pass_cache[key] = result
    return result


_plan_cache: dict[tuple[tuple[str, int | str], ...], BenchmarkInputPlan] = {}


def _plan(**overrides: int | str) -> BenchmarkInputPlan:
    key = tuple(sorted(overrides.items()))
    cached = _plan_cache.get(key)
    if cached is not None:
        return cached
    plan = compute_benchmark_input_plan(
        **cast(dict[str, Any], overrides)
    )
    _plan_cache[key] = plan
    return plan


def _composition_item(
    plan: BenchmarkInputPlan,
    pass_result: _CorpusPass,
) -> dict[str, object]:
    return {
        "composition_version": plan.composition_version,
        "generator_version": plan.generator_version,
        "seed": plan.seed,
        "record_count": plan.record_count,
        "language_counts": dict(sorted(pass_result.language_counts.items())),
        "cjk_count": pass_result.cjk_count,
        "short_count": pass_result.short_count,
        "duplicate_source_count": pass_result.duplicate_source_count,
        "multi_target_count": pass_result.multi_target_count,
        "context_count": pass_result.context_count,
        "near_edit_record_pairs": pass_result.near_edit_pairs_ok,
        "exact_cohort_count": plan.exact_cohort_count,
        "fuzzy_cohort_count": plan.fuzzy_cohort_count,
        "fuzzy_near_edit_count": plan.fuzzy_near_edit_count,
        "fuzzy_miss_count": plan.fuzzy_miss_count,
        "oracle_subset_record_count": plan.oracle_subset_record_count,
        "oracle_query_count": plan.oracle_query_count,
        "oracle_exact_query_count": plan.oracle_exact_query_count,
        "oracle_near_edit_query_count": plan.oracle_near_edit_query_count,
        "oracle_miss_query_count": plan.oracle_miss_query_count,
    }


def _replace_payload_key(text: str, key: str, replacement: str) -> str:
    marker = f'"{key}":'
    position = text.index(marker)
    end = text.index(",", position)
    return text[:position] + replacement + text[end:]


class BenchmarkContractJsonTests(unittest.TestCase):
    def test_contract_file_strict_loads_and_matches_generator(self) -> None:
        text = _CONTRACT_PATH.read_text(encoding="utf-8")
        loaded = contract_from_json(text)
        expected = compute_benchmark_contract()
        self.assertIsInstance(loaded, BenchmarkContract)
        self.assertEqual(loaded, expected)
        self.assertEqual(contract_to_json(loaded), text)
        self.assertEqual(
            load_benchmark_contract(_CONTRACT_PATH),
            expected,
        )

    def test_contract_carries_exact_fixed_constants_and_generator_digests(
        self,
    ) -> None:
        loaded = load_benchmark_contract(_CONTRACT_PATH)
        plan = _plan()
        self.assertEqual(loaded.contract_version, BENCHMARK_CONTRACT_VERSION)
        self.assertEqual(
            loaded.corpus_generator_version,
            TM_BENCHMARK_CORPUS_VERSION,
        )
        self.assertEqual(loaded.corpus_seed, 20260729)
        self.assertEqual(loaded.corpus_record_count, 100_000)
        self.assertEqual(
            loaded.corpus_composition_version,
            TM_BENCHMARK_COMPOSITION_VERSION,
        )
        self.assertEqual(loaded.exact_min_samples, 1_000)
        self.assertEqual(loaded.exact_cohort_count, 1_200)
        self.assertEqual(loaded.fuzzy_min_samples, 200)
        self.assertEqual(loaded.fuzzy_cohort_count, 240)
        self.assertEqual(loaded.oracle_subset_record_count, 5_000)
        self.assertEqual(loaded.oracle_query_count, 200)
        self.assertEqual(loaded.top_k, 10)
        self.assertEqual(loaded.minimum_similarity, 0.60)
        self.assertEqual(loaded.warmup_queries_per_cohort, 100)
        self.assertEqual(loaded.measured_repeats, 1)
        self.assertEqual(loaded.percentile_method, "nearest-rank")
        self.assertEqual(loaded.rss_scope, "child-process-lifetime-v1")
        self.assertEqual(
            loaded.candidate_budget_version,
            CANDIDATE_BUDGET_VERSION,
        )
        self.assertEqual(loaded.corpus_digest, plan.corpus_digest)
        self.assertEqual(
            loaded.corpus_composition_digest,
            plan.corpus_composition_digest,
        )
        self.assertEqual(loaded.exact_cohort_digest, plan.exact_cohort_digest)
        self.assertEqual(loaded.fuzzy_cohort_digest, plan.fuzzy_cohort_digest)
        self.assertEqual(
            loaded.oracle_subset_digest,
            plan.oracle_subset_digest,
        )
        self.assertEqual(
            loaded.scorer_config_digest,
            plan.scorer_config_digest,
        )
        self.assertEqual(
            loaded.fast_path_config_digest,
            plan.fast_path_config_digest,
        )
        self.assertEqual(
            loaded.fallback_path_config_digest,
            plan.fallback_path_config_digest,
        )
        for digest in (
            plan.corpus_digest,
            plan.corpus_composition_digest,
            plan.exact_cohort_digest,
            plan.fuzzy_cohort_digest,
            plan.oracle_subset_digest,
        ):
            self.assertNotEqual(digest, "a" * 64)

    def test_recompute_twice_is_identical_and_matches_contract(self) -> None:
        first = recompute_benchmark_inputs(_CONTRACT_PATH)
        second = recompute_benchmark_inputs(_CONTRACT_PATH)
        self.assertEqual(first, second)
        self.assertEqual(first, _plan())
        contract = load_benchmark_contract(_CONTRACT_PATH)
        self.assertEqual(first.corpus_digest, contract.corpus_digest)
        self.assertEqual(
            first.corpus_composition_digest,
            contract.corpus_composition_digest,
        )
        self.assertEqual(
            first.exact_cohort_digest,
            contract.exact_cohort_digest,
        )
        self.assertEqual(
            first.fuzzy_cohort_digest,
            contract.fuzzy_cohort_digest,
        )
        self.assertEqual(
            first.oracle_subset_digest,
            contract.oracle_subset_digest,
        )

    def test_strict_json_rejects_duplicate_keys_unknown_missing_and_types(
        self,
    ) -> None:
        text = _CONTRACT_PATH.read_text(encoding="utf-8")
        duplicate = text.replace(
            '"corpus_seed":20260729',
            '"corpus_seed":20260729,"corpus_seed":20260729',
            1,
        )
        with _contract_text_file(duplicate) as path:
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                load_benchmark_contract(path)

        envelope = json.loads(text)
        payload = dict(envelope["payload"])
        payload["bogus_field"] = 1
        envelope["payload"] = payload
        unknown = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with _contract_text_file(unknown) as path:
            with self.assertRaisesRegex(ValueError, "unexpected fields"):
                load_benchmark_contract(path)

        payload = dict(json.loads(text)["payload"])
        del payload["top_k"]
        envelope["payload"] = payload
        missing = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with _contract_text_file(missing) as path:
            with self.assertRaisesRegex(ValueError, "missing fields"):
                load_benchmark_contract(path)

        non_finite = _replace_payload_key(text, "top_k", '"top_k":NaN')
        with _contract_text_file(non_finite) as path:
            with self.assertRaisesRegex(ValueError, "non-finite"):
                load_benchmark_contract(path)

        bool_as_int = _replace_payload_key(
            text,
            "corpus_seed",
            '"corpus_seed":true',
        )
        with _contract_text_file(bool_as_int) as path:
            with self.assertRaisesRegex(TypeError, "corpus seed"):
                load_benchmark_contract(path)

        mistyped = _replace_payload_key(text, "top_k", '"top_k":"10"')
        with _contract_text_file(mistyped) as path:
            with self.assertRaisesRegex((TypeError, ValueError), "top_k"):
                load_benchmark_contract(path)

        with _contract_text_file("[]") as path:
            with self.assertRaisesRegex(ValueError, "JSON object"):
                load_benchmark_contract(path)

    def test_tampered_digest_fails_closed_on_recompute(self) -> None:
        text = _CONTRACT_PATH.read_text(encoding="utf-8")
        for field in ("corpus_digest", "oracle_subset_digest"):
            envelope = json.loads(text)
            payload = dict(envelope["payload"])
            payload[field] = "0" * 64
            envelope["payload"] = payload
            tampered = json.dumps(
                envelope,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            expected = field.replace("_", " ")
            with self.subTest(field=field):
                with _contract_text_file(tampered) as path:
                    with self.assertRaisesRegex(ValueError, expected):
                        recompute_benchmark_inputs(path)


class BenchmarkCorpusTests(unittest.TestCase):
    def test_full_corpus_stream_is_deterministic_and_ordered(self) -> None:
        pass_result = _full_pass()
        self.assertEqual(pass_result.record_count, 100_000)
        self.assertEqual(pass_result.ids, tuple(range(1, 100_001)))
        self.assertEqual(len(pass_result.sources), 100_000)
        first = list(iter_corpus_records(record_count=5_000))
        second = list(iter_corpus_records(record_count=5_000))
        self.assertEqual(
            [(r.record_id, r.source_raw, r.target_raw) for r in first],
            [(r.record_id, r.source_raw, r.target_raw) for r in second],
        )

    def test_corpus_category_coverage_from_generated_facts(self) -> None:
        pass_result = _full_pass()
        self.assertGreaterEqual(pass_result.cjk_count, 2_000)
        self.assertGreaterEqual(pass_result.short_count, 3_000)
        self.assertGreaterEqual(pass_result.context_count, 5_000)
        self.assertGreaterEqual(pass_result.duplicate_source_count, 3_000)
        self.assertGreaterEqual(pass_result.multi_target_count, 3_000)
        self.assertEqual(pass_result.near_edit_pairs_ok, 1_000)
        self.assertEqual(pass_result.near_edit_pairs_broken, 0)
        self.assertTrue(pass_result.miss_marker_absent)
        self.assertGreaterEqual(len(pass_result.scripts), 5)
        plan = _plan()
        self.assertGreaterEqual(len(plan.language_counts), 8)
        for language, count in plan.language_counts:
            self.assertEqual(
                pass_result.language_counts[language],
                count,
            )
            self.assertGreaterEqual(count, 1)

    def test_record_drafts_are_constructible(self) -> None:
        for record in iter_corpus_records():
            if record.record_id in (1, 3_000, 7_500, 10_000, 14_999, 100_000):
                draft = record.to_draft()
                self.assertEqual(draft.source_raw, record.source_raw)
                self.assertEqual(draft.target_raw, record.target_raw)
                self.assertEqual(
                    draft.provenance,
                    (("origin", TM_BENCHMARK_CORPUS_VERSION),),
                )

    def test_streaming_plan_holds_no_corpus_and_writes_no_files(self) -> None:
        source = Path(tm_benchmark.__file__).read_text(encoding="utf-8")
        for write_api in (
            "write_text",
            "write_bytes",
            "os.O_WRONLY",
            "os.O_RDWR",
            "os.O_CREAT",
            "unlink",
            "remove(",
            "os.replace",
        ):
            self.assertNotIn(write_api, source)
        self.assertIsInstance(iter_corpus_records(), Iterator)
        plan = _plan()
        for field_name, value in vars(plan).items():
            self.assertFalse(
                isinstance(value, (list, tuple)) and len(value) >= 100_000,
                field_name,
            )
        self.assertFalse(hasattr(plan, "records"))
        self.assertFalse(hasattr(plan, "corpus"))
        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                compute_benchmark_input_plan()
                for _ in iter_corpus_records():
                    pass
                for _ in iter_exact_queries():
                    pass
                for _ in iter_fuzzy_queries():
                    pass
                for _ in iter_oracle_subset_records():
                    pass
                for _ in iter_oracle_queries():
                    pass
                self.assertEqual(sorted(os.listdir(tmp)), [])
            finally:
                os.chdir(original_cwd)


class BenchmarkCohortTests(unittest.TestCase):
    def test_exact_cohort_is_fixed_and_bound_to_existing_records(self) -> None:
        pass_result = _full_pass()
        queries = list(iter_exact_queries())
        self.assertEqual(len(queries), 1_200)
        self.assertEqual([q.query_id for q in queries], list(range(1, 1_201)))
        references = [q.reference_record_id for q in queries]
        self.assertEqual(len(set(references)), 1_200)
        for query in queries:
            self.assertEqual(query.cohort, "exact")
            self.assertEqual(query.category, "exact")
            assert query.reference_record_id is not None
            self.assertEqual(
                query.query_raw,
                pass_result.sources[query.reference_record_id],
            )

    def test_fuzzy_cohort_has_near_edit_and_miss_cases(self) -> None:
        pass_result = _full_pass()
        queries = list(iter_fuzzy_queries())
        self.assertEqual(len(queries), 240)
        self.assertEqual([q.query_id for q in queries], list(range(1, 241)))
        near_edit = [q for q in queries if q.category == "near-edit"]
        miss = [q for q in queries if q.category == "miss"]
        self.assertEqual(len(near_edit), 200)
        self.assertEqual(len(miss), 40)
        self.assertEqual(
            [q.query_id for q in near_edit],
            list(range(1, 201)),
        )
        self.assertEqual(
            [q.query_id for q in miss],
            list(range(201, 241)),
        )
        for query in near_edit:
            assert query.reference_record_id is not None
            source = pass_result.sources[query.reference_record_id]
            self.assertEqual(len(query.query_raw), len(source))
            self.assertEqual(
                sum(a != b for a, b in zip(query.query_raw, source)),
                1,
            )
        for query in miss:
            self.assertIsNone(query.reference_record_id)
            self.assertTrue(query.query_raw.startswith("zzmissf5v1-"))

    def test_oracle_subset_is_fixed_5000_records_and_200_queries(self) -> None:
        pass_result = _full_pass()
        records = list(iter_oracle_subset_records())
        self.assertEqual(len(records), 5_000)
        record_ids = [record.record_id for record in records]
        self.assertEqual(record_ids, sorted(record_ids))
        self.assertEqual(len(set(record_ids)), 5_000)
        self.assertTrue(all(1 <= rid <= 100_000 for rid in record_ids))
        oracle_id_set = set(record_ids)

        queries = list(iter_oracle_queries())
        self.assertEqual(len(queries), 200)
        self.assertEqual([q.query_id for q in queries], list(range(1, 201)))
        exact = [q for q in queries if q.category == "exact"]
        near_edit = [q for q in queries if q.category == "near-edit"]
        miss = [q for q in queries if q.category == "miss"]
        self.assertEqual(len(exact), 160)
        self.assertEqual(len(near_edit), 20)
        self.assertEqual(len(miss), 20)
        for query in exact + near_edit:
            assert query.reference_record_id is not None
            self.assertIn(query.reference_record_id, oracle_id_set)
            source = pass_result.sources[query.reference_record_id]
            if query.category == "exact":
                self.assertEqual(query.query_raw, source)
            else:
                self.assertEqual(len(query.query_raw), len(source))
                self.assertEqual(
                    sum(a != b for a, b in zip(query.query_raw, source)),
                    1,
                )
        for query in miss:
            self.assertIsNone(query.reference_record_id)
            self.assertTrue(query.query_raw.startswith("zzmissf5v1-"))


class BenchmarkDigestSensitivityTests(unittest.TestCase):
    def test_changed_seed_changes_all_content_digests(self) -> None:
        baseline = _plan()
        changed = _plan(seed=20260730)
        self.assertNotEqual(changed.corpus_digest, baseline.corpus_digest)
        self.assertNotEqual(
            changed.corpus_composition_digest,
            baseline.corpus_composition_digest,
        )
        self.assertNotEqual(
            changed.exact_cohort_digest,
            baseline.exact_cohort_digest,
        )
        self.assertNotEqual(
            changed.fuzzy_cohort_digest,
            baseline.fuzzy_cohort_digest,
        )
        self.assertNotEqual(
            changed.oracle_subset_digest,
            baseline.oracle_subset_digest,
        )

    def test_changed_record_count_changes_corpus_digest(self) -> None:
        baseline = _plan()
        changed = _plan(record_count=99_999)
        self.assertNotEqual(changed.corpus_digest, baseline.corpus_digest)

    def test_changed_version_changes_content_digests(self) -> None:
        baseline = _plan()
        changed = _plan(generator_version="tm-benchmark-corpus-v2")
        self.assertNotEqual(changed.corpus_digest, baseline.corpus_digest)
        self.assertNotEqual(
            changed.corpus_composition_digest,
            baseline.corpus_composition_digest,
        )
        self.assertNotEqual(
            changed.exact_cohort_digest,
            baseline.exact_cohort_digest,
        )

    def test_changed_order_changes_cohort_digest(self) -> None:
        plan = _plan()
        queries = list(iter_exact_queries())
        payloads = [_query_item(query) for query in queries]
        forward = tm_benchmark.benchmark_digest(
            plan.generator_version,
            "exact-cohort",
            payloads,
        )
        reversed_digest = tm_benchmark.benchmark_digest(
            plan.generator_version,
            "exact-cohort",
            reversed(payloads),
        )
        self.assertNotEqual(forward, reversed_digest)
        self.assertEqual(forward, plan.exact_cohort_digest)

    def test_changed_query_body_changes_cohort_digest(self) -> None:
        plan = _plan()
        payloads = [_query_item(query) for query in iter_fuzzy_queries()]
        altered: list[dict[str, object]] = [
            dict(payload) for payload in payloads
        ]
        query_raw = altered[0]["query_raw"]
        if not isinstance(query_raw, str):
            self.fail("query_raw must be a string")
        altered[0]["query_raw"] = query_raw + "x"
        original = tm_benchmark.benchmark_digest(
            plan.generator_version,
            "fuzzy-cohort",
            payloads,
        )
        tampered = tm_benchmark.benchmark_digest(
            plan.generator_version,
            "fuzzy-cohort",
            altered,
        )
        self.assertNotEqual(original, tampered)


class BenchmarkDigestIndependenceTests(unittest.TestCase):
    def test_corpus_digest_is_independently_recomputed(self) -> None:
        pass_result = _full_pass()
        self.assertEqual(pass_result.corpus_digest, _plan().corpus_digest)

    def test_composition_digest_is_independently_recomputed(self) -> None:
        plan = _plan()
        pass_result = _full_pass()
        independent = _independent_digest(
            plan.generator_version,
            "corpus-composition",
            iter([_composition_item(plan, pass_result)]),
        )
        self.assertEqual(independent, plan.corpus_composition_digest)

    def test_cohort_digests_are_independently_recomputed(self) -> None:
        plan = _plan()
        exact = _independent_digest(
            plan.generator_version,
            "exact-cohort",
            (_query_item(query) for query in iter_exact_queries()),
        )
        fuzzy = _independent_digest(
            plan.generator_version,
            "fuzzy-cohort",
            (_query_item(query) for query in iter_fuzzy_queries()),
        )
        self.assertEqual(exact, plan.exact_cohort_digest)
        self.assertEqual(fuzzy, plan.fuzzy_cohort_digest)

    def test_oracle_digest_is_independently_recomputed(self) -> None:
        plan = _plan()
        items: list[dict[str, object]] = [
            {"section": "record", **_record_item(record)}
            for record in iter_oracle_subset_records()
        ]
        items.extend(
            {"section": "query", **_query_item(query)}
            for query in iter_oracle_queries()
        )
        independent = _independent_digest(
            plan.generator_version,
            "oracle-subset",
            iter(items),
        )
        self.assertEqual(independent, plan.oracle_subset_digest)


class BenchmarkModuleBoundaryTests(unittest.TestCase):
    def test_module_imports_only_stdlib_and_frozen_contracts(self) -> None:
        source = Path(tm_benchmark.__file__).read_text(encoding="utf-8")
        imported = {
            match.group(1).split(".")[0]
            for match in _IMPORT_RE.finditer(source)
        }
        self.assertTrue(imported)
        stdlib = set(sys.stdlib_module_names)
        for module in sorted(imported):
            self.assertTrue(
                module in stdlib or module == "tm_contracts",
                f"unexpected import: {module}",
            )

    def test_no_forbidden_runtime_imports(self) -> None:
        banned = ", ".join(repr(name) for name in sorted(_BANNED_RUNTIME_MODULES))
        code = (
            "import sys\n"
            "import tm_benchmark\n"
            f"banned = {{{banned}}}\n"
            "loaded = {m.split('.')[0] for m in sys.modules}\n"
            "assert not (loaded & banned), sorted(loaded & banned)\n"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_no_runtime_reverse_imports_of_tm_benchmark(self) -> None:
        code = (
            "import sys\n"
            "import tm_contracts\n"
            "import tm_similarity\n"
            "assert 'tm_benchmark' not in sys.modules\n"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        for filename in _RUNTIME_MODULES:
            with self.subTest(filename=filename):
                text = (_ROOT / filename).read_text(encoding="utf-8")
                self.assertNotIn("tm_benchmark", text)


@contextmanager
def _contract_text_file(text: str) -> Iterator[Path]:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        delete=False,
    )
    try:
        handle.write(text)
    finally:
        handle.close()
    path = Path(handle.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
