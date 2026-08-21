"""Wave 2 unit guards for the SQLite candidate read data plane."""

from __future__ import annotations

import ast
from pathlib import Path
import sqlite3
import unittest

import tm_sqlite_candidate_projection as projection
from tm_candidate_store_contracts import (
    SQLiteCandidateProofBlock,
    SQLiteStoreSchemaError,
)


_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED_PROJECTION_FUNCTION_SURFACE = (
    ("_candidate_projection_table_digest", ("table",)),
    ("_chunks", ("values",)),
    ("_finish_candidate_projection_digest", ("table_digests", "fts5_available")),
    ("_fts5_match_expression", ("trigrams",)),
    (
        "_prepare_streamed_candidate_records",
        ("candidate_records", "record_ids_by_ordinal"),
    ),
    ("_proof_int", ("value", "code")),
    ("_proof_text", ("value", "code")),
    ("_record_id", ("value", "code")),
    (
        "_update_candidate_gram_projection_digest",
        ("connection", "digest", "gram_chunk_rows"),
    ),
    ("_update_candidate_projection_digest", ("digest", "row")),
    (
        "_validate_candidate_proof_index_core",
        (
            "connection",
            "required_sizes",
            "fts5_available",
            "include_projection_digest",
            "gram_chunk_rows",
        ),
    ),
    ("_validate_candidate_proof_index_core.flush_block", ()),
    ("_validate_candidate_proof_index_core.proof_int", ("value",)),
    ("_validate_candidate_proof_index_core.proof_text", ("value",)),
    (
        "bounded_seed_stages",
        ("connection", "folded_query", "fts5_available", "seed_limit"),
    ),
    (
        "candidate_proof_block_records",
        ("connection", "folded_query", "block", "total_record_count"),
    ),
    (
        "candidate_proof_dense_phase1",
        ("connection", "folded_query", "blocks", "total_record_count"),
    ),
    (
        "candidate_proof_dense_phase2",
        (
            "connection",
            "total_record_count",
            "record_ids",
            "source_fold_lengths",
        ),
    ),
    (
        "candidate_proof_projection_digest",
        ("connection", "fts5_available", "gram_chunk_rows"),
    ),
    (
        "candidate_proof_query_block_uppers",
        ("connection", "query_terms"),
    ),
    ("candidate_proof_query_maxima_digest", ("blocks",)),
    (
        "candidate_proof_snapshot",
        (
            "connection",
            "folded_query",
            "seed_limit",
            "fts5_available",
            "total_record_count",
        ),
    ),
    (
        "candidate_recall_snapshot",
        (
            "connection",
            "fts5_available",
            "fts_query_trigrams",
            "query_grams_by_size",
            "candidate_floor",
            "fts_query_degenerate",
        ),
    ),
    ("fts5_candidate_ids", ("connection", "match_expression")),
    ("fts5_candidate_ids_for_trigrams", ("connection", "trigrams")),
    (
        "gram_candidate_overlaps",
        ("connection", "query_postings", "candidate_cap"),
    ),
    (
        "insert_candidate_fts_rows",
        (
            "connection",
            "plan",
            "record_ids_by_ordinal",
            "folded_sources_by_ordinal",
        ),
    ),
    (
        "insert_candidate_gram_rows",
        (
            "connection",
            "plan",
            "record_ids_by_ordinal",
            "folded_sources_by_ordinal",
        ),
    ),
    (
        "insert_streamed_candidate_fts_rows",
        (
            "connection",
            "candidate_records",
            "record_ids_by_ordinal",
            "fts5_available",
        ),
    ),
    (
        "insert_streamed_candidate_gram_rows",
        (
            "connection",
            "candidate_records",
            "record_ids_by_ordinal",
            "candidate_gram_facts",
            "gram_size",
        ),
    ),
    (
        "insert_streamed_candidate_proof_rows",
        (
            "connection",
            "candidate_records",
            "record_ids_by_ordinal",
            "expected_gram_row_count",
        ),
    ),
    (
        "maintain_candidate_proof_summaries",
        (
            "connection",
            "plan",
            "record_ids_by_ordinal",
            "folded_sources_by_ordinal",
        ),
    ),
    (
        "project_candidate_write_plan",
        ("plan", "record_ids_by_ordinal", "folded_sources_by_ordinal"),
    ),
    ("restore_streamed_stage_secondary_indexes", ("connection",)),
    ("streamed_stage_secondary_index_inventory", ("connection",)),
    ("suspend_streamed_stage_secondary_indexes", ("connection",)),
    (
        "validate_candidate_proof_blocks",
        ("connection", "blocks", "query_maxima_digest"),
    ),
    (
        "validate_candidate_proof_index",
        ("connection", "required_sizes", "fts5_available", "gram_chunk_rows"),
    ),
    (
        "validate_candidate_proof_index_with_digest",
        ("connection", "required_sizes", "fts5_available", "gram_chunk_rows"),
    ),
)


