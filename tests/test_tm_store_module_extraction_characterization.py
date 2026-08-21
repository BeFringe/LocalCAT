"""Wave 0 brownfield inventory for the TM candidate store extraction.

These tests intentionally describe the pre-extraction authority.  Later waves
must update the expected owner, never weaken the closed inventories.
"""

from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import unittest

from tests.acceptance_matrix_registry import acceptance_matrix_source_paths
from tests.fault_matrix_registry import fault_matrix_source_paths
from tm_benchmark import (
    BENCHMARK_IMPLEMENTATION_SOURCE_PATHS,
    benchmark_implementation_fingerprint,
)


_ROOT = Path(__file__).resolve().parents[1]
_CANDIDATE_NAMES = (
    "SQLiteCandidateRecord",
    "SQLiteCandidateProofBlock",
    "SQLiteCandidateProofDensePhase1",
    "SQLiteCandidateProofDensePhase2",
    "SQLiteCandidateProofRecord",
    "SQLiteCandidateProofSnapshot",
    "SQLiteCandidateRecallSnapshot",
    "SQLiteCandidateWritePlan",
    "SQLiteStoreSchemaError",
    "CANDIDATE_PROOF_BLOCK_SIZE",
    "CANDIDATE_PROOF_BLOCK_VERSION_V1",
    "_validate_candidate_proof_dense_phase1_result",
    "_validate_candidate_proof_dense_phase2_result",
    "build_candidate_write_plan",
    "unique_character_ngrams",
)
_EXPECTED_INDEX_STORE_IMPORTS: tuple[tuple[str, str | None], ...] = ()
_EXPECTED_STORE_CONSUMERS = {
    "editor_tm_adapter.py": (("SQLiteStoreSchemaError", None),),
    "resource_importer.py": (("SQLiteStoreSchemaError", None),),
    "tm_application_composition.py": (("SQLiteStoreSchemaError", None),),
    "tm_engine.py": (("SQLiteStoreSchemaError", None),),
    "tm_migration.py": (
        ("SQLiteStoreSchemaError", None),
        ("unique_character_ngrams", None),
        ("validate_candidate_proof_index", None),
    ),
    "tm_retrieval.py": (("SQLiteStoreSchemaError", None),),
    "tm_stage_sealer.py": (
        ("CandidateProofIndexError", None),
        ("SQLiteStoreSchemaError", None),
        ("_candidate_proof_projection_digest", None),
        ("_validate_candidate_proof_index_with_digest", None),
    ),
}

_EXPECTED_MODULE_PATCH_SEAMS = Counter(
    {
        (
            "tests/test_tm_activation_publication.py",
            "tm_sqlite_store.validate_candidate_proof_index",
        ): 1,
        (
            "tests/test_tm_activation_recovery.py",
            "tm_sqlite_store.validate_candidate_proof_index",
        ): 1,
        (
            "tests/test_tm_candidate_index.py",
            "tm_sqlite_store._apply_candidate_write_plan",
        ): 1,
        (
            "tests/test_tm_candidate_proof_query.py",
            "tm_sqlite_store._validate_candidate_proof_dense_binding",
        ): 1,
        (
            "tests/test_tm_sqlite_store.py",
            "tm_sqlite_store.build_candidate_write_plan",
        ): 1,
        (
            "tests/test_tm_sqlite_store.py",
            "tm_sqlite_store.character_ngram_frequencies",
        ): 1,
    }
)
_EXPECTED_INSTANCE_PATCH_SEAMS = Counter(
    {
        ("tests/test_tm_candidate_index.py", "candidate_recall_snapshot"): 5,
        ("tests/test_tm_candidate_index.py", "fts5_candidate_ids"): 1,
        ("tests/test_tm_candidate_index.py", "gram_candidate_overlaps"): 3,
        (
            "tests/test_tm_candidate_proof_query.py",
            "candidate_proof_block_records",
        ): 4,
        (
            "tests/test_tm_candidate_proof_query.py",
            "candidate_proof_dense_phase1",
        ): 2,
        (
            "tests/test_tm_candidate_proof_query.py",
            "candidate_proof_dense_phase2",
        ): 6,
        (
            "tests/test_tm_candidate_proof_query.py",
            "candidate_proof_snapshot",
        ): 3,
        (
            "tests/test_tm_store_candidate_projection_delegation.py",
            "candidate_recall_snapshot",
        ): 4,
        (
            "tests/test_tm_store_candidate_projection_delegation.py",
            "candidate_proof_snapshot",
        ): 4,
        (
            "tests/test_tm_store_candidate_projection_delegation.py",
            "candidate_proof_block_records",
        ): 2,
        (
            "tests/test_tm_store_candidate_projection_delegation.py",
            "candidate_proof_dense_phase1",
        ): 3,
        (
            "tests/test_tm_store_candidate_projection_delegation.py",
            "candidate_proof_dense_phase2",
        ): 2,
        (
            "tests/test_tm_store_candidate_projection_delegation.py",
            "fts5_candidate_ids",
        ): 2,
        (
            "tests/test_tm_store_candidate_projection_delegation.py",
            "fts5_candidate_ids_for_trigrams",
        ): 2,
        (
            "tests/test_tm_store_candidate_projection_delegation.py",
            "gram_candidate_overlaps",
        ): 2,
    }
)
_FULL_MODULE_PATCH_ENTRY_COUNT = 61
_FULL_MODULE_PATCH_CALL_COUNT = 306
_FULL_MODULE_PATCH_DIGEST = (
    "ce059addd6233cf8ae9f2d455606ef49167964001d307dd2da309f885e4f035a"
)
_FULL_INSTANCE_PATCH_ENTRY_COUNT = 19
_FULL_INSTANCE_PATCH_CALL_COUNT = 49
_FULL_INSTANCE_PATCH_DIGEST = (
    "d923e57c0b6c37a516045e735259d0d13a6fe8ee8a3de6a441e3869922eadf6a"
)

