from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from threading import Event, Thread
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

import termbase_store as termbase_store_module
from capability_host import CapabilityHostComposition, MatcherHandoffSnapshot
from configured_term_adapter import ConfiguredTermAdapter
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ImportRequest,
    ResourceConfig,
    ResourceKind,
    TermbaseImportHeaderMode,
    TermbaseImportSelection,
    TermCommitState,
    TermCommitOutcome,
    TermDraft,
    TermRecord,
    TermRowKind,
)
from editor_controller import EditorController, EditorControllerError
from qt_editor import _compose_editor_controller
from resource_repository import ResourceRepository
from termbase_store import TermbaseStore


_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


class EditorControllerTermImportReloadTests(unittest.TestCase):
    def _controller(
        self,
        root: Path,
        *payloads: bytes,
        source: str = "Configured Fresh dog Dogmatic",
    ) -> tuple[
        EditorController,
        CapabilityHostComposition,
        ResourceRepository,
        tuple[ResourceConfig, ...],
    ]:
        repository = ResourceRepository(root / "app-data")
        resources: list[ResourceConfig] = []
        for ordinal, payload in enumerate(payloads, start=1):
            resource = repository.create_resource(
                f"Terms {ordinal}",
                ResourceKind.TERMBASE,
            )
            resource.path.write_bytes(payload)
            resources.append(resource)
        controller, composition = _compose_editor_controller(repository)
        controller.set_project(
            EditorProject(
                name="Terms",
                segments=(EditorSegment(id="one", source=source),),
            )
        )
        return controller, composition, repository, tuple(resources)

    @staticmethod
    def _term_engines(controller: EditorController) -> dict[str, object]:
        return dict(cast(Any, controller)._glossary_engines)

    @staticmethod
    def _private_records(
        controller: EditorController,
    ) -> dict[str, tuple[TermRecord, ...]]:
        return dict(cast(Any, controller)._term_record_snapshots)

    @staticmethod
    def _force_indeterminate_create(
        controller: EditorController,
        resource: ResourceConfig,
    ) -> TermCommitOutcome:
        real_fsync = termbase_store_module._fsync_directory
        real_replace = os.replace
        fsync_calls = 0
        replace_calls = 0

        def fail_commit_and_rollback_fsync(path: Path) -> None:
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
                side_effect=fail_commit_and_rollback_fsync,
            ),
            patch(
                "termbase_store.os.replace",
                side_effect=fail_rollback_replace,
            ),
        ):
            return controller.create_term(
                resource.id,
                TermDraft("Fresh", "new"),
            )

    def test_import_preserves_v1_metadata_counts_runtime_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = (
                b"Legacy,old\n"
                b"localcat-term-v1,term-1,Configured,configured-old,true,false\n"
            )
            controller, composition, repository, (resource,) = self._controller(
                root,
                payload,
            )
            _ = composition.matcher_validation_owner.validate_text_v1(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            controller.reload_resources()
            incoming = root / "incoming.csv"
            incoming.write_text(
                "Source,Target\n"
                "Configured,configured-first\n"
                "Fresh,fresh-new\n"
                "Configured,configured-final\n",
                encoding="utf-8-sig",
            )

            report = controller.import_resource(
                ImportRequest(resource_id=resource.id, input_path=incoming)
            )
            records = controller.list_terms(resource.id)
            suggestions = controller.term_suggestions()
            private = self._private_records(controller)[resource.id]
            restarted, restarted_composition = _compose_editor_controller(
                repository
            )
            _ = restarted_composition.matcher_validation_owner.validate_text_v1(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            restarted.reload_resources()
            restarted_records = restarted.list_terms(resource.id)

        self.assertEqual(
            (report.imported, report.skipped, report.overwritten, report.errors),
            (2, 1, 2, ()),
        )
        self.assertEqual(
            tuple(
                (
                    record.source,
                    record.target,
                    record.locator.row_kind,
                    record.record_id,
                    record.match_case,
                    record.whole_word,
                )
                for record in records
            ),
            (
                ("Legacy", "old", TermRowKind.LEGACY, None, None, None),
                (
                    "Configured",
                    "configured-final",
                    TermRowKind.V1,
                    "term-1",
                    True,
                    False,
                ),
                ("Fresh", "fresh-new", TermRowKind.LEGACY, None, None, None),
            ),
        )
        self.assertEqual(restarted_records, records)
        self.assertEqual(
            tuple(
                (suggestion.source_term, suggestion.target_term)
                for suggestion in suggestions
            ),
            (
                ("Configured", "configured-final"),
                ("Fresh", "fresh-new"),
            ),
        )
        self.assertTrue(
            all(
                public is not owned and public.locator is not owned.locator
                for public, owned in zip(records, private, strict=True)
            )
        )

    def test_controller_preview_is_store_free_and_propagates_programmer_faults(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller, _composition, _repository, (_resource,) = self._controller(
                root,
                b"Keep,stable\n",
            )
            source = root / "incoming.csv"
            source.write_text(
                "Target,Notes,Source\n甲,first,Alpha\n",
                encoding="utf-8-sig",
            )

            with patch.object(
                controller._term_store,
                "prepare_merge_legacy",
                side_effect=AssertionError("preview touched the term store"),
            ):
                preview = controller.preview_termbase_import(source)

            self.assertEqual(preview.format_name, "csv")
            self.assertEqual(
                tuple(column.zero_based_index for column in preview.columns),
                (0, 1, 2),
            )

            with patch(
                "editor_controller.preview_termbase_import_file",
                side_effect=AssertionError("injected controller preview fault"),
            ):
                with self.assertRaisesRegex(
                    AssertionError,
                    "injected controller preview fault",
                ):
                    _ = controller.preview_termbase_import(source)

    def test_controller_import_consumes_explicit_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller, _composition, _repository, (resource,) = self._controller(
                root,
                b"Keep,stable\n",
            )
            source = root / "incoming.csv"
            source.write_text(
                "Target,Notes,Source\n甲,first,Alpha\n乙,second,Beta\n",
                encoding="utf-8-sig",
            )
            preview = controller.preview_termbase_import(source)
            selection = TermbaseImportSelection(
                source_zero_based_index=2,
                target_zero_based_index=0,
                header_mode=TermbaseImportHeaderMode.FIRST_ROW,
                preview_column_count=len(preview.columns),
                preview_source_identity=preview.source_identity,
            )

            report = controller.import_resource(
                ImportRequest(
                    resource_id=resource.id,
                    input_path=source,
                    termbase_selection=selection,
                )
            )
            records = controller.list_terms(resource.id)

        self.assertEqual(
            (report.imported, report.skipped, report.overwritten, report.errors),
            (2, 1, 0, ()),
        )
        self.assertEqual(
            tuple((record.source, record.target) for record in records),
            (("Keep", "stable"), ("Alpha", "甲"), ("Beta", "乙")),
        )

    def test_stale_selection_returns_error_without_term_store_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller, _composition, _repository, (resource,) = self._controller(
                root,
                b"Keep,stable\n",
            )
            source = root / "incoming.csv"
            source.write_text("Source,Target\nAlpha,甲\n", encoding="utf-8-sig")
            preview = controller.preview_termbase_import(source)
            selection = TermbaseImportSelection(
                source_zero_based_index=0,
                target_zero_based_index=1,
                header_mode=TermbaseImportHeaderMode.FIRST_ROW,
                preview_column_count=len(preview.columns),
                preview_source_identity=preview.source_identity,
            )
            source.write_text("Source,Target\nChanged,乙\n", encoding="utf-8-sig")

            with patch.object(
                controller._term_store,
                "prepare_merge_legacy",
                side_effect=AssertionError("stale import reached term store prepare"),
            ) as prepare:
                report = controller.import_resource(
                    ImportRequest(
                        resource_id=resource.id,
                        input_path=source,
                        termbase_selection=selection,
                    )
                )

            prepare.assert_not_called()
            retained = controller.list_terms(resource.id)

        self.assertEqual(report.imported, 0)
        self.assertEqual(report.skipped, 0)
        self.assertEqual(report.overwritten, 0)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("PARSER.SOURCE.STALE", report.errors[0])
        self.assertEqual(
            tuple((record.source, record.target) for record in retained),
            (("Keep", "stable"),),
        )

    def test_import_candidate_failure_keeps_exact_bytes_and_engine_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (
                controller,
                _composition,
                _repository,
                resources,
            ) = self._controller(
                root,
                b"Legacy,old\n",
                b"Other,other-old\n",
            )
            resource = resources[0]
            incoming = root / "incoming.csv"
            incoming.write_text("Fresh,new\n", encoding="utf-8-sig")
            before_bytes = resource.path.read_bytes()
            before_engines = self._term_engines(controller)

            with patch(
                "editor_controller.ConfiguredTermAdapter",
                side_effect=ValueError("injected candidate failure"),
            ):
                report = controller.import_resource(
                    ImportRequest(resource_id=resource.id, input_path=incoming)
                )

            after_engines = self._term_engines(controller)
            after_bytes = resource.path.read_bytes()

        self.assertTrue(report.errors)
        self.assertEqual(after_bytes, before_bytes)
        for active in resources:
            self.assertIs(
                after_engines[active.id],
                before_engines[active.id],
            )

    def test_indeterminate_import_quarantines_and_keeps_lkg_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller, composition, _repository, (resource,) = self._controller(
                root,
                b"Legacy,old\n",
                source="Legacy Fresh",
            )
            incoming = root / "incoming.csv"
            incoming.write_text("Fresh,new\n", encoding="utf-8-sig")
            before_records = controller.list_terms(resource.id)
            before_private_records = self._private_records(controller)[resource.id]
            before_engine = self._term_engines(controller)[resource.id]
            before_registry = controller.list_resources()
            initial = self._force_indeterminate_create(controller, resource)
            private_quarantine = cast(Any, controller)._term_quarantines[
                resource.id
            ]
            recovery_path = initial.recovery_path
            self.assertIsNotNone(recovery_path)
            assert recovery_path is not None
            self.assertIsNot(initial, private_quarantine)
            object.__setattr__(initial, "error_code", "POISONED.INITIAL")
            object.__setattr__(initial, "safe_detail", "private source body")
            object.__setattr__(initial, "recovery_path", root / "poison-initial")

            retained = controller.list_terms(resource.id)
            quarantine = controller.create_term(
                resource.id,
                TermDraft("Another", "value"),
            )
            self.assertIsNot(quarantine, private_quarantine)
            self.assertIsNot(quarantine, initial)
            object.__setattr__(quarantine, "error_code", "POISONED.REPEAT")
            object.__setattr__(quarantine, "safe_detail", "other source body")
            object.__setattr__(quarantine, "recovery_path", root / "poison-repeat")
            report = controller.import_resource(
                ImportRequest(
                    resource_id=resource.id,
                    input_path=incoming,
                )
            )
            fresh_quarantine = controller.create_term(
                resource.id,
                TermDraft("Again", "value"),
            )
            retained_engine = self._term_engines(controller)[resource.id]
            with patch.object(
                controller.repository,
                "delete_resource",
                wraps=controller.repository.delete_resource,
            ) as repository_delete:
                with self.assertRaisesRegex(
                    EditorControllerError,
                    "^TERM\\.RESOURCE_QUARANTINED$",
                ):
                    _ = controller.delete_resource(resource.id)
            repository_delete.assert_not_called()
            after_registry = controller.list_resources()
            after_private_records = self._private_records(controller)[resource.id]
            after_delete_engine = self._term_engines(controller)[resource.id]
            retained_quarantine = cast(Any, controller)._term_quarantines[
                resource.id
            ]
            resource_exists = resource.path.exists()
            recovery_exists = recovery_path.exists()
            current = composition.matcher_validation_owner.validate_text_v1(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            lkg_suggestions = controller.term_suggestions()
            switched_engine = self._term_engines(controller)[resource.id]

        self.assertEqual(report.imported, 0)
        self.assertEqual(
            report.errors,
            (
                "ROLLBACK_FAILED",
                f"Recovery file: {recovery_path}",
                "Quarantine the resource and restore it from the recovery file "
                "before retrying.",
            ),
        )
        self.assertEqual(retained, before_records)
        self.assertIs(retained_engine, before_engine)
        self.assertIs(fresh_quarantine.state, TermCommitState.INDETERMINATE)
        self.assertEqual(fresh_quarantine.error_code, "ROLLBACK_FAILED")
        self.assertEqual(
            fresh_quarantine.safe_detail,
            "Quarantine the resource and restore it from the recovery file "
            "before retrying.",
        )
        self.assertEqual(fresh_quarantine.recovery_path, recovery_path)
        self.assertTrue(fresh_quarantine.quarantined)
        self.assertIsNot(fresh_quarantine, private_quarantine)
        self.assertIsNot(fresh_quarantine, quarantine)
        self.assertEqual(after_registry, before_registry)
        self.assertTrue(resource_exists)
        self.assertTrue(recovery_exists)
        self.assertIs(after_delete_engine, before_engine)
        self.assertIs(after_private_records, before_private_records)
        self.assertIs(retained_quarantine, private_quarantine)
        self.assertEqual(
            tuple(
                (item.source_term, item.target_term)
                for item in lkg_suggestions
            ),
            (("Legacy", "old"),),
        )
        self.assertIs(cast(Any, switched_engine)._handoff, current)

    def test_private_quarantine_semantic_drift_fails_closed_body_safe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, _composition, _repository, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            before_engine = self._term_engines(controller)[resource.id]
            before_records = self._private_records(controller)[resource.id]
            _ = self._force_indeterminate_create(controller, resource)
            private = cast(Any, controller)._term_quarantines[resource.id]
            recovery_path = cast(TermCommitOutcome, private).recovery_path
            self.assertIsNotNone(recovery_path)
            assert recovery_path is not None
            object.__setattr__(private, "safe_detail", "private source body")

            with self.assertRaisesRegex(
                EditorControllerError,
                "^TERM\\.QUARANTINE_INVALID$",
            ):
                _ = controller.create_term(
                    resource.id,
                    TermDraft("Again", "value"),
                )
            with self.assertRaisesRegex(
                EditorControllerError,
                "^TERM\\.RESOURCE_QUARANTINED$",
            ):
                _ = controller.delete_resource(resource.id)

            after_engine = self._term_engines(controller)[resource.id]
            after_records = self._private_records(controller)[resource.id]
            resource_exists = resource.path.exists()
            recovery_exists = recovery_path.exists()

        self.assertIs(after_engine, before_engine)
        self.assertIs(after_records, before_records)
        self.assertTrue(resource_exists)
        self.assertTrue(recovery_exists)

    def test_private_quarantine_programmer_faults_propagate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, _composition, _repository, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            _ = self._force_indeterminate_create(controller, resource)
            private = cast(Any, controller)._term_quarantines[resource.id]
            object.__setattr__(private, "error_code", object())
            with self.assertRaises(TypeError):
                _ = controller.create_term(
                    resource.id,
                    TermDraft("Again", "value"),
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            controller, _composition, _repository, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            _ = self._force_indeterminate_create(controller, resource)
            with patch.object(
                TermCommitOutcome,
                "__post_init__",
                autospec=True,
                side_effect=AssertionError("injected validator fault"),
            ):
                with self.assertRaisesRegex(
                    AssertionError,
                    "injected validator fault",
                ):
                    _ = controller.create_term(
                        resource.id,
                        TermDraft("Again", "value"),
                    )

    def test_capability_switch_rebuilds_all_active_terms_as_one_current_cohort(
        self,
    ) -> None:
        payload = (
            b"localcat-term-v1,term-1,Dog,dog-target,false,true\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, composition, _repository, resources = self._controller(
                Path(temp_dir),
                payload,
                payload.replace(b"term-1", b"term-2"),
                source="dog Dogmatic",
            )
            _ = composition.matcher_validation_owner.validate_basic(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            controller.reload_resources()
            basic_engines = self._term_engines(controller)
            basic_suggestions = controller.term_suggestions()

            current = composition.matcher_validation_owner.validate_text_v1(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            configured_suggestions = controller.term_suggestions()
            configured_engines = self._term_engines(controller)

        self.assertEqual(
            tuple(item.source_term for item in basic_suggestions),
            ("Dog", "Dog"),
        )
        self.assertEqual(
            tuple(item.source_term for item in configured_suggestions),
            ("dog", "dog"),
        )
        for resource in resources:
            self.assertIsNot(
                configured_engines[resource.id],
                basic_engines[resource.id],
            )
            engine = configured_engines[resource.id]
            self.assertIs(type(engine), ConfiguredTermAdapter)
            self.assertIs(cast(Any, engine)._handoff, current)

    def test_build_to_commit_matcher_race_never_publishes_stale_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, composition, _repository, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            _ = composition.matcher_validation_owner.validate_basic(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            controller.reload_resources()
            before_bytes = resource.path.read_bytes()
            before_engine = self._term_engines(controller)[resource.id]
            built = Event()
            switched = Event()
            real_build = cast(Any, controller)._build_term_engine_sets

            def build_then_allow_switch(*args: object, **kwargs: object):
                candidate = real_build(*args, **kwargs)
                built.set()
                if not switched.wait(timeout=10):
                    raise AssertionError("matcher switch did not complete")
                return candidate

            def switch_matcher() -> None:
                if not built.wait(timeout=10):
                    return
                _ = composition.matcher_validation_owner.validate_text_v1(
                    generated_at_utc=_GENERATED_AT,
                    valid_until_utc=_VALID_UNTIL,
                    evaluated_at_utc=_EVALUATED_AT,
                )
                switched.set()

            worker = Thread(target=switch_matcher, daemon=True)
            worker.start()
            outcome = None
            error: EditorControllerError | None = None
            with patch.object(
                controller,
                "_build_term_engine_sets",
                side_effect=build_then_allow_switch,
            ):
                try:
                    outcome = controller.create_term(
                        resource.id,
                        TermDraft("Fresh", "new"),
                    )
                except EditorControllerError as caught:
                    error = caught
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            current = composition.host.matcher_snapshot()
            after_engine = self._term_engines(controller)[resource.id]
            after_bytes = resource.path.read_bytes()

        if error is not None:
            self.assertIn("TERM.MATCHER_GENERATION_CHANGED", str(error))
            self.assertEqual(after_bytes, before_bytes)
            self.assertIs(after_engine, before_engine)
            return
        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertIs(outcome.state, TermCommitState.COMMITTED)
        self.assertIs(type(after_engine), ConfiguredTermAdapter)
        self.assertIs(cast(Any, after_engine)._handoff, current)

    def test_commit_publication_reservation_blocks_capability_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, composition, _repository, (resource,) = self._controller(
                Path(temp_dir),
                b"Legacy,old\n",
            )
            _ = composition.matcher_validation_owner.validate_basic(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            controller.reload_resources()
            entered_commit = Event()
            release_commit = Event()
            switch_started = Event()
            switch_published = Event()
            real_commit = TermbaseStore.commit
            outcome: list[object] = []

            def blocking_commit(
                store: TermbaseStore,
                prepared: object,
            ):
                entered_commit.set()
                if not release_commit.wait(timeout=10):
                    raise AssertionError("term commit was not released")
                return real_commit(store, cast(Any, prepared))

            def mutate() -> None:
                outcome.append(
                    controller.create_term(
                        resource.id,
                        TermDraft("Fresh", "new"),
                    )
                )

            def switch_matcher() -> None:
                switch_started.set()
                _ = composition.matcher_validation_owner.validate_text_v1(
                    generated_at_utc=_GENERATED_AT,
                    valid_until_utc=_VALID_UNTIL,
                    evaluated_at_utc=_EVALUATED_AT,
                )
                switch_published.set()

            with patch.object(
                TermbaseStore,
                "commit",
                autospec=True,
                side_effect=blocking_commit,
            ):
                mutation_worker = Thread(target=mutate, daemon=True)
                mutation_worker.start()
                self.assertTrue(entered_commit.wait(timeout=10))
                switch_worker = Thread(target=switch_matcher, daemon=True)
                switch_worker.start()
                self.assertTrue(switch_started.wait(timeout=10))
                self.assertFalse(switch_published.wait(timeout=0.1))
                release_commit.set()
                mutation_worker.join(timeout=10)
                switch_worker.join(timeout=10)

            suggestions = controller.term_suggestions()
            current = composition.host.matcher_snapshot()
            current_engine = self._term_engines(controller)[resource.id]

        self.assertFalse(mutation_worker.is_alive())
        self.assertFalse(switch_worker.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIs(cast(Any, outcome[0]).state, TermCommitState.COMMITTED)
        self.assertTrue(switch_published.is_set())
        self.assertIn(
            ("Fresh", "new"),
            tuple(
                (item.source_term, item.target_term)
                for item in suggestions
            ),
        )
        self.assertIs(cast(Any, current_engine)._handoff, current)

    def test_hot_reload_replaces_complete_term_graph_from_committed_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, composition, _repository, resources = self._controller(
                Path(temp_dir),
                b"Legacy,old-one\n",
                b"Other,old-two\n",
                source="Legacy Other",
            )
            current = composition.matcher_validation_owner.validate_text_v1(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            controller.reload_resources()
            before = self._term_engines(controller)
            resources[0].path.write_bytes(b"Legacy,new-one\n")
            resources[1].path.write_bytes(b"Other,new-two\n")

            controller.reload_resources()

            after = self._term_engines(controller)
            suggestions = controller.term_suggestions()
            records = self._private_records(controller)

        self.assertEqual(
            tuple(item.target_term for item in suggestions),
            ("new-one", "new-two"),
        )
        self.assertEqual(
            tuple(records[resource.id][0].target for resource in resources),
            ("new-one", "new-two"),
        )
        for resource in resources:
            self.assertIsNot(after[resource.id], before[resource.id])
            self.assertIs(cast(Any, after[resource.id])._handoff, current)

    def test_programmer_error_from_import_candidate_build_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller, _composition, _repository, (resource,) = self._controller(
                root,
                b"Legacy,old\n",
            )
            incoming = root / "incoming.csv"
            incoming.write_text("Fresh,new\n", encoding="utf-8-sig")

            with patch(
                "editor_controller.ConfiguredTermAdapter",
                side_effect=AssertionError("injected programmer fault"),
            ):
                with self.assertRaisesRegex(
                    AssertionError,
                    "injected programmer fault",
                ):
                    _ = controller.import_resource(
                        ImportRequest(
                            resource_id=resource.id,
                            input_path=incoming,
                        )
                    )


if __name__ == "__main__":
    unittest.main()