def _function_surface(tree: ast.Module) -> tuple[tuple[str, tuple[str, ...]], ...]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    surface: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = [node.name]
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(parent.name)
            parent = parents.get(parent)
        parameters = tuple(
            argument.arg
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        )
        if node.args.vararg is not None:
            parameters += (f"*{node.args.vararg.arg}",)
        if node.args.kwarg is not None:
            parameters += (f"**{node.args.kwarg.arg}",)
        surface.append((".".join(reversed(names)), parameters))
    return tuple(sorted(surface))


def _projection_authority_violations(tree: ast.Module) -> frozenset[str]:
    violations: set[str] = set()
    sql_methods = {"execute", "executemany", "executescript"}
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parameters = {
            argument.arg
            for argument in (
                *function.args.posonlyargs,
                *function.args.args,
                *function.args.kwonlyargs,
            )
        }
        for node in ast.walk(function):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr in sql_methods
                ):
                    violations.add(f"{function.name}:aliased-sql-method")
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in parameters:
                violations.add(f"{function.name}:caller-callable")
            if (
                isinstance(node.func, ast.Call)
                and isinstance(node.func.func, ast.Name)
                and node.func.func.id == "getattr"
                and len(node.func.args) >= 2
                and isinstance(node.func.args[1], ast.Constant)
                and node.func.args[1].value in sql_methods
            ):
                violations.add(f"{function.name}:dynamic-sql-method")
    return frozenset(violations)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE tm_record ("
        "record_id INTEGER PRIMARY KEY, source_fold_v1 TEXT NOT NULL, "
        "source_fold_length INTEGER NOT NULL);"
        "CREATE TABLE tm_gram ("
        "record_id INTEGER NOT NULL, gram_size INTEGER NOT NULL, "
        "gram TEXT NOT NULL, term_frequency INTEGER NOT NULL);"
        "CREATE TABLE tm_gram_block_max ("
        "block_id INTEGER NOT NULL, gram_size INTEGER NOT NULL, "
        "gram TEXT NOT NULL, max_term_frequency INTEGER NOT NULL);"
        "CREATE TABLE tm_candidate_block ("
        "block_id INTEGER PRIMARY KEY, first_record_id INTEGER NOT NULL, "
        "last_record_id INTEGER NOT NULL, record_count INTEGER NOT NULL, "
        "min_source_fold_length INTEGER NOT NULL, "
        "max_source_fold_length INTEGER NOT NULL);"
    )
    connection.executemany(
        "INSERT INTO tm_record VALUES (?, ?, ?)",
        ((1, "aba", 3), (2, "abb", 3)),
    )
    grams = (
        (1, 1, "a", 2),
        (1, 1, "b", 1),
        (1, 2, "ab", 1),
        (1, 2, "ba", 1),
        (1, 3, "aba", 1),
        (2, 1, "a", 1),
        (2, 1, "b", 2),
        (2, 2, "ab", 1),
        (2, 2, "bb", 1),
        (2, 3, "abb", 1),
    )
    connection.executemany("INSERT INTO tm_gram VALUES (?, ?, ?, ?)", grams)
    maxima: dict[tuple[int, str], int] = {}
    for _record_id, gram_size, gram, frequency in grams:
        key = (gram_size, gram)
        maxima[key] = max(maxima.get(key, 0), frequency)
    connection.executemany(
        "INSERT INTO tm_gram_block_max VALUES (0, ?, ?, ?)",
        tuple((size, gram, frequency) for (size, gram), frequency in maxima.items()),
    )
    connection.execute(
        "INSERT INTO tm_candidate_block VALUES (0, 1, 256, 2, 3, 3)"
    )
    connection.commit()
    return connection


