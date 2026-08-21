"""Wave 1 guards for the neutral TM candidate storage contracts."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

import tm_candidate_store_contracts as contracts
import tm_sqlite_store as store_module
from tm_candidate_index import (
    CandidateRetriever,
    FTS5TrigramIndex,
    GramPostingIndex,
)


_ROOT = Path(__file__).resolve().parents[1]
_MOVED_NAMES = (
    "CANDIDATE_INDEX_VERSION",
    "CANDIDATE_PROOF_BLOCK_SIZE",
    "CANDIDATE_PROOF_BLOCK_VERSION_V1",
    "CandidateProofIndexError",
    "SQLiteCandidateProofBlock",
    "SQLiteCandidateProofDensePhase1",
    "SQLiteCandidateProofDensePhase2",
    "SQLiteCandidateProofRecord",
    "SQLiteCandidateProofSnapshot",
    "SQLiteCandidateRecallSnapshot",
    "SQLiteCandidateRecord",
    "SQLiteCandidateWritePlan",
    "SQLiteGramRow",
    "SQLiteStoreSchemaError",
    "_CANDIDATE_PROOF_DENSE_RECEIPT_FACTORY_KEY",
    "_SQLiteCandidateProofDenseReceipt",
    "build_candidate_write_plan",
    "character_ngram_frequencies",
    "unique_character_ngrams",
)


def _tree(relative: str) -> ast.Module:
    return ast.parse((_ROOT / relative).read_text(encoding="utf-8"))


class _RecallPort:
    def __init__(
        self,
        *,
        resource_id: str = "tm.fake",
        scope: str = "STORE",
        snapshot: object | None = None,
    ) -> None:
        self.resource_id = resource_id
        self.candidate_port_scope = scope
        self.snapshot = (
            contracts.SQLiteCandidateRecallSnapshot(False, (), ())
            if snapshot is None
            else snapshot
        )
        self.calls = 0

    def candidate_recall_snapshot(self, **_kwargs: object) -> object:
        self.calls += 1
        return self.snapshot

    def fts5_candidate_ids(self, _expression: str) -> tuple[int, ...] | None:
        return ()

    def fts5_candidate_ids_for_trigrams(
        self,
        _trigrams: tuple[str, ...],
    ) -> tuple[int, ...] | None:
        return ()

    def gram_candidate_overlaps(
        self,
        _query_postings: tuple[tuple[int, str], ...],
        *,
        candidate_cap: int,
    ) -> tuple[tuple[int, int], ...]:
        del candidate_cap
        return ()


class _PostingPort:
    resource_id = "tm.fake"
    candidate_port_scope = "STORE"

    def __init__(self) -> None:
        self.gram_calls = 0

    def fts5_candidate_ids(self, _expression: str) -> tuple[int, ...] | None:
        return ()

    def fts5_candidate_ids_for_trigrams(
        self,
        _trigrams: tuple[str, ...],
    ) -> tuple[int, ...] | None:
        return ()

    def gram_candidate_overlaps(
        self,
        _query_postings: tuple[tuple[int, str], ...],
        *,
        candidate_cap: int,
    ) -> tuple[tuple[int, int], ...]:
        self.gram_calls += 1
        self.last_cap = candidate_cap
        return ()


class CandidateStoreContractArchitectureTests(unittest.TestCase):
    def test_leaf_imports_only_the_approved_standard_library_modules(self) -> None:
        imports: set[str] = set()
        for node in ast.walk(_tree("tm_candidate_store_contracts.py")):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
        self.assertEqual(imports, {"__future__", "dataclasses", "typing"})

    def test_store_reexports_the_exact_leaf_objects(self) -> None:
        for name in _MOVED_NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(store_module, name), getattr(contracts, name))

    def test_algorithm_has_no_concrete_store_or_projection_dependency(self) -> None:
        imported_modules = {
            node.module
            for node in ast.walk(_tree("tm_candidate_index.py"))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertIn("tm_candidate_store_contracts", imported_modules)
        self.assertNotIn("tm_sqlite_store", imported_modules)
        self.assertNotIn("tm_sqlite_candidate_projection", imported_modules)

    def test_store_no_longer_defines_moved_authorities(self) -> None:
        defined = {
            node.name
            for node in _tree("tm_sqlite_store.py").body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assigned = {
            target.id
            for node in _tree("tm_sqlite_store.py").body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else (node.target,)
            )
            if isinstance(target, ast.Name)
        }
        self.assertTrue(set(_MOVED_NAMES).isdisjoint(defined | assigned))

    def test_algorithm_validates_dense_dto_before_view_binding(self) -> None:
        tree = _tree("tm_candidate_index.py")
        candidate_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "CandidateProofSession"
        )
        for method_name, validator_name in (
            ("_load_dense_frontier", "validate_candidate_proof_dense_phase1_result"),
            ("_refine_dense_frontier", "validate_candidate_proof_dense_phase2_result"),
        ):
            method = next(
                node
                for node in candidate_class.body
                if isinstance(node, ast.FunctionDef) and node.name == method_name
            )
            calls = tuple(
                ast.unparse(node.func)
                for node in sorted(
                    (item for item in ast.walk(method) if isinstance(item, ast.Call)),
                    key=lambda item: (item.lineno, item.col_offset),
                )
            )
            self.assertLess(
                calls.index(validator_name),
                calls.index(f"self._view.{validator_name}"),
            )


class CandidateStorePortTests(unittest.TestCase):
    def test_hostile_port_property_fault_is_not_relabelled_or_swallowed(self) -> None:
        class SentinelError(RuntimeError):
            pass

        sentinel = SentinelError("programmer fault")

        class HostilePropertyPort(_RecallPort):
            def __init__(self) -> None:
                self.candidate_port_scope = "STORE"
                self.snapshot = contracts.SQLiteCandidateRecallSnapshot(False, (), ())
                self.calls = 0

            @property
            def resource_id(self) -> str:
                raise sentinel

        with self.assertRaises(SentinelError) as raised:
            CandidateRetriever().candidates(
                "tm.fake",
                HostilePropertyPort(),
                "abc",
                result_limit=10,
            )
        self.assertIs(raised.exception, sentinel)

    def test_hostile_port_callable_fault_is_not_relabelled_or_swallowed(self) -> None:
        class SentinelError(RuntimeError):
            pass

        sentinel = SentinelError("programmer fault")

        class HostileCallablePort(_RecallPort):
            def candidate_recall_snapshot(self, **_kwargs: object) -> object:
                self.calls += 1
                raise sentinel

        port = HostileCallablePort()
        with self.assertRaises(SentinelError) as raised:
            CandidateRetriever().candidates(
                "tm.fake",
                port,
                "abc",
                result_limit=10,
            )
        self.assertIs(raised.exception, sentinel)
        self.assertEqual(port.calls, 1)

    def test_structural_recall_port_is_accepted_and_called_once(self) -> None:
        port = _RecallPort()
        report = CandidateRetriever().candidates(
            "tm.fake",
            port,
            "",
            result_limit=10,
        )
        self.assertEqual(port.calls, 1)
        self.assertEqual(report.candidates, ())

    def test_recall_identity_scope_and_behavior_fail_before_storage(self) -> None:
        for port, error in (
            (_RecallPort(resource_id="tm.other"), ValueError),
            (_RecallPort(scope="QUERY_VIEW"), TypeError),
            (object(), TypeError),
        ):
            with self.subTest(port=type(port).__name__, error=error.__name__):
                with self.assertRaises(error):
                    CandidateRetriever().candidates(
                        "tm.fake",
                        port,
                        "abc",
                        result_limit=10,
                    )
                self.assertEqual(getattr(port, "calls", 0), 0)

    def test_forged_snapshot_subtype_is_rejected_after_one_storage_call(self) -> None:
        class SnapshotSubtype(contracts.SQLiteCandidateRecallSnapshot):
            pass

        port = _RecallPort(snapshot=SnapshotSubtype(False, (), ()))
        with self.assertRaisesRegex(
            contracts.SQLiteStoreSchemaError,
            "STORE.CANDIDATE_EVIDENCE_INVALID",
        ):
            CandidateRetriever().candidates(
                "tm.fake",
                port,
                "abc",
                result_limit=10,
            )
        self.assertEqual(port.calls, 1)

    def test_structural_posting_port_is_accepted_and_scope_is_checked(self) -> None:
        port = _PostingPort()
        result = GramPostingIndex(fts5_available=False).candidates(
            port,
            "a",
            limit=10,
        )
        self.assertEqual(result.record_ids, ())
        self.assertEqual(port.gram_calls, 1)
        self.assertEqual(port.last_cap, 10)

        port.candidate_port_scope = "QUERY_VIEW"
        with self.assertRaises(TypeError):
            FTS5TrigramIndex(available=True).candidates(port, "abc")

    def test_forged_posting_values_are_rejected_before_hash_or_sort(self) -> None:
        class IntSubtype(int):
            def __hash__(self) -> int:
                raise AssertionError("forged record id was hashed")

        class ForgedPostingPort(_PostingPort):
            def fts5_candidate_ids(
                self,
                _expression: str,
            ) -> tuple[int, ...] | None:
                return (IntSubtype(1),)

            def gram_candidate_overlaps(
                self,
                _query_postings: tuple[tuple[int, str], ...],
                *,
                candidate_cap: int,
            ) -> tuple[tuple[int, int], ...]:
                del candidate_cap
                return ((IntSubtype(1), 1),)

        port = ForgedPostingPort()
        with self.assertRaisesRegex(
            contracts.SQLiteStoreSchemaError,
            "STORE.CANDIDATE_EVIDENCE_INVALID",
        ):
            FTS5TrigramIndex(available=True).candidates(port, "abc")
        with self.assertRaisesRegex(
            contracts.SQLiteStoreSchemaError,
            "STORE.CANDIDATE_EVIDENCE_INVALID",
        ):
            GramPostingIndex(fts5_available=False).candidates(
                port,
                "a",
                limit=10,
            )

    def test_proof_port_requires_dense_validation_behavior_before_query(self) -> None:
        port = _RecallPort(scope="QUERY_VIEW")
        with self.assertRaises(TypeError):
            CandidateRetriever().proof_session_from_view(
                "tm.fake",
                port,
                "abc",
                minimum_similarity=0.5,
                result_limit=10,
            )
        self.assertEqual(port.calls, 0)


if __name__ == "__main__":
    unittest.main()
