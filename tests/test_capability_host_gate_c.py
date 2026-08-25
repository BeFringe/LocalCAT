from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import shutil
import sys
import tm_retrieval_validation as retrieval_validation_module
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from types import MethodType
from typing import Any, cast
from unittest.mock import patch

from capability_host import CapabilityHostComposition, compose_capability_host
from tm_contracts import QueryReport, TMQuery, TextMatcherState
from tm_retrieval import TMRetrievalService
from tm_retrieval_capability import (
    RetrievalCapabilityExpectation,
    RetrievalCapabilityManifest,
    RetrievalCapabilitySnapshot,
    RetrievalCapabilityEvaluator,
    RetrievalCapabilityPublisher,
    RetrievalContextDecision,
)
from tm_retrieval_validation import (
    RetrievalValidationRelease,
    recompute_retrieval_validation,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
_EMPTY_QUERY = TMQuery(
    query_source="source",
    speaker_raw=None,
    context_prev_raw=None,
    context_next_raw=None,
    minimum_similarity=0.60,
    limit=10,
    resource_order=(),
)


def _composition() -> CapabilityHostComposition:
    return compose_capability_host(evaluated_at_utc=_EVALUATED_AT)


def _gate_c_owner(composition: CapabilityHostComposition) -> Any:
    return cast(Any, composition).retrieval_gate_c_validation_owner


def _core_release() -> RetrievalValidationRelease:
    release = recompute_retrieval_validation(
        repository_root=_REPOSITORY_ROOT,
        generated_at_utc=_GENERATED_AT,
        valid_until_utc=_VALID_UNTIL,
    )
    if release.manifest is None:
        raise AssertionError("current checkout must produce Gate C evidence")
    return release


def _private_service(snapshot: Any) -> TMRetrievalService:
    port = snapshot.query_port
    field = dataclasses.fields(port)[0]
    service = getattr(port, field.name)
    if type(service) is not TMRetrievalService:
        raise AssertionError("host query port must retain one Core service")
    return service


class CapabilityHostGateCPublicBoundaryTests(unittest.TestCase):
    def test_composition_exposes_one_narrow_gate_c_owner(self) -> None:
        composition = _composition()
        owner = _gate_c_owner(composition)

        self.assertEqual(
            tuple(name for name in dir(owner) if not name.startswith("_")),
            ("validate_gate_c",),
        )
        self.assertEqual(
            tuple(inspect.signature(owner.validate_gate_c).parameters),
            (
                "generated_at_utc",
                "valid_until_utc",
                "evaluated_at_utc",
            ),
        )
        self.assertEqual(
            tuple(inspect.signature(compose_capability_host).parameters),
            ("evaluated_at_utc", "gate_d_attestation_root"),
        )
        with self.assertRaises(TypeError):
            cast(Any, compose_capability_host)(
                evaluated_at_utc=_EVALUATED_AT,
                repository_root=_REPOSITORY_ROOT,
            )

        for forbidden in (
            "approved_roots_path",
            "expectation",
            "manifest",
            "evaluator",
            "publisher",
            "service",
            "refresh",
            "passed",
            "enable_context",
            "enable_fuzzy",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(hasattr(owner, forbidden))
                self.assertFalse(hasattr(composition.host, forbidden))
                self.assertFalse(
                    hasattr(composition.host.retrieval_snapshot(), forbidden)
                )
                self.assertFalse(
                    hasattr(
                        composition.host.retrieval_snapshot().query_port,
                        forbidden,
                    )
                )

    def test_gate_c_owner_and_generation_observer_are_host_bound(self) -> None:
        first = _composition()
        second = _composition()
        observer = cast(Any, first.host).retrieval_generation_notifications()

        self.assertEqual(
            tuple(name for name in dir(observer) if not name.startswith("_")),
            ("current", "wait_for_change"),
        )
        self.assertEqual(observer.current(), 0)
        for forbidden in (
            "publish",
            "refresh",
            "increment",
            "set_generation",
            "manifest",
            "publisher",
        ):
            self.assertFalse(hasattr(observer, forbidden))

        with self.assertRaises(ValueError):
            CapabilityHostComposition(
                host=first.host,
                matcher_validation_owner=first.matcher_validation_owner,
                retrieval_gate_c_validation_owner=(
                    _gate_c_owner(second)
                ),
            )

    def test_public_retrieval_graph_stays_frozen_and_query_only_after_gate_c(
        self,
    ) -> None:
        composition = _composition()
        snapshot = _gate_c_owner(composition).validate_gate_c(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )

        self.assertTrue(dataclasses.is_dataclass(snapshot))
        self.assertTrue(cast(Any, type(snapshot)).__dataclass_params__.frozen)
        self.assertFalse(hasattr(snapshot, "__dict__"))
        self.assertEqual(
            tuple(name for name in dir(snapshot.query_port) if not name.startswith("_")),
            ("query",),
        )
        self.assertFalse(hasattr(snapshot.query_port, "__dict__"))
        for forbidden in (
            "expectation",
            "manifest",
            "evaluator",
            "publisher",
            "service",
            "refresh",
        ):
            self.assertFalse(hasattr(snapshot, forbidden))
            self.assertFalse(hasattr(snapshot.query_port, forbidden))


class CapabilityHostGateCCompositionTests(unittest.TestCase):
    def _assert_notification_failure_is_atomic(
        self,
        *,
        fail_after_generation_assignment: bool,
    ) -> None:
        composition = _composition()
        host = composition.host
        owner = _gate_c_owner(composition)
        old_handoff = host.retrieval_snapshot()
        old_status = host.status_snapshot()
        host_private = cast(Any, host)
        old_publisher = host_private._CapabilityHost__retrieval_publisher
        old_service = host_private._CapabilityHost__retrieval_service
        old_manifest = host_private._CapabilityHost__retrieval_base_manifest
        notification = host.retrieval_generation_notifications()
        notification_type = cast(Any, type(notification))
        original_publish = cast(
            Any,
            notification_type._publish_prevalidated_locked,
        )
        failure = RuntimeError("Gate C notification publication failed")

        def failing_publish(
            observed: object,
            generation: int,
        ) -> None:
            if fail_after_generation_assignment:
                original_publish(observed, generation)
            raise failure

        with patch.object(
            notification_type,
            "_publish_prevalidated_locked",
            failing_publish,
        ):
            returned = owner.validate_gate_c(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )

        self.assertIs(returned, old_handoff)
        self.assertIs(host.retrieval_snapshot(), old_handoff)
        self.assertIs(host.status_snapshot(), old_status)
        self.assertIs(host.status_snapshot().retrieval, old_handoff.display)
        self.assertIs(
            host_private._CapabilityHost__retrieval_publisher,
            old_publisher,
        )
        self.assertIs(
            host_private._CapabilityHost__retrieval_service,
            old_service,
        )
        self.assertIs(
            host_private._CapabilityHost__retrieval_base_manifest,
            old_manifest,
        )
        self.assertEqual(notification.current(), old_handoff.generation)

    def test_notification_failure_before_generation_assignment_is_atomic(
        self,
    ) -> None:
        self._assert_notification_failure_is_atomic(
            fail_after_generation_assignment=False
        )

    def test_notification_failure_after_generation_assignment_is_atomic(
        self,
    ) -> None:
        self._assert_notification_failure_is_atomic(
            fail_after_generation_assignment=True
        )

    def test_loaded_authority_class_member_replacement_blocks_gate_c(
        self,
    ) -> None:
        authority_members = (
            (RetrievalContextDecision, "__repr__"),
            (RetrievalCapabilityExpectation, "__eq__"),
            (RetrievalCapabilityManifest, "__hash__"),
            (RetrievalCapabilityEvaluator, "evaluate"),
            (RetrievalCapabilityPublisher, "snapshot"),
            (TMRetrievalService, "query"),
            (QueryReport, "__init__"),
        )
        for authority_type, member_name in authority_members:
            with self.subTest(
                authority_type=authority_type.__name__,
                member_name=member_name,
            ):
                original = cast(Any, vars(authority_type)[member_name])

                def replacement(
                    *args: object,
                    __original: Any = original,
                    **kwargs: object,
                ) -> object:
                    return __original(*args, **kwargs)

                with patch.object(
                    authority_type,
                    member_name,
                    replacement,
                ):
                    composition = _composition()
                    old = composition.host.retrieval_snapshot()
                    returned = _gate_c_owner(
                        composition
                    ).validate_gate_c(
                        generated_at_utc=_GENERATED_AT,
                        valid_until_utc=_VALID_UNTIL,
                        evaluated_at_utc=_EVALUATED_AT,
                    )

                self.assertIs(returned, old)
                self.assertIs(composition.host.retrieval_snapshot(), old)
                self.assertEqual(old.generation, 0)

    def test_late_validator_dataclass_init_replacement_blocks_gate_c(
        self,
    ) -> None:
        original_init = cast(Any, RetrievalValidationRelease.__init__)
        replacement_calls: list[object] = []

        def replacement_init(
            release: RetrievalValidationRelease,
            *args: object,
            **kwargs: object,
        ) -> None:
            replacement_calls.append(release)
            original_init(release, *args, **kwargs)

        with patch.object(
            RetrievalValidationRelease,
            "__init__",
            replacement_init,
        ):
            composition = _composition()
            old = composition.host.retrieval_snapshot()
            returned = _gate_c_owner(composition).validate_gate_c(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )

        self.assertIs(returned, old)
        self.assertIs(composition.host.retrieval_snapshot(), old)
        self.assertEqual(old.generation, 0)
        self.assertEqual(replacement_calls, [])

    def test_late_validator_generated_init_closure_drift_blocks_gate_c(
        self,
    ) -> None:
        init_function = cast(Any, RetrievalValidationRelease.__init__)
        closure = init_function.__closure__
        if closure is None or len(closure) != 1:
            self.fail("frozen release init must retain one object closure")
        object_cell = closure[0]
        original_object = object_cell.cell_contents
        replacement_calls: list[str] = []

        class ForgedObject:
            def __setattr__(
                self,
                name: str,
                value: object,
            ) -> None:
                replacement_calls.append(name)
                object.__setattr__(self, name, value)

        object_cell.cell_contents = ForgedObject
        try:
            composition = _composition()
            old = composition.host.retrieval_snapshot()
            returned = _gate_c_owner(composition).validate_gate_c(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
        finally:
            object_cell.cell_contents = original_object

        self.assertIs(returned, old)
        self.assertIs(composition.host.retrieval_snapshot(), old)
        self.assertEqual(old.generation, 0)
        self.assertEqual(replacement_calls, [])

    def test_precomposition_snapshot_init_replacement_cannot_open_expired_gate(
        self,
    ) -> None:
        original_init = cast(Any, RetrievalCapabilitySnapshot.__init__)
        replacement_calls: list[str] = []

        def replacement_init(
            snapshot: RetrievalCapabilitySnapshot,
            *args: object,
            **kwargs: object,
        ) -> None:
            original_init(snapshot, *args, **kwargs)
            context = snapshot.context
            if (
                not context.available
                and context.unavailable_code is not None
                and "EXPIRED" in context.unavailable_code
            ):
                replacement_calls.append(context.unavailable_code)
                object.__setattr__(
                    snapshot,
                    "context",
                    RetrievalContextDecision(
                        available=True,
                        unavailable_code=None,
                    ),
                )

        with patch.object(
            RetrievalCapabilitySnapshot,
            "__init__",
            replacement_init,
        ):
            composition = _composition()
            old = composition.host.retrieval_snapshot()
            returned = _gate_c_owner(composition).validate_gate_c(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_VALID_UNTIL,
            )

        self.assertIs(returned, old)
        self.assertIs(composition.host.retrieval_snapshot(), old)
        self.assertEqual(old.generation, 0)
        self.assertEqual(replacement_calls, [])

    def test_precomposition_refresh_replacement_cannot_open_expired_gate_c(
        self,
    ) -> None:
        release = _core_release()
        assert release.manifest is not None
        evaluator = RetrievalCapabilityEvaluator(release.expectation)
        seed_publisher = RetrievalCapabilityPublisher(
            evaluator,
            initial_manifest=None,
            evaluated_at_utc=_EVALUATED_AT,
        )
        cached_valid_snapshot = seed_publisher.refresh(
            release.manifest,
            evaluated_at_utc=_EVALUATED_AT,
        )
        original_refresh = RetrievalCapabilityPublisher.refresh
        replacement_calls: list[datetime] = []

        def replacement_refresh(
            publisher: RetrievalCapabilityPublisher,
            manifest: object,
            *,
            evaluated_at_utc: datetime,
        ) -> object:
            if evaluated_at_utc == _VALID_UNTIL:
                replacement_calls.append(evaluated_at_utc)
                setattr(
                    publisher,
                    "_RetrievalCapabilityPublisher__snapshot",
                    cached_valid_snapshot,
                )
                return cached_valid_snapshot
            return original_refresh(
                publisher,
                cast(Any, manifest),
                evaluated_at_utc=evaluated_at_utc,
            )

        with patch.object(
            RetrievalCapabilityPublisher,
            "refresh",
            replacement_refresh,
        ):
            composition = _composition()
            old = composition.host.retrieval_snapshot()
            returned = _gate_c_owner(composition).validate_gate_c(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_VALID_UNTIL,
            )

        self.assertIs(returned, old)
        self.assertIs(composition.host.retrieval_snapshot(), old)
        self.assertEqual(old.generation, 0)
        self.assertEqual(replacement_calls, [])

    def test_same_source_alias_module_cannot_mint_gate_c(self) -> None:
        module_path = Path(cast(str, retrieval_validation_module.__file__)).resolve(
            strict=True
        )
        alias_name = "gate_c_validation_alias_for_test"
        spec = importlib.util.spec_from_file_location(alias_name, module_path)
        if spec is None or spec.loader is None:
            raise AssertionError("validator alias spec must have a loader")
        alias_module = importlib.util.module_from_spec(spec)
        sys.modules[alias_name] = alias_module
        try:
            spec.loader.exec_module(alias_module)
            with patch.dict(
                sys.modules,
                {"tm_retrieval_validation": alias_module},
            ):
                composition = _composition()
                old = composition.host.retrieval_snapshot()
                returned = _gate_c_owner(composition).validate_gate_c(
                    generated_at_utc=_GENERATED_AT,
                    valid_until_utc=_VALID_UNTIL,
                    evaluated_at_utc=_EVALUATED_AT,
                )
        finally:
            sys.modules.pop(alias_name, None)

        self.assertIs(returned, old)
        self.assertIs(composition.host.retrieval_snapshot(), old)
        self.assertEqual(old.generation, 0)

    def test_precomposition_validator_helper_replacement_cannot_mint_gate_c(
        self,
    ) -> None:
        fixture = (
            _REPOSITORY_ROOT
            / "tests"
            / "fixtures"
            / "retrieval_gate_c_vectors_v1.json"
        )
        cached_transcript = cast(
            Any,
            retrieval_validation_module,
        )._observe_context_transcript(fixture)
        replacement_calls: list[Path] = []

        def cached_observation(path: Path) -> object:
            replacement_calls.append(path)
            return cached_transcript

        with patch.object(
            retrieval_validation_module,
            "_observe_context_transcript",
            cached_observation,
        ):
            composition = _composition()
            old = composition.host.retrieval_snapshot()
            returned = _gate_c_owner(composition).validate_gate_c(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )

        self.assertIs(returned, old)
        self.assertIs(composition.host.retrieval_snapshot(), old)
        self.assertEqual(old.generation, 0)
        self.assertEqual(replacement_calls, [])

    def test_precomposition_imported_core_helper_replacement_is_rejected(
        self,
    ) -> None:
        original = cast(Any, retrieval_validation_module).canonical_digest
        replacement_calls: list[object] = []

        def replacement(value: object) -> str:
            replacement_calls.append(value)
            return cast(str, original(value))

        with patch.object(
            retrieval_validation_module,
            "canonical_digest",
            replacement,
        ):
            composition = _composition()
            old = composition.host.retrieval_snapshot()
            returned = _gate_c_owner(composition).validate_gate_c(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )

        self.assertIs(returned, old)
        self.assertIs(composition.host.retrieval_snapshot(), old)
        self.assertEqual(replacement_calls, [])

    def test_precomposition_validator_replacement_cannot_mint_gate_c(
        self,
    ) -> None:
        release = _core_release()
        original = retrieval_validation_module.recompute_retrieval_validation
        module_path = Path(cast(str, retrieval_validation_module.__file__)).resolve(
            strict=True
        )
        replacement_namespace: dict[str, object] = {}
        cached_release_name = "__gate_c_cached_release_for_test"
        replacement_source = """
def recompute_retrieval_validation(
    *,
    repository_root,
    approved_roots_path=_DEFAULT_APPROVED_ROOTS,
    generated_at_utc,
    valid_until_utc,
):
    return __gate_c_cached_release_for_test
"""
        retrieval_validation_module.__dict__[cached_release_name] = release
        try:
            exec(
                compile(replacement_source, str(module_path), "exec"),
                retrieval_validation_module.__dict__,
                replacement_namespace,
            )
            replacement = cast(
                Any,
                replacement_namespace["recompute_retrieval_validation"],
            )
            self.assertIsNot(replacement, original)
            self.assertEqual(replacement.__name__, original.__name__)
            self.assertEqual(replacement.__qualname__, original.__qualname__)
            self.assertEqual(replacement.__code__.co_filename, str(module_path))
            self.assertIs(replacement.__globals__, original.__globals__)
            original_kwdefaults = original.__kwdefaults__
            if original_kwdefaults is None:
                raise AssertionError("Core validator must bind approved roots")
            self.assertEqual(
                replacement.__kwdefaults__["approved_roots_path"],
                original_kwdefaults["approved_roots_path"],
            )

            with patch.object(
                retrieval_validation_module,
                "recompute_retrieval_validation",
                replacement,
            ):
                composition = _composition()
                old = composition.host.retrieval_snapshot()
                returned = _gate_c_owner(composition).validate_gate_c(
                    generated_at_utc=_GENERATED_AT,
                    valid_until_utc=_VALID_UNTIL,
                    evaluated_at_utc=_EVALUATED_AT,
                )
        finally:
            retrieval_validation_module.__dict__.pop(cached_release_name, None)

        self.assertIs(returned, old)
        self.assertIs(composition.host.retrieval_snapshot(), old)
        self.assertEqual(old.generation, 0)

    def test_current_checkout_and_core_bindings_are_rechecked_before_recompute(
        self,
    ) -> None:
        composition = _composition()
        owner = _gate_c_owner(composition)
        old = composition.host.retrieval_snapshot()
        private_owner = cast(Any, owner)
        checkout = (
            private_owner._RetrievalGateCValidationOwner__checkout_identity
        )
        root_identity = checkout.root
        original_path = root_identity.path
        with tempfile.TemporaryDirectory(
            prefix="localcat-gate-c-checkout-",
            dir="/private/tmp",
        ) as raw_root:
            object.__setattr__(root_identity, "path", Path(raw_root))
            try:
                returned = owner.validate_gate_c(
                    generated_at_utc=_GENERATED_AT,
                    valid_until_utc=_VALID_UNTIL,
                    evaluated_at_utc=_EVALUATED_AT,
                )
            finally:
                object.__setattr__(root_identity, "path", original_path)
        self.assertIs(returned, old)

        with patch(
            "capability_host.RetrievalCapabilityPublisher",
            object(),
        ):
            returned = owner.validate_gate_c(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
        self.assertIs(returned, old)
        self.assertIs(composition.host.retrieval_snapshot(), old)

        with patch(
            "tm_retrieval_validation._observe_context_transcript",
            side_effect=AssertionError(
                "replaced Core validation dependency must not execute"
            ),
        ) as replaced_dependency:
            returned = owner.validate_gate_c(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
        replaced_dependency.assert_not_called()
        self.assertIs(returned, old)

    def test_paired_release_builds_new_service_without_refreshing_sentinel(
        self,
    ) -> None:
        composition = _composition()
        host = composition.host
        owner = _gate_c_owner(composition)
        old = host.retrieval_snapshot()
        old_service = _private_service(old)
        sentinel = cast(Any, old_service)._capability_publisher
        sentinel_snapshot = sentinel.snapshot()
        matcher_before = composition.matcher_validation_owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        self.assertIs(
            matcher_before.display.state,
            TextMatcherState.BASIC_VALIDATED,
        )
        new = owner.validate_gate_c(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )

        self.assertIs(host.retrieval_snapshot(), new)
        self.assertIsNot(new, old)
        self.assertIsNot(new.query_port, old.query_port)
        self.assertEqual(old.generation, 0)
        self.assertEqual(new.generation, 1)
        self.assertFalse(old.display.context_available)
        self.assertTrue(new.display.context_available)
        self.assertFalse(new.display.fuzzy_available)
        self.assertIs(host.status_snapshot().retrieval, new.display)
        self.assertIs(host.matcher_snapshot(), matcher_before)
        self.assertEqual(host.matcher_snapshot().generation, 1)
        observer = cast(Any, host).retrieval_generation_notifications()
        self.assertEqual(observer.current(), 1)

        new_service = _private_service(new)
        formal_publisher = cast(Any, new_service)._capability_publisher
        self.assertIsNot(formal_publisher, sentinel)
        self.assertIs(sentinel.snapshot(), sentinel_snapshot)
        capability = formal_publisher.snapshot()
        self.assertTrue(capability.context.available)
        self.assertTrue(capability.fuzzy_core.available)
        self.assertEqual(
            capability.fuzzy_available_for("FTS5_TRIGRAM")[0],
            False,
        )
        self.assertEqual(
            capability.fuzzy_available_for("GRAM_FALLBACK")[0],
            False,
        )

    def test_expired_or_failed_refresh_preserves_existing_handoff_and_generation(
        self,
    ) -> None:
        composition = _composition()
        host = composition.host
        owner = _gate_c_owner(composition)
        initial = host.retrieval_snapshot()
        observer = cast(Any, host).retrieval_generation_notifications()

        expired = owner.validate_gate_c(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_VALID_UNTIL,
        )
        self.assertIs(expired, initial)
        self.assertIs(host.retrieval_snapshot(), initial)
        self.assertEqual(observer.current(), 0)

        opened = owner.validate_gate_c(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        self.assertEqual(opened.generation, 1)
        preserved = owner.validate_gate_c(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_VALID_UNTIL,
        )
        self.assertIs(preserved, opened)
        self.assertIs(host.retrieval_snapshot(), opened)
        self.assertEqual(observer.current(), 1)

    def test_recompute_constructor_or_refresh_failure_never_partially_swaps(
        self,
    ) -> None:
        release = _core_release()
        for failure_target in (
            "recompute",
            "publisher",
            "service",
            "refresh",
        ):
            with self.subTest(failure_target=failure_target):
                composition = _composition()
                host = composition.host
                owner = _gate_c_owner(composition)
                old = host.retrieval_snapshot()
                observer = cast(Any, host).retrieval_generation_notifications()
                binding = cast(
                    Any,
                    owner,
                )._RetrievalGateCValidationOwner__validation_binding

                if failure_target == "recompute":
                    contexts = (
                        patch.object(
                            type(binding),
                            "recompute",
                            side_effect=OSError("recomputation failed"),
                        ),
                    )
                else:
                    contexts = (
                        patch.object(
                            type(binding),
                            "recompute",
                            return_value=release,
                        ),
                        patch.object(
                            type(binding),
                            "compose_service",
                            side_effect=RuntimeError(
                                f"{failure_target} graph failed"
                            ),
                        ),
                    )
                with ExitStack() as stack:
                    for context in contexts:
                        stack.enter_context(context)
                    returned = owner.validate_gate_c(
                        generated_at_utc=_GENERATED_AT,
                        valid_until_utc=_VALID_UNTIL,
                        evaluated_at_utc=_EVALUATED_AT,
                    )

                self.assertIs(returned, old)
                self.assertIs(host.retrieval_snapshot(), old)
                self.assertEqual(observer.current(), 0)

    def test_programmer_errors_propagate_without_swapping_service(self) -> None:
        release = _core_release()
        for failure_target in (
            "recompute",
            "evaluator",
            "publisher",
            "service",
            "refresh",
            "install",
        ):
            for error_type in (AssertionError, AttributeError, TypeError):
                with self.subTest(
                    failure_target=failure_target,
                    error_type=error_type.__name__,
                ):
                    composition = _composition()
                    host = composition.host
                    owner = _gate_c_owner(composition)
                    old = host.retrieval_snapshot()
                    observer = cast(
                        Any,
                        host,
                    ).retrieval_generation_notifications()
                    binding = cast(
                        Any,
                        owner,
                    )._RetrievalGateCValidationOwner__validation_binding
                    error = error_type(
                        f"{failure_target} programmer error"
                    )

                    contexts: tuple[object, ...]
                    if failure_target == "recompute":
                        contexts = (
                            patch.object(
                                type(binding),
                                "recompute",
                                side_effect=error,
                            ),
                        )
                    else:
                        contexts = (
                            patch.object(
                                type(binding),
                                "recompute",
                                return_value=release,
                            ),
                        )
                        if failure_target in {
                            "evaluator",
                            "publisher",
                            "service",
                            "refresh",
                        }:
                            contexts += (
                                patch.object(
                                    type(binding),
                                    "compose_service",
                                    side_effect=error,
                                ),
                            )
                        else:
                            contexts += (
                                patch.object(
                                    type(host),
                                    "_install_gate_c_service",
                                    side_effect=error,
                                ),
                            )

                    with ExitStack() as stack:
                        for context in contexts:
                            stack.enter_context(cast(Any, context))
                        with self.assertRaises(error_type):
                            owner.validate_gate_c(
                                generated_at_utc=_GENERATED_AT,
                                valid_until_utc=_VALID_UNTIL,
                                evaluated_at_utc=_EVALUATED_AT,
                            )

                    self.assertIs(host.retrieval_snapshot(), old)
                    self.assertEqual(observer.current(), 0)

    def test_foreign_or_mismatched_release_cannot_replace_current_service(
        self,
    ) -> None:
        required_paths = (
            "tm_candidate_index.py",
            "tm_candidate_store_contracts.py",
            "tm_contracts.py",
            "tm_gate_a.py",
            "tm_retrieval.py",
            "tm_retrieval_capability.py",
            "tm_retrieval_validation.py",
            "tm_similarity.py",
            "tm_sqlite_candidate_projection.py",
            "tm_sqlite_store.py",
            "tests/fixtures/retrieval_gate_c_vectors_v1.json",
        )
        with tempfile.TemporaryDirectory(
            prefix="localcat-gate-c-foreign-",
            dir="/private/tmp",
        ) as raw_root:
            foreign_root = Path(raw_root)
            for relative_path in required_paths:
                source = _REPOSITORY_ROOT / relative_path
                target = foreign_root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            foreign_release = recompute_retrieval_validation(
                repository_root=foreign_root,
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
            )
        self.assertIsNotNone(foreign_release.manifest)

        valid_release = _core_release()
        assert valid_release.manifest is not None
        mismatched_manifest = replace(
            valid_release.manifest,
            retrieval_build_digest="0" * 64,
        )
        mismatched_release = RetrievalValidationRelease(
            expectation=valid_release.expectation,
            manifest=mismatched_manifest,
        )

        for release in (foreign_release, mismatched_release):
            with self.subTest(release=release):
                composition = _composition()
                old = composition.host.retrieval_snapshot()
                with patch(
                    "tm_retrieval_validation.recompute_retrieval_validation",
                    return_value=release,
                ) as replacement:
                    returned = _gate_c_owner(composition).validate_gate_c(
                        generated_at_utc=_GENERATED_AT,
                        valid_until_utc=_VALID_UNTIL,
                        evaluated_at_utc=_EVALUATED_AT,
                    )
                replacement.assert_not_called()
                self.assertIs(returned, old)
                self.assertIs(composition.host.retrieval_snapshot(), old)

        mismatched_snapshot = RetrievalCapabilityEvaluator(
            valid_release.expectation
        ).evaluate(
            mismatched_manifest,
            evaluated_at_utc=_EVALUATED_AT,
        )
        self.assertFalse(mismatched_snapshot.context.available)
        self.assertFalse(mismatched_snapshot.fuzzy_core.available)

    def test_old_inflight_query_finishes_on_old_service_and_next_query_uses_new(
        self,
    ) -> None:
        composition = _composition()
        host = composition.host
        owner = _gate_c_owner(composition)
        old = host.retrieval_snapshot()
        old_service = _private_service(old)
        entered = Event()
        release = Event()
        completed: list[QueryReport] = []
        errors: list[BaseException] = []
        original_query = old_service.query

        def blocked_query(
            service: TMRetrievalService,
            resources: tuple[object, ...],
            query: TMQuery,
        ) -> QueryReport:
            del service
            entered.set()
            if not release.wait(timeout=5.0):
                raise AssertionError("query release timed out")
            return original_query(cast(Any, resources), query)

        cast(Any, old_service).query = MethodType(blocked_query, old_service)

        def run_old_query() -> None:
            try:
                completed.append(old.query_port.query((), _EMPTY_QUERY))
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=run_old_query)
        thread.start()
        self.assertTrue(entered.wait(timeout=5.0))
        try:
            new = owner.validate_gate_c(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
        finally:
            release.set()
            thread.join(timeout=5.0)
            cast(Any, old_service).query = original_query

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(completed), 1)
        self.assertIsNot(new.query_port, old.query_port)
        self.assertIs(host.retrieval_snapshot(), new)
        self.assertEqual(new.generation, 1)
        self.assertEqual(new.query_port.query((), _EMPTY_QUERY).results, ())

    def test_each_query_uses_one_capability_snapshot_after_swap(self) -> None:
        composition = _composition()
        new = _gate_c_owner(composition).validate_gate_c(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        new_service = _private_service(new)
        publisher = cast(Any, new_service)._capability_publisher
        original_snapshot = RetrievalCapabilityPublisher.snapshot
        captured: list[object] = []

        def record_snapshot(current: RetrievalCapabilityPublisher) -> object:
            snapshot = original_snapshot(current)
            if current is publisher:
                captured.append(snapshot)
            return snapshot

        with patch.object(
            RetrievalCapabilityPublisher,
            "snapshot",
            record_snapshot,
        ):
            report = new.query_port.query((), _EMPTY_QUERY)

        self.assertEqual(report.results, ())
        self.assertEqual(report.resource_failures, ())
        self.assertEqual(len(captured), 1)


if __name__ == "__main__":
    unittest.main()