class CandidateProjectionArchitectureTests(unittest.TestCase):
    def test_imports_and_executable_calls_stay_inside_the_data_plane(self) -> None:
        source = (_ROOT / "tm_sqlite_candidate_projection.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
        self.assertEqual(
            imports,
            {
                "__future__",
                "collections",
                "hashlib",
                "json",
                "sqlite3",
                "text_matcher",
                "tm_candidate_store_contracts",
                "typing",
            },
        )
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        self.assertNotIn("sqlite3.connect", calls)
        self.assertFalse(
            {
                call
                for call in calls
                if call.endswith((".connect", ".commit", ".rollback"))
            }
        )
        transaction_sql = {
            node.value.strip().upper()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and type(node.value) is str
            if node.value.strip().upper()
            in {"BEGIN", "BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"}
        }
        self.assertEqual(transaction_sql, set())
        self.assertNotIn("tm_contracts", source)
        self.assertNotIn("coordinator", source)

        text_matcher_imports = tuple(
            (item.name, item.asname)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "text_matcher"
            for item in node.names
        )
        self.assertEqual(
            text_matcher_imports,
            (("fold_text_value_v1", None),),
        )

    def test_projection_function_and_execution_surface_is_closed(self) -> None:
        tree = ast.parse(
            (_ROOT / "tm_sqlite_candidate_projection.py").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            _function_surface(tree),
            _EXPECTED_PROJECTION_FUNCTION_SURFACE,
        )
        self.assertEqual(_projection_authority_violations(tree), frozenset())

        hostile = ast.parse(
            '''
def future_candidate_alias(connection, caller_query):
    executor = connection.execute
    return executor(caller_query)

def future_candidate_getattr(connection, caller_query):
    return getattr(connection, "execute")(caller_query)

def future_candidate_callback(connection, callback):
    callback(connection)
'''
        )
        self.assertEqual(
            _projection_authority_violations(hostile),
            frozenset(
                {
                    "future_candidate_alias:aliased-sql-method",
                    "future_candidate_callback:caller-callable",
                    "future_candidate_getattr:dynamic-sql-method",
                }
            ),
        )

    def test_projection_defines_no_authority_or_intermediate_dto_class(self) -> None:
        source = (_ROOT / "tm_sqlite_candidate_projection.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        self.assertEqual(
            tuple(node.name for node in tree.body if isinstance(node, ast.ClassDef)),
            (),
        )
        self.assertNotIn("SQLiteCandidateRecallSnapshot", source)
        self.assertNotIn("SQLiteCandidateProofSnapshot", source)
        self.assertNotIn("receipt", source)
        self.assertNotIn("binding_digest", source)

    def test_streamed_maxima_scan_is_physically_rowid_bounded(self) -> None:
        tree = ast.parse(
            (_ROOT / "tm_sqlite_candidate_projection.py").read_text(
                encoding="utf-8"
            )
        )
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "insert_streamed_candidate_proof_rows"
        )
        sql_literals = tuple(
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Constant) and type(node.value) is str
        )
        maxima_sql = next(
            value
            for value in sql_literals
            if "INSERT INTO tm_gram_block_max" in value
        )
        self.assertIn("FROM tm_gram NOT INDEXED", maxima_sql)
        self.assertIn("WHERE rowid BETWEEN ? AND ?", maxima_sql)

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE tm_gram (gram_size INTEGER NOT NULL, "
            "gram TEXT NOT NULL, record_id INTEGER NOT NULL, "
            "term_frequency INTEGER NOT NULL, "
            "PRIMARY KEY(gram_size, gram, record_id))"
        )
        connection.execute(
            "CREATE INDEX idx_tm_gram_lookup "
            "ON tm_gram(gram_size, gram, record_id)"
        )
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT gram_size, gram, record_id, "
            "term_frequency FROM tm_gram NOT INDEXED "
            "WHERE rowid BETWEEN ? AND ? AND record_id BETWEEN ? AND ? "
            "AND gram_size IN (1, 2)",
            (10, 20, 3, 7),
        ).fetchall()
        details = " ".join(str(row[3]).upper() for row in plan)
        self.assertIn("INTEGER PRIMARY KEY", details)
        self.assertIn("ROWID>?", details)
        self.assertIn("ROWID<?", details)


