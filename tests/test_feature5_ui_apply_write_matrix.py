"""Task 7.5 cross-layer suggestion/apply/write permission acceptance.

The journeys start with the Task 7.2 production-built activated SQLite
fixture, reopen it through the production resolver, and exercise the public
Controller business API.  No handwritten canonical store or UI-side matcher
stands in for the Feature 5 path.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Callable
import unittest
from unittest.mock import patch

from capability_host import CapabilityHostComposition
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceConfig,
    ResourceKind,
    TMResourceDisplayMode,
    TMSuggestion,
    TMSuggestionProvenance,
)
from editor_controller import EditorController, EditorControllerError
from editor_tm_adapter import EditorTMAdapter
from resource_repository import ResourceRepository
from tm_application_composition import (
    LegacyAppendOperationError,
    TMResourceResolver,
    TMRuntimeHost,
)
from tm_contracts import TMMatchType
from tests.feature5_ui_canonical_fixture import (
    QUERY_SOURCE,
    ActivatedCanonicalFixture,
    build_activated_canonical_fixture,
)
from tests.test_editor_tm_adapter_mixed import (
    _open_capability as _open_validated_capability,
)
from tests.test_capability_host_gate_d import _gate_c as _refresh_gate_c


_LEGACY_LOOKUP_ID = "tm.legacy.lookup-only"
_LEGACY_UPDATE_ID = "tm.legacy.update-only"
_LEGACY_INACTIVE_ID = "tm.legacy.inactive"


def _resource_family_hashes(config: ResourceConfig) -> tuple[tuple[str, str], ...]:
    """Hash every regular configured/canonical sibling for one resource."""

    entries: list[tuple[str, str]] = []
    for path in sorted(config.path.parent.glob(f"{config.path.name}*")):
        if path.is_symlink() or not path.is_file():
            continue
        entries.append((path.name, hashlib.sha256(path.read_bytes()).hexdigest()))
    if not entries:
        raise AssertionError("resource family hash requires at least one regular file")
    return tuple(entries)


def _editor_state(controller: EditorController) -> tuple[str, bool, bool, int]:
    segment = controller.current_segment
    return (
        segment.target,
        segment.confirmed,
        controller.dirty,
        controller.current_index,
    )


def _write_registry(
    config_dir: Path,
    configs: tuple[ResourceConfig, ...],
) -> ResourceRepository:
    config_dir.mkdir(parents=True, exist_ok=False)
    payload = {
        "schema_version": 1,
        "resources": [
            {
                "id": config.id,
                "name": config.name,
                "kind": config.kind.value,
                "path": str(config.path),
                "active": config.active,
                "lookup": config.lookup,
                "update": config.update,
            }
            for config in configs
        ],
    }
    (config_dir / "resources.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return ResourceRepository(config_dir)


def _matrix_configs(
    root: Path,
    fixture: ActivatedCanonicalFixture,
) -> tuple[ResourceConfig, ...]:
    primary, secondary = fixture.resources[:2]
    legacy_lookup = (root / "legacy-lookup.jsonl").resolve()
    legacy_lookup.write_text(
        json.dumps(
            {"source": QUERY_SOURCE, "target": "legacy lookup target"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    legacy_update = (root / "legacy-update.jsonl").resolve()
    legacy_update.write_text("", encoding="utf-8")
    legacy_inactive = (root / "legacy-inactive.jsonl").resolve()
    legacy_inactive.write_text("", encoding="utf-8")
    return (
        ResourceConfig(
            id=primary.resource_id,
            name="Canonical lookup only",
            kind=ResourceKind.TRANSLATION_MEMORY,
            path=primary.identity.configured_jsonl_path,
            active=True,
            lookup=True,
            update=False,
        ),
        ResourceConfig(
            id=secondary.resource_id,
            name="Canonical lookup and update",
            kind=ResourceKind.TRANSLATION_MEMORY,
            path=secondary.identity.configured_jsonl_path,
            active=True,
            lookup=True,
            update=True,
        ),
        ResourceConfig(
            id=_LEGACY_LOOKUP_ID,
            name="Legacy lookup only",
            kind=ResourceKind.TRANSLATION_MEMORY,
            path=legacy_lookup,
            active=True,
            lookup=True,
            update=False,
        ),
        ResourceConfig(
            id=_LEGACY_UPDATE_ID,
            name="Legacy update only",
            kind=ResourceKind.TRANSLATION_MEMORY,
            path=legacy_update,
            active=True,
            lookup=False,
            update=True,
        ),
        ResourceConfig(
            id=_LEGACY_INACTIVE_ID,
            name="Legacy inactive",
            kind=ResourceKind.TRANSLATION_MEMORY,
            path=legacy_inactive,
            active=False,
            lookup=True,
            update=True,
        ),
    )


def _controller(
    root: Path,
    configs: tuple[ResourceConfig, ...],
    composition: CapabilityHostComposition,
) -> tuple[EditorController, TMRuntimeHost, ResourceRepository]:
    repository = _write_registry(root / "app-data", configs)
    runtime = TMRuntimeHost(
        resolver=TMResourceResolver(),
        configs=repository.list_resources(),
    )
    controller = EditorController(
        repository,
        tm_adapter=EditorTMAdapter(
            runtime_host=runtime,
            capability_host=composition.host,
        ),
    )
    controller.set_project(
        EditorProject(
            name="Task 7.5 integration",
            segments=(
                EditorSegment(
                    id="segment-current",
                    source=QUERY_SOURCE,
                    target="original target",
                    speaker="Narrator",
                    confirmed=True,
                ),
                EditorSegment(
                    id="segment-next",
                    source="next source",
                    target="",
                    speaker="Narrator",
                    confirmed=False,
                ),
            ),
        )
    )
    return controller, runtime, repository


def _suggestion_of_type(
    controller: EditorController,
    match_type: TMMatchType,
) -> TMSuggestion:
    return next(
        suggestion
        for suggestion in controller.tm_suggestion_report().suggestions
        if suggestion.match_type is match_type
    )


class Feature5UIApplyWriteMatrixTests(unittest.TestCase):
    def test_three_match_types_apply_explicitly_then_write_only_active_update(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_activated_canonical_fixture(root / "canonical")
            configs = _matrix_configs(root, fixture)
            composition = _open_validated_capability(self)
            controller, _runtime, _repository = _controller(
                root / "controller",
                configs,
                composition,
            )
            issued = controller.tm_suggestion_report().suggestions
            before_apply = {
                config.id: _resource_family_hashes(config) for config in configs
            }
            self.assertEqual(
                {suggestion.match_type for suggestion in issued},
                {TMMatchType.EXACT, TMMatchType.CONTEXT, TMMatchType.FUZZY},
            )
            self.assertIn(_LEGACY_LOOKUP_ID, {item.resource_id for item in issued})
            self.assertNotIn(_LEGACY_UPDATE_ID, {item.resource_id for item in issued})
            self.assertNotIn(_LEGACY_INACTIVE_ID, {item.resource_id for item in issued})

            for match_type in (
                TMMatchType.EXACT,
                TMMatchType.CONTEXT,
                TMMatchType.FUZZY,
            ):
                suggestion = next(
                    item for item in issued if item.match_type is match_type
                )
                position = controller.current_index

                controller.apply_tm_suggestion(suggestion)

                self.assertEqual(controller.current_segment.target, suggestion.target)
                self.assertFalse(controller.current_segment.confirmed)
                self.assertTrue(controller.dirty)
                self.assertEqual(controller.current_index, position)
                self.assertEqual(
                    {
                        config.id: _resource_family_hashes(config)
                        for config in configs
                    },
                    before_apply,
                )

            before_confirm = {
                config.id: _resource_family_hashes(config) for config in configs
            }
            result = controller.confirm_current()

            expected_written = (
                fixture.resources[1].resource_id,
                _LEGACY_UPDATE_ID,
            )
            self.assertTrue(result.write_report.succeeded)
            self.assertEqual(
                result.write_report.written_resource_ids,
                expected_written,
            )
            self.assertTrue(result.project.segments[0].confirmed)
            self.assertEqual(result.current_index, 1)
            self.assertEqual(controller.current_index, 1)
            self.assertEqual(
                _resource_family_hashes(configs[0]),
                before_confirm[configs[0].id],
            )
            self.assertEqual(
                _resource_family_hashes(configs[2]),
                before_confirm[configs[2].id],
            )
            self.assertEqual(
                _resource_family_hashes(configs[4]),
                before_confirm[configs[4].id],
            )
            self.assertNotEqual(
                _resource_family_hashes(configs[1]),
                before_confirm[configs[1].id],
            )
            self.assertNotEqual(
                _resource_family_hashes(configs[3]),
                before_confirm[configs[3].id],
            )

    def test_every_suggestion_field_substitution_rejects_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_activated_canonical_fixture(root / "canonical")
            configs = _matrix_configs(root, fixture)
            controller, _runtime, _repository = _controller(
                root / "controller",
                configs,
                _open_validated_capability(self),
            )
            exact = _suggestion_of_type(controller, TMMatchType.EXACT)
            fuzzy = _suggestion_of_type(controller, TMMatchType.FUZZY)
            identity = fuzzy.query_identity
            substitutions = {
                "resource_id": replace(fuzzy, resource_id="tm.substituted"),
                "record_id": replace(fuzzy, record_id="canonical:999999"),
                "query_source": replace(fuzzy, query_source="other query"),
                "matched_source": replace(fuzzy, matched_source="other match"),
                "target": replace(fuzzy, target="substituted target"),
                "match_type": replace(exact, match_type=TMMatchType.CONTEXT),
                "final_similarity": replace(fuzzy, final_similarity=0.81),
                "provenance.resource_name": replace(
                    fuzzy,
                    provenance=TMSuggestionProvenance(
                        resource_name="Substituted resource",
                        resource_mode=fuzzy.provenance.resource_mode,
                    ),
                ),
                "provenance.resource_mode": replace(
                    fuzzy,
                    provenance=TMSuggestionProvenance(
                        resource_name=fuzzy.provenance.resource_name,
                        resource_mode=TMResourceDisplayMode.DEGRADED,
                    ),
                ),
                "query_identity.project_session_id": replace(
                    fuzzy,
                    query_identity=replace(
                        identity,
                        project_session_id="substituted-session",
                    ),
                ),
                "query_identity.segment_id": replace(
                    fuzzy,
                    query_identity=replace(identity, segment_id="other-segment"),
                ),
                "query_identity.source_digest": replace(
                    fuzzy,
                    query_identity=replace(identity, source_digest="0" * 64),
                ),
                "query_identity.query_epoch": replace(
                    fuzzy,
                    query_identity=replace(
                        identity,
                        query_epoch=identity.query_epoch + 1,
                    ),
                ),
            }
            initial_state = _editor_state(controller)
            initial_hashes = {
                config.id: _resource_family_hashes(config) for config in configs
            }

            for field_name, candidate in substitutions.items():
                with self.subTest(field=field_name), self.assertRaises(
                    EditorControllerError
                ):
                    controller.apply_tm_suggestion(candidate)
                self.assertEqual(_editor_state(controller), initial_state)
                self.assertEqual(
                    {
                        config.id: _resource_family_hashes(config)
                        for config in configs
                    },
                    initial_hashes,
                )

    def test_all_six_epoch_changes_reject_old_suggestion_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_activated_canonical_fixture(root / "canonical")
            configs = _matrix_configs(root, fixture)
            composition = _open_validated_capability(self)
            controller_index = 0

            def fresh_controller() -> tuple[
                EditorController,
                TMRuntimeHost,
                ResourceRepository,
            ]:
                nonlocal controller_index
                controller_index += 1
                return _controller(
                    root / f"controller-{controller_index}",
                    configs,
                    composition,
                )

            def project_change(
                controller: EditorController,
                _runtime: TMRuntimeHost,
                _repository: ResourceRepository,
            ) -> None:
                controller.set_project(
                    EditorProject(
                        name="replacement project",
                        segments=(
                            EditorSegment(
                                id="replacement-segment",
                                source=QUERY_SOURCE,
                                target="replacement target",
                                confirmed=True,
                            ),
                        ),
                    )
                )

            def segment_change(
                controller: EditorController,
                _runtime: TMRuntimeHost,
                _repository: ResourceRepository,
            ) -> None:
                controller.move(1)

            def source_change(
                controller: EditorController,
                _runtime: TMRuntimeHost,
                _repository: ResourceRepository,
            ) -> None:
                object.__setattr__(controller.current_segment, "source", "changed source")

            def resource_change(
                controller: EditorController,
                _runtime: TMRuntimeHost,
                repository: ResourceRepository,
            ) -> None:
                configured = repository.get(configs[0].id)
                controller.update_resource(replace(configured, lookup=False))

            def threshold_change(
                controller: EditorController,
                _runtime: TMRuntimeHost,
                _repository: ResourceRepository,
            ) -> None:
                outcome = controller.update_tm_minimum_similarity(0.80)
                self.assertTrue(outcome.succeeded)

            cases: tuple[
                tuple[
                    str,
                    Callable[
                        [EditorController, TMRuntimeHost, ResourceRepository],
                        None,
                    ],
                ],
                ...,
            ] = (
                ("project", project_change),
                ("segment", segment_change),
                ("source", source_change),
                ("resource", resource_change),
                ("threshold", threshold_change),
            )
            for name, trigger in cases:
                controller, runtime, repository = fresh_controller()
                suggestion = _suggestion_of_type(controller, TMMatchType.FUZZY)
                issued_epoch = suggestion.query_identity.query_epoch
                trigger(controller, runtime, repository)
                state_after_trigger = _editor_state(controller)
                hashes_after_trigger = {
                    config.id: _resource_family_hashes(config)
                    for config in repository.list_resources()
                }

                with self.subTest(epoch=name), self.assertRaises(
                    EditorControllerError
                ):
                    controller.apply_tm_suggestion(suggestion)
                self.assertGreater(controller.query_epoch, issued_epoch)
                self.assertEqual(_editor_state(controller), state_after_trigger)
                self.assertEqual(
                    {
                        config.id: _resource_family_hashes(config)
                        for config in repository.list_resources()
                    },
                    hashes_after_trigger,
                )

            controller, _runtime, repository = fresh_controller()
            suggestion = _suggestion_of_type(controller, TMMatchType.FUZZY)
            issued_epoch = suggestion.query_identity.query_epoch
            _ = _refresh_gate_c(composition)
            state_after_trigger = _editor_state(controller)
            hashes_after_trigger = {
                config.id: _resource_family_hashes(config)
                for config in repository.list_resources()
            }

            with self.subTest(epoch="capability"), self.assertRaises(
                EditorControllerError
            ):
                controller.apply_tm_suggestion(suggestion)
            self.assertGreater(controller.query_epoch, issued_epoch)
            self.assertEqual(_editor_state(controller), state_after_trigger)
            self.assertEqual(
                {
                    config.id: _resource_family_hashes(config)
                    for config in repository.list_resources()
                },
                hashes_after_trigger,
            )

    def test_partial_write_failure_does_not_confirm_or_navigate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_activated_canonical_fixture(root / "canonical")
            configs = _matrix_configs(root, fixture)
            controller, runtime, _repository = _controller(
                root / "controller",
                configs,
                _open_validated_capability(self),
            )
            controller.apply_tm_suggestion(
                _suggestion_of_type(controller, TMMatchType.FUZZY)
            )
            before_state = _editor_state(controller)
            before_hashes = {
                config.id: _resource_family_hashes(config) for config in configs
            }
            snapshot = runtime.capture_operation_snapshot()
            failing_backend = next(
                port.backend
                for port in snapshot.legacy_ports
                if port.resource_id == _LEGACY_UPDATE_ID
            )

            with patch.object(
                type(failing_backend),
                "append",
                autospec=True,
                side_effect=LegacyAppendOperationError(),
            ):
                result = controller.confirm_current()

            self.assertFalse(result.write_report.succeeded)
            self.assertEqual(
                result.write_report.written_resource_ids,
                (fixture.resources[1].resource_id,),
            )
            self.assertEqual(
                result.write_report.errors,
                ("TM.WRITE.LEGACY_APPEND_FAILED",),
            )
            self.assertEqual(_editor_state(controller), before_state)
            self.assertFalse(controller.current_segment.confirmed)
            self.assertEqual(controller.current_index, 0)
            for config in (configs[0], configs[2], configs[3], configs[4]):
                self.assertEqual(
                    _resource_family_hashes(config),
                    before_hashes[config.id],
                )
            self.assertNotEqual(
                _resource_family_hashes(configs[1]),
                before_hashes[configs[1].id],
            )


if __name__ == "__main__":
    unittest.main()
