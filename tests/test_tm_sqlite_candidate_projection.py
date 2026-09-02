"""Wave 2 unit guards for the SQLite candidate read data plane."""

from __future__ import annotations

import ast
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

import tm_sqlite_candidate_projection as projection
from tm_candidate_store_contracts import (
    CandidateProofIndexError,
    SQLiteCandidateProofBlock,
    SQLiteStoreSchemaError,
    character_ngram_frequencies,
)
from text_matcher import fold_text_value_v1


_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED_PROJECTION_FUNCTION_SURFACE = (
    ("_candidate_projection_table_digest", ("table",)),
    ("_chunks", ("values",)),
    ("_finish_candidate_projection_digest", ("table_digests", "fts5_available")),
    ("_fts5_match_expression", ("trigrams",)),
    (
        "_insert_generated_streamed_candidate_gram_rows",
        ("connection", "prepared", "gram_size"),
    ),
    (
        "_insert_prepared_streamed_candidate_fts_rows",
        ("connection", "prepared", "fts5_available"),
    ),
    (
        "_insert_prepared_streamed_candidate_gram_rows",
        ("connection", "prepared", "candidate_gram_facts", "gram_size"),
    ),
    (
        "_insert_prepared_streamed_candidate_proof_rows",
        ("connection", "prepared", "expected_gram_row_count"),
    ),
    (
        "_insert_streamed_candidate_projection",
        (
            "connection",
            "candidate_records",
            "record_ids_by_ordinal",
            "fts5_available",
        ),
    ),
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
    ("_validate_candidate_proof_index_core.batched_rows", ("cursor",)),
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


def _proof_validation_connection() -> sqlite3.Connection:
    connection = _connection()
    connection.execute("ALTER TABLE tm_record ADD COLUMN source_raw TEXT")
    connection.execute(
        "UPDATE tm_record SET source_raw = source_fold_v1"
    )
    connection.execute(
        "DELETE FROM tm_gram_block_max WHERE gram_size = 3"
    )
    connection.commit()
    return connection


class CandidateProjectionArchitectureTests(unittest.TestCase):
    def test_streamed_exported_wrappers_keep_raw_input_validation(self) -> None:
        connection = _connection()
        self.addCleanup(connection.close)
        invalid_records: object = []
        with self.assertRaisesRegex(TypeError, "candidate_records"):
            projection.insert_streamed_candidate_gram_rows(
                connection,
                invalid_records,  # type: ignore[arg-type]
                (),
                (),
                gram_size=1,
            )
        with self.assertRaisesRegex(TypeError, "candidate_records"):
            projection.insert_streamed_candidate_fts_rows(
                connection,
                invalid_records,  # type: ignore[arg-type]
                (),
                fts5_available=False,
            )
        with self.assertRaisesRegex(TypeError, "candidate_records"):
            projection.insert_streamed_candidate_proof_rows(
                connection,
                invalid_records,  # type: ignore[arg-type]
                (),
                expected_gram_row_count=0,
            )

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
            and node.name == "_insert_prepared_streamed_candidate_proof_rows"
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
    def test_validator_direct_grams_match_canonical_helper(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript(
            "CREATE TABLE tm_record ("
            "record_id INTEGER PRIMARY KEY, source_raw TEXT NOT NULL, "
            "source_fold_v1 TEXT NOT NULL, source_fold_length INTEGER NOT NULL);"
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
        raw_sources = ("x", "xy", "abab", "ÉéÉ")
        gram_rows: list[tuple[int, int, str, int]] = []
        maxima: dict[tuple[int, str], int] = {}
        lengths: list[int] = []
        for record_id, raw_source in enumerate(raw_sources, start=1):
            folded_source = fold_text_value_v1(raw_source)
            lengths.append(len(folded_source))
            connection.execute(
                "INSERT INTO tm_record VALUES (?, ?, ?, ?)",
                (record_id, raw_source, folded_source, len(folded_source)),
            )
            for size in (1, 2, 3):
                for gram, frequency in character_ngram_frequencies(
                    folded_source,
                    size,
                ):
                    gram_rows.append((record_id, size, gram, frequency))
                    if size in {1, 2}:
                        key = (size, gram)
                        maxima[key] = max(maxima.get(key, 0), frequency)
        connection.executemany(
            "INSERT INTO tm_gram VALUES (?, ?, ?, ?)",
            gram_rows,
        )
        connection.executemany(
            "INSERT INTO tm_gram_block_max VALUES (0, ?, ?, ?)",
            tuple(
                (size, gram, frequency)
                for (size, gram), frequency in maxima.items()
            ),
        )
        connection.execute(
            "INSERT INTO tm_candidate_block VALUES (0, 1, 256, ?, ?, ?)",
            (len(raw_sources), min(lengths), max(lengths)),
        )
        connection.commit()
        expected_counts = tuple(
            (size, sum(1 for row in gram_rows if row[1] == size))
            for size in (1, 2, 3)
        )

        with mock.patch.object(
            projection,
            "character_ngram_frequencies",
            side_effect=AssertionError("validator must use direct gram counting"),
        ):
            self.assertEqual(
                projection.validate_candidate_proof_index(
                    connection,
                    required_sizes=(1, 2, 3),
                    fts5_available=False,
                ),
                (expected_counts, 0),
            )

    def test_gram_duplicate_and_mismatch_preserve_error_precedence(
        self,
    ) -> None:
        duplicate = _proof_validation_connection()
        self.addCleanup(duplicate.close)
        duplicate.execute(
            "INSERT INTO tm_gram(record_id, gram_size, gram, term_frequency) "
            "VALUES (1, 1, 'a', 2)"
        )
        duplicate.execute(
            "INSERT INTO tm_gram(record_id, gram_size, gram, term_frequency) "
            "VALUES (1, 'bad-size', 'later', 1)"
        )
        with self.assertRaisesRegex(
            CandidateProofIndexError,
            "^candidate gram fact is invalid$",
        ):
            projection.validate_candidate_proof_index(
                duplicate,
                required_sizes=(1, 2, 3),
                fts5_available=False,
            )

        mismatch = _proof_validation_connection()
        self.addCleanup(mismatch.close)
        mismatch.execute(
            "UPDATE tm_gram SET term_frequency = term_frequency + 1 "
            "WHERE record_id = 1 AND gram_size = 1 AND gram = 'a'"
        )
        mismatch.execute(
            "INSERT INTO tm_gram(record_id, gram_size, gram, term_frequency) "
            "VALUES (1, 'bad-size', 'later', 1)"
        )
        with self.assertRaisesRegex(
            CandidateProofIndexError,
            "^candidate integer fact is invalid$",
        ):
            projection.validate_candidate_proof_index(
                mismatch,
                required_sizes=(1, 2, 3),
                fts5_available=False,
            )

    def test_projection_digest_preserves_v2_bytes_across_chunk_boundaries(
        self,
    ) -> None:
        connection = _connection()
        self.addCleanup(connection.close)
        expected_without_fts = {
            1: (
                "61ca0ced6519efa8b3c4d2ecac7965f7"
                "4dd513f35c07dcce5621654d24ea03e4"
            ),
            2: (
                "bb72a73938dd3a88fa4038da67f64636"
                "8d358044309b095f6c20423c8ea037d6"
            ),
            50_000: (
                "be50776b7f9c703d8bc4c0b672b24816"
                "099b0eea5b920baf0a84f3a9d66184eb"
            ),
        }
        for chunk_rows, expected in expected_without_fts.items():
            with self.subTest(fts5_available=False, chunk_rows=chunk_rows):
                self.assertEqual(
                    projection.candidate_proof_projection_digest(
                        connection,
                        fts5_available=False,
                        gram_chunk_rows=chunk_rows,
                    ),
                    expected,
                )

        connection.execute(
            "CREATE TABLE tm_fts(record_id INTEGER, source_fold_v1 TEXT)"
        )
        connection.executemany(
            "INSERT INTO tm_fts VALUES (?, ?)",
            ((1, "aba"), (2, "abb")),
        )
        expected_with_fts = {
            1: (
                "0b32660d8bf4389d32a92be30c9c7eab"
                "d74c6c609c1074e248574ec42ce715b5"
            ),
            2: (
                "5e459d274d196d5d0cdfc4d19565eace"
                "315233ecd67ab759a02fe2846486be06"
            ),
            50_000: (
                "97215c21b096d7f10b5ae1ddbdd7827e"
                "ecca0c32bcdf620b389d336079e6b1fc"
            ),
        }
        for chunk_rows, expected in expected_with_fts.items():
            with self.subTest(fts5_available=True, chunk_rows=chunk_rows):
                self.assertEqual(
                    projection.candidate_proof_projection_digest(
                        connection,
                        fts5_available=True,
                        gram_chunk_rows=chunk_rows,
                    ),
                    expected,
                )

    def test_gram_digest_hex_elides_cast_without_payload_drift(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE payload_values("
            "record_id, gram_size, gram, term_frequency)"
        )
        connection.executemany(
            "INSERT INTO payload_values VALUES (?, ?, ?, ?)",
            (
                (1, 2, 3, 4),
                ("1", "2", "gram", "4"),
                (None, None, None, None),
                (
                    sqlite3.Binary(b"\x00\xff"),
                    sqlite3.Binary(b"\x01\xfe"),
                    sqlite3.Binary(b"\x02\xfd"),
                    sqlite3.Binary(b"\x03\xfc"),
                ),
                (1.5, 2.5, 3.5, 4.5),
            ),
        )
        columns = (
            "rowid",
            "record_id",
            "gram_size",
            "gram",
            "term_frequency",
        )
        old_payload = "json_array(" + ", ".join(
            item
            for column in columns
            for item in (
                f"typeof({column})",
                f"hex(CAST({column} AS BLOB))",
            )
        ) + ")"
        new_payload = "json_array(" + ", ".join(
            item
            for column in columns
            for item in (f"typeof({column})", f"hex({column})")
        ) + ")"
        rows = connection.execute(
            f"SELECT CAST({old_payload} AS BLOB), "
            f"CAST({new_payload} AS BLOB), typeof(record_id) "
            "FROM payload_values ORDER BY rowid"
        ).fetchall()
        self.assertEqual(
            tuple(row[2] for row in rows),
            ("integer", "text", "null", "blob", "real"),
        )
        self.assertEqual(
            tuple(row[0] for row in rows),
            tuple(row[1] for row in rows),
        )

    def test_projection_digest_empty_gram_path_and_tamper_remain_exact(
        self,
    ) -> None:
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        empty.executescript(
            "CREATE TABLE tm_gram(record_id INTEGER, gram_size INTEGER, "
            "gram TEXT, term_frequency INTEGER);"
            "CREATE TABLE tm_candidate_block("
            "block_id INTEGER, first_record_id INTEGER, last_record_id INTEGER, "
            "record_count INTEGER, min_source_fold_length INTEGER, "
            "max_source_fold_length INTEGER);"
            "CREATE TABLE tm_gram_block_max("
            "block_id INTEGER, gram_size INTEGER, gram TEXT, "
            "max_term_frequency INTEGER);"
        )
        empty_digest = projection.candidate_proof_projection_digest(
            empty,
            fts5_available=False,
            gram_chunk_rows=1,
        )
        self.assertEqual(
            empty_digest,
            "fa799b02f752b48ca3d3e3275b16a288"
            "8e2e58f126a5b5e4976f4429da253447",
        )

        connection = _connection()
        self.addCleanup(connection.close)
        baseline = projection.candidate_proof_projection_digest(
            connection,
            fts5_available=False,
            gram_chunk_rows=2,
        )
        connection.execute(
            "UPDATE tm_gram SET term_frequency = term_frequency + 1 "
            "WHERE rowid = (SELECT MIN(rowid) FROM tm_gram)"
        )
        tampered = projection.candidate_proof_projection_digest(
            connection,
            fts5_available=False,
            gram_chunk_rows=2,
        )
        self.assertNotEqual(tampered, baseline)
        connection.rollback()
        self.assertEqual(
            projection.candidate_proof_projection_digest(
                connection,
                fts5_available=False,
                gram_chunk_rows=2,
            ),
            baseline,
        )

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
