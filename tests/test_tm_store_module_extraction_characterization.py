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
from tm_gate_a import aggregate_paths_digest


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
_FULL_INSTANCE_PATCH_ENTRY_COUNT = 36
_FULL_INSTANCE_PATCH_CALL_COUNT = 83
_FULL_INSTANCE_PATCH_DIGEST = (
    "ac027645e9edf0089bf01e238859b71316502a8899a4d61a7b57dc50e09e1f59"
)

_SQL_TOKENS = (
    "tm_gram",
    "tm_fts",
    "tm_candidate_block",
    "tm_candidate_gram_block_max",
    "candidate_index_digest",
    "PRAGMA threads",
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
            "_update_candidate_gram_projection_digest",
            "_validate_candidate_proof_index_core",
            "bounded_seed_stages",
            "candidate_proof_block_records",
            "candidate_proof_dense_phase1",
            "candidate_proof_dense_phase2",
            "candidate_proof_projection_digest",
            "candidate_proof_query_block_uppers",
            "candidate_proof_snapshot",
            "candidate_recall_snapshot",
            "fts5_candidate_ids",
            "fts5_candidate_ids_for_trigrams",
            "gram_candidate_overlaps",
            "insert_candidate_fts_rows",
            "insert_candidate_gram_rows",
            "insert_streamed_candidate_fts_rows",
            "insert_streamed_candidate_gram_rows",
            "insert_streamed_candidate_proof_rows",
            "maintain_candidate_proof_summaries",
            "restore_streamed_stage_secondary_indexes",
            "streamed_stage_secondary_index_inventory",
            "suspend_streamed_stage_secondary_indexes",
            "validate_candidate_proof_blocks",
        }
    ),
    "tm_sqlite_store.py": frozenset(
        {
            "<module>",
            "_insert_prepared_records_and_indexes",
            "_insert_streamed_records",
            "_probe_fts5",
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

_RETIRED_STORE_WRAPPERS = frozenset(
    {
        "_candidate_proof_query_maxima_digest",
        "_candidate_proof_query_block_uppers",
        "_candidate_projection_table_digest",
        "_update_candidate_projection_digest",
        "_finish_candidate_projection_digest",
        "_update_candidate_gram_projection_digest",
    }
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


def _retired_store_consumers_in_tree(
    relative: str,
    tree: ast.Module,
) -> set[tuple[str, str, str]]:
    consumers: set[tuple[str, str, str]] = set()
    store_aliases = {
        item.asname or item.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for item in node.names
        if item.name == "tm_sqlite_store"
    }
    store_prefix = "tm_sqlite_store" + "."
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tm_sqlite_store":
            consumers.update(
                (relative, "import", item.name)
                for item in node.names
                if item.name in _RETIRED_STORE_WRAPPERS
            )
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in store_aliases
            and node.attr in _RETIRED_STORE_WRAPPERS
        ):
            consumers.add((relative, "attribute", node.attr))
        if (
            isinstance(node, ast.Constant)
            and type(node.value) is str
            and node.value.startswith(store_prefix)
        ):
            symbol = node.value.removeprefix(store_prefix)
            if symbol in _RETIRED_STORE_WRAPPERS:
                consumers.add((relative, "patch", symbol))
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in store_aliases
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _RETIRED_STORE_WRAPPERS
        ):
            consumers.add((relative, "getattr", node.args[1].value))
        target_arguments = list(node.args[:1])
        target_arguments.extend(
            keyword.value for keyword in node.keywords if keyword.arg == "target"
        )
        attribute_arguments = list(node.args[1:2])
        attribute_arguments.extend(
            keyword.value
            for keyword in node.keywords
            if keyword.arg == "attribute"
        )
        if not any(
            isinstance(target, ast.Name) and target.id in store_aliases
            for target in target_arguments
        ):
            continue
        consumers.update(
            (relative, "patch.object", attribute.value)
            for attribute in attribute_arguments
            if isinstance(attribute, ast.Constant)
            and attribute.value in _RETIRED_STORE_WRAPPERS
        )
    return consumers


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
        sql_call_aliases: dict[str, set[str]] = {}

        def owner_name_for(node: ast.AST) -> str:
            owner: ast.AST = node
            while owner in parents and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents[owner]
            return (
                owner.name
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
                else "<module>"
            )

        def is_sql_method(value: ast.AST, owner_name: str) -> bool:
            if (
                isinstance(value, ast.Attribute)
                and value.attr in {"execute", "executemany", "executescript"}
            ):
                return True
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "getattr"
                and len(value.args) >= 2
                and isinstance(value.args[1], ast.Constant)
                and value.args[1].value
                in {"execute", "executemany", "executescript"}
            ):
                return True
            return (
                isinstance(value, ast.Name)
                and value.id in sql_call_aliases.get(owner_name, set())
            )

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            value = node.value
            owner_name = owner_name_for(node)
            if value is not None and is_sql_method(value, owner_name):
                sql_call_aliases.setdefault(owner_name, set()).update(
                    target.id for target in targets if isinstance(target, ast.Name)
                )
            if not isinstance(value, ast.Constant) or type(value.value) is not str:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    bound_sql[(owner_name, target.id)] = value.value
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            owner_name = owner_name_for(node)
            if not (
                ast.unparse(node.func).endswith(
                    (".execute", ".executemany", ".executescript")
                )
                or (
                    isinstance(node.func, ast.Name)
                    and node.func.id in sql_call_aliases.get(owner_name, set())
                )
                or is_sql_method(node.func, owner_name)
            ):
                continue
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
                    "PRAGMA ",
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
    def test_closed_store_wrappers_are_retired_without_consumers(self) -> None:
        store_tree = _tree("tm_sqlite_store.py")
        defined = {
            node.name
            for node in store_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue(_RETIRED_STORE_WRAPPERS.isdisjoint(defined))

        consumers: set[tuple[str, str, str]] = set()
        paths = (
            *_ROOT.glob("*.py"),
            *(_ROOT / "tests").glob("*.py"),
            *(_ROOT / "tools").glob("*.py"),
        )
        for path in sorted(paths):
            relative = path.relative_to(_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            consumers.update(_retired_store_consumers_in_tree(relative, tree))
        self.assertEqual(consumers, set())

    def test_retired_store_consumer_scan_catches_aliases_and_patch_forms(self) -> None:
        symbol = "_candidate_projection_table_digest"
        tree = ast.parse(
            f'''\
import tm_sqlite_store as store
from tm_sqlite_store import {symbol}
store.{symbol}("tm_gram")
getattr(store, "{symbol}")
patch.object(store, "{symbol}")
patch("tm_sqlite_store.{symbol}")
'''
        )
        self.assertEqual(
            _retired_store_consumers_in_tree("tools/synthetic.py", tree),
            {
                ("tools/synthetic.py", "attribute", symbol),
                ("tools/synthetic.py", "getattr", symbol),
                ("tools/synthetic.py", "import", symbol),
                ("tools/synthetic.py", "patch", symbol),
                ("tools/synthetic.py", "patch.object", symbol),
            },
        )

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

def future_candidate_insert(connection, rows):
    query = "INSERT INTO tm_gram VALUES (?, ?, ?, ?)"
    connection.executemany(query, rows)

def future_candidate_schema(connection):
    connection.executescript("CREATE TABLE tm_candidate_future(value TEXT)")
'''
        )
        self.assertEqual(
            _sql_owners_in_tree("tm_sqlite_candidate_projection.py", tree),
            frozenset(
                {
                    "future_candidate_count",
                    "future_candidate_insert",
                    "future_candidate_schema",
                }
            ),
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
        indirect_tree = ast.parse(
            '''
def alias_query(connection, caller_query):
    executor = connection.execute
    return executor(caller_query)

def getattr_query(connection, caller_query):
    return getattr(connection, "execute")(caller_query)
'''
        )
        self.assertEqual(
            _sql_owners_in_tree(
                "tm_sqlite_candidate_projection.py",
                indirect_tree,
            ),
            frozenset({"<dynamic:alias_query>", "<dynamic:getattr_query>"}),
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

    def test_final_source_registries_and_evidence_are_frozen_and_current(
        self,
    ) -> None:
        gate_c = json.loads(
            (_ROOT / "tests/fixtures/retrieval_gate_c_roots_v1.json").read_text(
                encoding="utf-8"
            )
        )
        final_candidate_roots = {
            "tm_candidate_index.py",
            "tm_candidate_store_contracts.py",
            "tm_sqlite_candidate_projection.py",
            "tm_sqlite_store.py",
        }
        self.assertTrue(
            final_candidate_roots.issubset(gate_c["artifact_paths"])
        )
        self.assertTrue(final_candidate_roots.issubset(gate_c["build_paths"]))
        self.assertTrue(
            final_candidate_roots.issubset(BENCHMARK_IMPLEMENTATION_SOURCE_PATHS)
        )
        self.assertEqual(
            gate_c["artifact_digest"],
            aggregate_paths_digest(_ROOT, tuple(gate_c["artifact_paths"])),
        )
        self.assertEqual(
            gate_c["build_digest"],
            aggregate_paths_digest(_ROOT, tuple(gate_c["build_paths"])),
        )

        acceptance_paths = set(acceptance_matrix_source_paths())
        fault_paths = set(fault_matrix_source_paths())
        self.assertTrue(final_candidate_roots.issubset(acceptance_paths))
        self.assertTrue(final_candidate_roots.issubset(fault_paths))

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
            "20b2d02fa150945bf4b61e2ef3bff2720489df6542539adc11997b850be031c3",
        )
        self.assertEqual(
            fault["source_fingerprint"],
            "70d62edb5b5bae2a37bbfdfe29bf12ad568ec1da05f6341ab85c21896863a06f",
        )
        self.assertEqual(
            benchmark["implementation_fingerprint"],
            "0a71eca62f427b747db08384af6514a43c07d08b8be4641007ed7c15cf2e7217",
        )
        self.assertEqual(
            release["source_fingerprint"],
            "0a4020933de958b78be7117e223da222a1661f400e1b68f085ecb1aadfdc92f4",
        )
        self.assertTrue(benchmark["suite_report"]["passed"])
        self.assertEqual(benchmark["suite_report"]["failed_paths"], [])
        self.assertEqual(release["release_decision"], "GO")
        self.assertEqual(release["blocked_criteria"], [])
        self.assertEqual(
            release["input_evidence"]["acceptance_source_fingerprint"],
            acceptance["source_fingerprint"],
        )
        self.assertEqual(
            {item["path"] for item in acceptance["source_files"]},
            acceptance_paths,
        )
        self.assertEqual(
            {item["path"] for item in fault["source_files"]},
            fault_paths,
        )
        self.assertEqual(
            benchmark_implementation_fingerprint(_ROOT),
            benchmark["implementation_fingerprint"],
        )
        for evidence in (acceptance, fault, release):
            stale_paths = tuple(
                item["path"]
                for item in evidence["source_files"]
                if hashlib.sha256(
                    (_ROOT / item["path"]).read_bytes()
                ).hexdigest()
                != item["sha256"]
            )
            self.assertEqual(stale_paths, ())


if __name__ == "__main__":
    unittest.main()