_SQL_TOKENS = (
    "tm_gram",
    "tm_fts",
    "tm_candidate_block",
    "tm_candidate_gram_block_max",
    "candidate_index_digest",
    "source_fold_v1",
    "source_fold_length",
)
_CANDIDATE_PATCH_MARKERS = ("candidate", "fts5", "gram", "proof", "seed")
_EXPECTED_SQL_OWNERS = {
    "tm_schema_upgrade.py": frozenset(
        {"_migrate_schema_copy", "flush_proof_block"}
    ),
    "tm_stage_sealer.py": frozenset(
        {
            "_stage_closure_digests",
            "_validate_schema_upgrade_stage_facts",
            "_validate_stage_facts",
        }
    ),
    "tm_sqlite_candidate_projection.py": frozenset(
        {
            "bounded_seed_stages",
            "candidate_proof_block_records",
            "candidate_proof_dense_phase1",
            "candidate_proof_dense_phase2",
            "candidate_proof_query_block_uppers",
            "candidate_proof_snapshot",
            "candidate_recall_snapshot",
            "fts5_candidate_ids",
            "fts5_candidate_ids_for_trigrams",
            "gram_candidate_overlaps",
            "validate_candidate_proof_blocks",
        }
    ),
    "tm_sqlite_store.py": frozenset(
        {
            "<module>",
            "_apply_candidate_write_plan",
            "_candidate_proof_projection_digest",
            "_insert_prepared_records_and_indexes",
            "_insert_streamed_candidate_index",
            "_insert_streamed_records",
            "_maintain_candidate_proof_summaries",
            "_probe_fts5",
            "_update_candidate_gram_projection_digest",
            "_validate_candidate_proof_index_core",
        }
    ),
}