class CandidateProjectionReadTests(unittest.TestCase):
    def test_fts5_single_and_chunked_union_queries_return_sorted_ids(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE VIRTUAL TABLE tm_fts USING fts5("
            "record_id UNINDEXED, source_fold_v1, tokenize='trigram')"
        )
        connection.executemany(
            "INSERT INTO tm_fts(record_id, source_fold_v1) VALUES (?, ?)",
            ((2, "abcxyz"), (1, "abcdef"), (3, "defghi")),
        )
        self.assertEqual(
            projection.fts5_candidate_ids(connection, '"abc"'),
            (1, 2),
        )
        self.assertEqual(
            projection.fts5_candidate_ids_for_trigrams(
                connection, ("abc", "def")
            ),
            (1, 2, 3),
        )

    def test_fallback_recall_overlap_and_transaction_authority(self) -> None:
        connection = _connection()
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO tm_record VALUES (3, 'sentinel', 8)"
        )
        self.assertTrue(connection.in_transaction)

        overlaps = projection.gram_candidate_overlaps(
            connection,
            ((1, "a"), (1, "b"), (2, "ab")),
            candidate_cap=2,
        )
        stage_matches, folded_sources = projection.candidate_recall_snapshot(
            connection,
            fts5_available=False,
            fts_query_trigrams=("aba",),
            query_grams_by_size=((3, ("aba",)), (2, ("ab", "ba")), (1, ("a", "b"))),
            candidate_floor=2,
            fts_query_degenerate=False,
        )

        self.assertEqual(overlaps, ((1, 3), (2, 3)))
        self.assertEqual(
            stage_matches,
            (
                ("GRAM_3", ((1, 1),)),
                ("GRAM_2", ((1, 2), (2, 1))),
                ("GRAM_1", ((1, 2), (2, 2))),
            ),
        )
        self.assertEqual(set(folded_sources), {(1, "aba"), (2, "abb")})
        self.assertTrue(connection.in_transaction)
        connection.rollback()
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM tm_record").fetchone(),
            (2,),
        )

    def test_proof_snapshot_sparse_and_dense_rows_match_existing_facts(self) -> None:
        connection = _connection()
        self.addCleanup(connection.close)
        seed_stages, blocks, query_maxima_digest = (
            projection.candidate_proof_snapshot(
                connection,
                folded_query="aba",
                seed_limit=8,
                fts5_available=False,
                total_record_count=2,
            )
        )
        block = blocks[0]
        records = projection.candidate_proof_block_records(
            connection,
            folded_query="aba",
            block=block,
            total_record_count=2,
        )
        projection.validate_candidate_proof_blocks(
            connection,
            blocks=blocks,
            query_maxima_digest=query_maxima_digest,
        )
        phase1 = projection.candidate_proof_dense_phase1(
            connection,
            folded_query="aba",
            blocks=blocks,
            total_record_count=2,
        )
        phase2 = projection.candidate_proof_dense_phase2(
            connection,
            total_record_count=2,
            record_ids=(2,),
            source_fold_lengths=(3,),
        )

        self.assertEqual(seed_stages[0], ("GRAM_3", (1,)))
        self.assertEqual(
            (block.character_intersection_upper, block.bigram_intersection_upper),
            (3, 2),
        )
        self.assertEqual(
            tuple(
                (
                    record.record_id,
                    record.character_multiset_intersection,
                    record.bigram_multiset_intersection,
                )
                for record in records
            ),
            ((1, 3, 2), (2, 2, 1)),
        )
        self.assertEqual(phase1, ((3, 3), (2, 1)))
        self.assertEqual(phase2, ((2,), ("abb",), (3,)))

    def test_stable_invalid_row_codes_and_programmer_fault_propagate(self) -> None:
        class Cursor:
            def fetchall(self) -> list[tuple[object, ...]]:
                return [("not-an-id",)]

        class InvalidRows:
            def execute(self, *_args: object) -> Cursor:
                return Cursor()

        with self.assertRaisesRegex(
            SQLiteStoreSchemaError, "STORE.FTS5_RESULT_INVALID"
        ):
            projection.fts5_candidate_ids(InvalidRows(), '"abc"')  # type: ignore[arg-type]

        class SentinelError(RuntimeError):
            pass

        sentinel = SentinelError("programmer fault")

        class HostileConnection:
            def execute(self, *_args: object) -> None:
                raise sentinel

        with self.assertRaises(SentinelError) as raised:
            projection.fts5_candidate_ids(
                HostileConnection(),  # type: ignore[arg-type]
                '"abc"',
            )
        self.assertIs(raised.exception, sentinel)


if __name__ == "__main__":
    unittest.main()
