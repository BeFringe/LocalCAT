from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import termbase_store as termbase_store_module
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceConfig,
    ResourceKind,
    TermCleanupReport,
    TermCommitState,
    TermDraft,
    TermRecord,
    TermRowKind,
)
from editor_controller import EditorController, EditorControllerError
from qt_editor import _compose_editor_controller
from resource_repository import ResourceRepository
from termbase_store import TermbaseStore


class EditorControllerTermTransactionTests(unittest.TestCase):
    def _controller(
        self,
        root: Path,
        *payloads: bytes,
    ) -> tuple[EditorController, tuple[ResourceConfig, ...]]:
        repository = ResourceRepository(root / "app-data")
        resources: list[ResourceConfig] = []
        for ordinal, payload in enumerate(payloads, start=1):
            resource = repository.create_resource(
                f"Terms {ordinal}",
                ResourceKind.TERMBASE,
            )
            resource.path.write_bytes(payload)
            resources.append(resource)
        controller, _ = _compose_editor_controller(repository)
        controller.set_project(
            EditorProject(
                name="Terms",
                segments=(
                    EditorSegment(
                        id="one",
                        source="Legacy Configured Other Fresh",
                    ),
                ),
            )
        )
        return controller, tuple(resources)

    @staticmethod
    def _term_engines(controller: EditorController) -> dict[str, object]:
        return dict(cast(Any, controller)._glossary_engines)

    @staticmethod
    def _private_term_records(
        controller: EditorController,
    ) -> dict[str, tuple[TermRecord, ...]]:
        return dict(cast(Any, controller)._term_record_snapshots)

    @staticmethod
    def _artifact_paths(resource: ResourceConfig) -> tuple[Path, ...]:
        return tuple(
            sorted(
                resource.path.parent.glob(f".{resource.path.name}.*"),
                key=lambda candidate: candidate.name,
            )
        )

    def test_crud_commits_mixed_rows_and_atomically_replaces_full_engine_graph(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, resources = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
                b"Other,other-old\n",
            )
            first, second = resources
            before_engines = self._term_engines(controller)

            created = controller.create_term(
                first.id,
                TermDraft("Configured", "configured-new"),
            )
            after_create_engines = self._term_engines(controller)
            created_records = controller.list_terms(first.id)
            private_created_records = self._private_term_records(controller)[
                first.id
            ]
            configured = created_records[1]
            after_create_terms = controller.term_suggestions()

            updated = controller.update_term(
                first.id,
                configured.locator,
                TermDraft(
                    "Configured",
                    "configured-updated",
                    match_case=True,
                    whole_word=False,
                ),
            )
            updated_records = controller.list_terms(first.id)
            updated_configured = updated_records[1]
            after_update_terms = controller.term_suggestions()
            deleted = controller.delete_term(
                first.id,
                updated_configured.locator,
            )
            final_records = controller.list_terms(first.id)
            after_delete_terms = controller.term_suggestions()
            artifacts = self._artifact_paths(first)

        self.assertIs(created.state, TermCommitState.COMMITTED)
        self.assertIsNotNone(created.report)
        assert created.report is not None
        self.assertEqual(created.report.records, created_records)
        self.assertTrue(
            all(
                report_record is not private_record
                and report_record.locator is not private_record.locator
                for report_record, private_record in zip(
                    created.report.records,
                    private_created_records,
                    strict=True,
                )
            )
        )
        self.assertTrue(
            all(
                public_record is not private_record
                and public_record.locator is not private_record.locator
                for public_record, private_record in zip(
                    created_records,
                    private_created_records,
                    strict=True,
                )
            )
        )
        self.assertIs(configured.locator.row_kind, TermRowKind.V1)
        self.assertFalse(configured.match_case)
        self.assertTrue(configured.whole_word)
        self.assertIn(
            ("Configured", "configured-new"),
            tuple(
                (suggestion.source_term, suggestion.target_term)
                for suggestion in after_create_terms
            ),
        )
        self.assertIs(updated.state, TermCommitState.COMMITTED)
        self.assertTrue(updated_configured.match_case)
        self.assertFalse(updated_configured.whole_word)
        self.assertIn(
            ("Configured", "configured-updated"),
            tuple(
                (suggestion.source_term, suggestion.target_term)
                for suggestion in after_update_terms
            ),
        )
        self.assertIs(deleted.state, TermCommitState.COMMITTED)
        self.assertEqual(
            tuple((record.source, record.target) for record in final_records),
            (("Legacy", "old"),),
        )
        self.assertNotIn(
            "Configured",
            tuple(suggestion.source_term for suggestion in after_delete_terms),
        )
        self.assertIsNot(before_engines[first.id], after_create_engines[first.id])
        self.assertIsNot(before_engines[second.id], after_create_engines[second.id])
        self.assertEqual(artifacts, ())

    def test_list_terms_rebuilds_every_record_and_locator_defensively(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            before_engine = self._term_engines(controller)[resource.id]
            private = self._private_term_records(controller)[resource.id]
            first = controller.list_terms(resource.id)
            original_digest = first[0].locator.row_digest

            object.__setattr__(first[0], "target", "caller-corruption")
            object.__setattr__(first[0].locator, "row_digest", "f" * 64)

            second = controller.list_terms(resource.id)
            suggestions = controller.term_suggestions()

        self.assertEqual(second[0].target, "old")
        self.assertEqual(second[0].locator.row_digest, original_digest)
        self.assertIsNot(first[0], second[0])
        self.assertIsNot(first[0].locator, second[0].locator)
        self.assertIsNot(second[0], private[0])
        self.assertIsNot(second[0].locator, private[0].locator)
        self.assertEqual(
            tuple(
                (suggestion.source_term, suggestion.target_term)
                for suggestion in suggestions
            ),
            (("Legacy", "old"),),
        )
        self.assertIs(self._term_engines(controller)[resource.id], before_engine)

    def test_private_semantic_drift_fails_closed_but_validator_faults_propagate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            before_engine = self._term_engines(controller)[resource.id]
            before_bytes = resource.path.read_bytes()
            private = self._private_term_records(controller)[resource.id]
            object.__setattr__(private[0], "target", "")

            with self.assertRaisesRegex(
                EditorControllerError,
                "TERM.RUNTIME_INVALID",
            ):
                controller.list_terms(resource.id)

            self.assertIs(
                self._term_engines(controller)[resource.id],
                before_engine,
            )
            self.assertEqual(resource.path.read_bytes(), before_bytes)

        with tempfile.TemporaryDirectory() as temp_dir:
            controller, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            private = self._private_term_records(controller)[resource.id]
            object.__setattr__(private[0], "target", object())
            with self.assertRaises(TypeError):
                controller.list_terms(resource.id)

        with tempfile.TemporaryDirectory() as temp_dir:
            controller, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            with patch.object(
                TermRecord,
                "__post_init__",
                autospec=True,
                side_effect=AssertionError("injected validator fault"),
            ):
                with self.assertRaisesRegex(
                    AssertionError,
                    "injected validator fault",
                ):
                    controller.list_terms(resource.id)

    def test_legacy_update_preserves_two_column_shape_and_legacy_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            legacy = controller.list_terms(resource.id)[0]

            outcome = controller.update_term(
                resource.id,
                legacy.locator,
                TermDraft(
                    "Legacy",
                    "new",
                    match_case=False,
                    whole_word=True,
                ),
            )
            updated = controller.list_terms(resource.id)[0]
            committed_bytes = resource.path.read_bytes()

        self.assertIs(outcome.state, TermCommitState.COMMITTED)
        self.assertEqual(committed_bytes, b"\xef\xbb\xbfLegacy,new\n")
        self.assertIs(updated.locator.row_kind, TermRowKind.LEGACY)
        self.assertIsNone(updated.record_id)
        self.assertIsNone(updated.match_case)
        self.assertIsNone(updated.whole_word)

    def test_candidate_build_failure_discards_without_changing_any_engine_or_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            original = resource.path.read_bytes()
            before_engines = self._term_engines(controller)

            with patch(
                "editor_controller.ConfiguredTermAdapter",
                side_effect=ValueError("injected candidate failure"),
            ):
                with self.assertRaisesRegex(
                    EditorControllerError,
                    "TERM.CANDIDATE_BUILD_FAILED",
                ):
                    controller.create_term(
                        resource.id,
                        TermDraft("Fresh", "new"),
                    )

            self.assertEqual(resource.path.read_bytes(), original)
            after_engines = self._term_engines(controller)
            artifacts = self._artifact_paths(resource)

        self.assertEqual(artifacts, ())
        self.assertIs(before_engines[resource.id], after_engines[resource.id])

    def test_not_committed_and_rolled_back_keep_exact_lkg_engine_identities(
        self,
    ) -> None:
        for mode in ("replace", "fsync"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                controller, (resource,) = self._controller(
                    Path(temp_dir),
                    b"Legacy,old\n",
                )
                original = resource.path.read_bytes()
                before_engines = self._term_engines(controller)
                real_fsync = termbase_store_module._fsync_directory
                fsync_calls = 0

                def fail_commit_fsync(path: Path) -> None:
                    nonlocal fsync_calls
                    fsync_calls += 1
                    if fsync_calls == 2:
                        raise OSError("injected commit fsync failure")
                    real_fsync(path)

                replacement = (
                    patch(
                        "termbase_store.os.replace",
                        side_effect=OSError("injected replace failure"),
                    )
                    if mode == "replace"
                    else patch(
                        "termbase_store._fsync_directory",
                        side_effect=fail_commit_fsync,
                    )
                )
                with replacement:
                    outcome = controller.create_term(
                        resource.id,
                        TermDraft("Fresh", "new"),
                    )

                self.assertIn(
                    outcome.state,
                    (TermCommitState.NOT_COMMITTED, TermCommitState.ROLLED_BACK),
                )
                self.assertEqual(resource.path.read_bytes(), original)
                self.assertIs(
                    before_engines[resource.id],
                    self._term_engines(controller)[resource.id],
                )
                self.assertEqual(self._artifact_paths(resource), ())

    def test_indeterminate_quarantines_mutation_but_retains_in_memory_lkg(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            before_records = controller.list_terms(resource.id)
            before_engine = self._term_engines(controller)[resource.id]
            real_fsync = termbase_store_module._fsync_directory
            real_replace = os.replace
            fsync_calls = 0
            replace_calls = 0

            def fail_commit_fsync(path: Path) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls >= 2:
                    raise OSError("injected fsync failure")
                real_fsync(path)

            def fail_rollback_replace(source: Path, target: Path) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    real_replace(source, target)
                    return
                raise OSError("injected rollback failure")

            with (
                patch(
                    "termbase_store._fsync_directory",
                    side_effect=fail_commit_fsync,
                ),
                patch(
                    "termbase_store.os.replace",
                    side_effect=fail_rollback_replace,
                ),
            ):
                outcome = controller.create_term(
                    resource.id,
                    TermDraft("Fresh", "new"),
                )

            retained = controller.list_terms(resource.id)
            retained_suggestions = controller.term_suggestions()
            repeated = controller.create_term(
                resource.id,
                TermDraft("Another", "value"),
            )

        self.assertIs(outcome.state, TermCommitState.INDETERMINATE)
        self.assertTrue(outcome.quarantined)
        self.assertIsNotNone(outcome.recovery_path)
        self.assertEqual(outcome.error_code, "ROLLBACK_FAILED")
        self.assertEqual(retained, before_records)
        self.assertEqual(
            tuple(
                (suggestion.source_term, suggestion.target_term)
                for suggestion in retained_suggestions
            ),
            (("Legacy", "old"),),
        )
        self.assertIs(repeated, outcome)
        self.assertIs(
            self._term_engines(controller)[resource.id],
            before_engine,
        )

    def test_current_host_issuer_proof_fails_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            original = resource.path.read_bytes()
            before_engine = self._term_engines(controller)[resource.id]
            adapter = cast(Any, controller)._tm_adapter

            with patch.object(
                type(adapter),
                "_is_current_text_matcher_handoff_for_controller",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    EditorControllerError,
                    "TERM.CANDIDATE_BUILD_FAILED",
                ):
                    controller.create_term(
                        resource.id,
                        TermDraft("Fresh", "new"),
                    )
            current_bytes = resource.path.read_bytes()
            artifacts = self._artifact_paths(resource)

        self.assertEqual(current_bytes, original)
        self.assertIs(
            self._term_engines(controller)[resource.id],
            before_engine,
        )
        self.assertEqual(artifacts, ())

    def test_cleanup_warning_does_not_rollback_committed_data_or_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            before_engine = self._term_engines(controller)[resource.id]

            def warn_only(
                _store: TermbaseStore,
                prepared: object,
                _outcome: object,
            ) -> TermCleanupReport:
                recovery_path = cast(Any, prepared).recovery_path
                return TermCleanupReport(
                    cleaned=False,
                    recovery_path=recovery_path,
                    warning_code="RECOVERY_DELETE_FAILED",
                )

            with (
                patch.object(
                    TermbaseStore,
                    "finalize",
                    autospec=True,
                    side_effect=warn_only,
                ),
                self.assertLogs("editor_controller", level="WARNING") as logs,
            ):
                outcome = controller.create_term(
                    resource.id,
                    TermDraft("Fresh", "new"),
                )

            committed_records = TermbaseStore().list_records(resource.path)

        self.assertIs(outcome.state, TermCommitState.COMMITTED)
        self.assertEqual(
            tuple(record.source for record in committed_records),
            ("Legacy", "Fresh"),
        )
        self.assertIsNot(
            self._term_engines(controller)[resource.id],
            before_engine,
        )
        self.assertIn("RECOVERY_DELETE_FAILED", "\n".join(logs.output))

    def test_only_writable_termbases_are_manageable_and_programmer_errors_propagate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            readonly = controller.repository.update_resource(
                replace(resource, update=False)
            )

            with self.assertRaisesRegex(
                EditorControllerError,
                "TERM.RESOURCE_NOT_WRITABLE",
            ):
                controller.list_terms(readonly.id)
            with self.assertRaises(TypeError):
                controller.create_term(resource.id, cast(TermDraft, object()))
            with self.assertRaises(TypeError):
                controller.list_terms(cast(str, object()))


if __name__ == "__main__":
    unittest.main()