_BEHAVIOR_ANCHORS = (
    "tests.test_tm_candidate_index.FTS5TrigramIndexTests."
    "test_fts_write_failure_rolls_back_origin_record_index_and_revision",
    "tests.test_tm_candidate_index.CandidateRetrieverTests."
    "test_candidate_query_failure_is_resource_local_and_explicit",
    "tests.test_tm_candidate_proof_query.CandidateProofQueryTests."
    "test_dense_fact_transaction_commits_before_scorer_and_append_is_stale",
    "tests.test_tm_candidate_proof_query.CandidateProofQueryTests."
    "test_generation_change_after_snapshot_fails_closed",
    "tests.test_tm_sqlite_store.SQLiteTMStoreTests."
    "test_candidate_sql_failure_rolls_back_entire_batch",
    "tests.test_tm_sqlite_store.SQLiteTMStoreTests."
    "test_commit_failure_rolls_back_origin_record_and_candidate_rows",
    "tests.test_tm_sqlite_store.SQLiteTMStoreTests."
    "test_streamed_append_mid_stream_failure_never_completes_batch",
    "tests.test_tm_sqlite_store.SQLiteTMQueryViewTests."
    "test_dense_phase2_returns_only_strict_ordered_fold_projection",
    "tests.test_tm_sqlite_store.SQLiteTMStoreTests."
    "test_chunked_candidate_helpers_hold_one_read_snapshot",
    "tests.test_tm_candidate_proof_index.CandidateProofIndexV16Tests."
    "test_committed_phase2_does_not_bypass_final_head_validation",
    "tests.test_tm_candidate_proof_query.CandidateProofQueryTests."
    "test_append_during_dense_phase2_is_stale_without_scorer_lock",
    "tests.test_tm_candidate_proof_query.CandidateProofQueryTests."
    "test_append_after_phase2_during_scorer_is_stale_and_nonblocking",
    "tests.test_tm_sqlite_store.SQLiteTMQueryViewTests."
    "test_expired_query_view_fails_closed_without_connection_or_new_lease",
    "tests.test_tm_sqlite_store.SQLiteTMQueryViewTests."
    "test_query_view_survives_drain_but_rejects_drift_or_foreign_binding",
    "tests.test_tm_sqlite_store.SQLiteTMQueryViewTests."
    "test_query_lease_blocks_generation_publication_until_exit",
)


def _tree(relative: str) -> ast.Module:
    return ast.parse((_ROOT / relative).read_text(encoding="utf-8"))


def _imports_from(relative: str, module: str) -> tuple[tuple[str, str | None], ...]:
    imports: list[tuple[str, str | None]] = []
    for node in ast.walk(_tree(relative)):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            imports.extend((item.name, item.asname) for item in node.names)
    return tuple(imports)


def _candidate_store_consumers() -> dict[str, tuple[tuple[str, str | None], ...]]:
    observed: dict[str, tuple[tuple[str, str | None], ...]] = {}
    for path in sorted(_ROOT.glob("*.py")):
        selected = tuple(
            item
            for item in _imports_from(path.name, "tm_sqlite_store")
            if item[0] in _CANDIDATE_NAMES
            or any(
                marker in item[0].lower()
                for marker in ("candidate", "gram", "proof")
            )
        )
        if selected:
            observed[path.name] = selected
    return observed


def _patch_seams_in_tree(
    relative: str,
    tree: ast.Module,
) -> tuple[Counter[tuple[str, str]], Counter[tuple[str, str]]]:
    module_targets: Counter[tuple[str, str]] = Counter()
    instance_targets: Counter[tuple[str, str]] = Counter()
    patch_aliases = {
        item.asname or item.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "unittest.mock"
        for item in node.names
        if item.name == "patch"
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for argument in (
            *node.args,
            *(keyword.value for keyword in node.keywords),
        ):
            if (
                isinstance(argument, ast.Constant)
                and type(argument.value) is str
                and argument.value.startswith(
                    ("tm_sqlite_store.", "tm_sqlite_candidate_projection.")
                )
                and argument.value not in {
                    "tm_sqlite_store.py",
                    "tm_sqlite_candidate_projection.py",
                }
            ):
                module_targets[(relative, argument.value)] += 1
        function_name = ast.unparse(node.func)
        is_patch_object = function_name.endswith("patch.object") or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "object"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in patch_aliases
        )
        if not is_patch_object:
            continue
        attribute_arguments = list(node.args[1:2])
        attribute_arguments.extend(
            keyword.value
            for keyword in node.keywords
            if keyword.arg == "attribute"
        )
        for attribute in attribute_arguments:
            if (
                isinstance(attribute, ast.Constant)
                and type(attribute.value) is str
                and any(
                    marker in attribute.value.lower()
                    for marker in _CANDIDATE_PATCH_MARKERS
                )
            ):
                instance_targets[(relative, attribute.value)] += 1
    return module_targets, instance_targets


def _patch_seams() -> tuple[Counter[tuple[str, str]], Counter[tuple[str, str]]]:
    module_targets: Counter[tuple[str, str]] = Counter()
    instance_targets: Counter[tuple[str, str]] = Counter()
    for path in sorted((_ROOT / "tests").glob("test_*.py")):
        relative = path.relative_to(_ROOT).as_posix()
        observed_module, observed_instance = _patch_seams_in_tree(
            relative,
            _tree(relative),
        )
        module_targets.update(observed_module)
        instance_targets.update(observed_instance)
    return module_targets, instance_targets


def _patch_inventory_digest(inventory: Counter[tuple[str, str]]) -> str:
    payload = json.dumps(
        sorted(
            (relative, target, count)
            for (relative, target), count in inventory.items()
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sql_owners_in_tree(relative: str, tree: ast.Module) -> frozenset[str]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    owners: set[str] = set()
    if relative == "tm_sqlite_candidate_projection.py":
        bound_sql: dict[tuple[str, str], str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            value = node.value
            if not isinstance(value, ast.Constant) or type(value.value) is not str:
                continue
            owner: ast.AST = node
            while owner in parents and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents[owner]
            owner_name = (
                owner.name if isinstance(owner, ast.FunctionDef) else "<module>"
            )
            for target in targets:
                if isinstance(target, ast.Name):
                    bound_sql[(owner_name, target.id)] = value.value
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Call)
                or not ast.unparse(node.func).endswith(".execute")
            ):
                continue
            owner: ast.AST = node
            while owner in parents and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents[owner]
            owner_name = (
                owner.name if isinstance(owner, ast.FunctionDef) else "<module>"
            )
            sql_text = " ".join(
                item.value
                for item in ast.walk(node)
                if isinstance(item, ast.Constant) and type(item.value) is str
            )
            if (
                not sql_text
                and node.args
                and isinstance(node.args[0], ast.Name)
            ):
                sql_text = bound_sql.get((owner_name, node.args[0].id), "")
            if not any(
                keyword in sql_text.upper()
                for keyword in (
                    "SELECT ",
                    "INSERT ",
                    "UPDATE ",
                    "DELETE ",
                    "CREATE ",
                    "ALTER ",
                    "DROP ",
                    "WITH ",
                )
            ):
                owners.add(f"<dynamic:{owner_name}>")
            else:
                owners.add(owner_name)
        return frozenset(owners)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Call, ast.Assign, ast.AnnAssign)):
            continue
        sql_text = " ".join(
            item.value
            for item in ast.walk(node)
            if isinstance(item, ast.Constant) and type(item.value) is str
        )
        if not (
            any(token in sql_text for token in _SQL_TOKENS)
            and any(
                keyword in sql_text.upper()
                for keyword in (
                    "SELECT ",
                    "INSERT ",
                    "UPDATE ",
                    "DELETE ",
                    "CREATE ",
                    "ALTER ",
                    "DROP ",
                    "WITH ",
                )
            )
        ):
            continue
        owner: ast.AST = node
        while owner in parents and not isinstance(
            owner, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            owner = parents[owner]
        owners.add(owner.name if isinstance(owner, ast.FunctionDef) else "<module>")
    return frozenset(owners)


def _sql_owners(relative: str) -> frozenset[str]:
    return _sql_owners_in_tree(relative, _tree(relative))


class TMStoreModuleExtractionCharacterizationTests(unittest.TestCase):
    def test_candidate_patch_inventory_is_closed_for_migrated_seams(self) -> None:
        tree = ast.parse(
            """
from unittest.mock import patch
patch("tm_sqlite_candidate_projection.candidate_recall_snapshot")
patch("tm_sqlite_store._bounded_seed_stages")
patch("tm_sqlite_candidate_projection.future_candidate_query")
mock.patch("tm_sqlite_candidate_projection.future_mock_query")
patch(target="tm_sqlite_candidate_projection.future_keyword_query")
from unittest.mock import patch as p
p("tm_sqlite_candidate_projection.future_alias_query")
patch.object(self.alternate_store, "_bounded_seed_stages")
patch.object(self.alternate_store, "future_candidate_query")
mock.patch.object(self.alternate_store, "future_proof_query")
p.object(self.alternate_store, attribute="future_gram_query")
"""
        )
        module_targets, instance_targets = _patch_seams_in_tree(
            "tests/test_synthetic.py",
            tree,
        )
        self.assertEqual(
            module_targets,
            Counter(
                {
                    (
                        "tests/test_synthetic.py",
                        "tm_sqlite_candidate_projection.candidate_recall_snapshot",
                    ): 1,
                    (
                        "tests/test_synthetic.py",
                        "tm_sqlite_store._bounded_seed_stages",
                    ): 1,
                    (
                        "tests/test_synthetic.py",
                        "tm_sqlite_candidate_projection.future_candidate_query",
                    ): 1,
                    (
                        "tests/test_synthetic.py",
                        "tm_sqlite_candidate_projection.future_mock_query",
                    ): 1,
                    (
                        "tests/test_synthetic.py",
                        "tm_sqlite_candidate_projection.future_keyword_query",
                    ): 1,
                    (
                        "tests/test_synthetic.py",
                        "tm_sqlite_candidate_projection.future_alias_query",
                    ): 1,
                }
            ),
        )
        self.assertEqual(
            instance_targets,
            Counter(
                {
                    (
                        "tests/test_synthetic.py",
                        "_bounded_seed_stages",
                    ): 1,
                    (
                        "tests/test_synthetic.py",
                        "future_candidate_query",
                    ): 1,
                    (
                        "tests/test_synthetic.py",
                        "future_proof_query",
                    ): 1,
                    (
                        "tests/test_synthetic.py",
                        "future_gram_query",
                    ): 1,
                }
            ),
        )

    def test_projection_sql_inventory_includes_plain_record_queries(self) -> None:
        tree = ast.parse(
            '''
def future_candidate_count(connection):
    query = "SELECT COUNT(*) FROM tm_record"
    return connection.execute(query).fetchone()
'''
        )
        self.assertEqual(
            _sql_owners_in_tree("tm_sqlite_candidate_projection.py", tree),
            frozenset({"future_candidate_count"}),
        )
        dynamic_tree = ast.parse(
            '''
def future_candidate_dynamic(connection, caller_query):
    return connection.execute(caller_query).fetchone()
'''
        )
        self.assertEqual(
            _sql_owners_in_tree(
                "tm_sqlite_candidate_projection.py",
                dynamic_tree,
            ),
            frozenset({"<dynamic:future_candidate_dynamic>"}),
        )

    def test_candidate_index_concrete_import_baseline_is_exact(self) -> None:
        self.assertEqual(
            _imports_from("tm_candidate_index.py", "tm_sqlite_store"),
            _EXPECTED_INDEX_STORE_IMPORTS,
        )

    def test_candidate_store_contract_consumers_are_closed(self) -> None:
        self.assertEqual(_candidate_store_consumers(), _EXPECTED_STORE_CONSUMERS)

    def test_candidate_patch_targets_and_counts_are_closed(self) -> None:
        module_targets, instance_targets = _patch_seams()
        self.assertEqual(len(module_targets), _FULL_MODULE_PATCH_ENTRY_COUNT)
        self.assertEqual(sum(module_targets.values()), _FULL_MODULE_PATCH_CALL_COUNT)
        self.assertEqual(
            _patch_inventory_digest(module_targets),
            _FULL_MODULE_PATCH_DIGEST,
        )
        self.assertEqual(len(instance_targets), _FULL_INSTANCE_PATCH_ENTRY_COUNT)
        self.assertEqual(
            sum(instance_targets.values()),
            _FULL_INSTANCE_PATCH_CALL_COUNT,
        )
        self.assertEqual(
            _patch_inventory_digest(instance_targets),
            _FULL_INSTANCE_PATCH_DIGEST,
        )
        self.assertEqual(
            Counter(
                {
                    key: module_targets[key]
                    for key in _EXPECTED_MODULE_PATCH_SEAMS
                    if key in module_targets
                }
            ),
            _EXPECTED_MODULE_PATCH_SEAMS,
        )
        self.assertEqual(
            Counter(
                {
                    key: instance_targets[key]
                    for key in _EXPECTED_INSTANCE_PATCH_SEAMS
                    if key in instance_targets
                }
            ),
            _EXPECTED_INSTANCE_PATCH_SEAMS,
        )

    def test_candidate_sql_literal_owners_are_closed(self) -> None:
        observed = {
            path.name: _sql_owners(path.name)
            for path in sorted(_ROOT.glob("*.py"))
            if _sql_owners(path.name)
        }
        self.assertEqual(observed, _EXPECTED_SQL_OWNERS)

    def test_transaction_and_fault_anchor_tests_resolve_exactly_once(self) -> None:
        loader = unittest.defaultTestLoader
        for test_id in _BEHAVIOR_ANCHORS:
            with self.subTest(test_id=test_id):
                suite = loader.loadTestsFromName(test_id)
                self.assertEqual(suite.countTestCases(), 1)
                self.assertNotIn("_FailedTest", repr(suite))

    def test_staged_current_source_evidence_staleness_is_explicit(self) -> None:
        gate_c = json.loads(
            (_ROOT / "tests/fixtures/retrieval_gate_c_roots_v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            set(gate_c["artifact_paths"])
            & {"tm_candidate_index.py", "tm_sqlite_store.py"},
            {"tm_candidate_index.py", "tm_sqlite_store.py"},
        )
        self.assertEqual(
            set(gate_c["build_paths"])
            & {"tm_candidate_index.py", "tm_sqlite_store.py"},
            {"tm_candidate_index.py", "tm_sqlite_store.py"},
        )
        future_roots = {
            "tm_candidate_store_contracts.py",
            "tm_sqlite_candidate_projection.py",
        }
        self.assertTrue(future_roots.isdisjoint(gate_c["artifact_paths"]))
        self.assertTrue(future_roots.isdisjoint(gate_c["build_paths"]))
        self.assertTrue(future_roots.isdisjoint(BENCHMARK_IMPLEMENTATION_SOURCE_PATHS))

        acceptance_paths = set(acceptance_matrix_source_paths())
        fault_paths = set(fault_matrix_source_paths())
        self.assertTrue(
            {"tm_candidate_index.py", "tm_sqlite_store.py"}.issubset(
                acceptance_paths
            )
        )
        self.assertTrue("tm_sqlite_store.py" in fault_paths)
        self.assertTrue(future_roots.isdisjoint(acceptance_paths | fault_paths))

        acceptance = json.loads(
            (_ROOT / "acceptance_matrix_evidence.json").read_text(encoding="utf-8")
        )
        fault = json.loads(
            (_ROOT / "fault_matrix_evidence.json").read_text(encoding="utf-8")
        )
        benchmark = json.loads(
            (_ROOT / "benchmark_tm_evidence.json").read_text(encoding="utf-8")
        )
        release = json.loads(
            (_ROOT / "release_criteria_evidence.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            acceptance["source_fingerprint"],
            "95486b40df30d198281f7972db9078ff4d19caf7b49715ba34a4862c8fafcedf",
        )
        self.assertEqual(
            fault["source_fingerprint"],
            "d503282bdfab54f8d9529311c333e9456bd5d318e09258c7e6e6fa6a7194ab62",
        )
        self.assertEqual(
            benchmark["implementation_fingerprint"],
            "2dadef65550cc338b57686961fe15cbe8a49aa04bdd583ea58beb8b5721f0e44",
        )
        self.assertEqual(
            release["source_fingerprint"],
            "9451b258e765d4c32d6560d8509682a8313e5c84d4d37e8ee409df7e6f8c09c0",
        )
        self.assertEqual(
            release["input_evidence"]["acceptance_source_fingerprint"],
            acceptance["source_fingerprint"],
        )
        self.assertTrue(
            all(
                hashlib.sha256((_ROOT / item["path"]).read_bytes()).hexdigest()
                == item["sha256"]
                for item in release["source_files"]
            )
        )
        self.assertNotEqual(
            benchmark_implementation_fingerprint(_ROOT),
            benchmark["implementation_fingerprint"],
        )
        stale_by_evidence: dict[str, tuple[str, ...]] = {}
        for name, evidence in (("acceptance", acceptance), ("fault", fault)):
            stale_by_evidence[name] = tuple(
                item["path"]
                for item in evidence["source_files"]
                if hashlib.sha256(
                    (_ROOT / item["path"]).read_bytes()
                ).hexdigest()
                != item["sha256"]
            )
        self.assertEqual(
            stale_by_evidence,
            {
                "acceptance": (
                    "editor_controller.py",
                    "qt_editor.py",
                    "qt_settings_dialog.py",
                    "tm_candidate_index.py",
                    "tm_sqlite_store.py",
                ),
                "fault": ("tm_sqlite_store.py",),
            },
        )


if __name__ == "__main__":
    unittest.main()
