from __future__ import annotations

import dataclasses
import inspect
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast
from unittest.mock import patch
import unittest

from capability_gated_text_matcher import CapabilityGatedTextMatcherV1
from capability_host import (
    CapabilityHost,
    CapabilityHostComposition,
    MatcherGenerationNotificationPort,
    MatcherHandoffSnapshot,
    MatcherValidationOwnerPort,
    compose_capability_host,
)
from editor_contracts import TextMatcherDisplayState
from matcher_validation import (
    MatcherValidationRelease,
    build_validated_matcher_v1,
    recompute_matcher_validation,
)
from tm_contracts import (
    SearchOptions,
    TextMatchProfile,
    TextMatchRejectCode,
    TextMatchRejected,
    TextMatchRequest,
    TextMatchSuccess,
    TextMatcherState,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
_EXPIRED_AT = _VALID_UNTIL


def _composition() -> CapabilityHostComposition:
    return compose_capability_host(
        evaluated_at_utc=_EVALUATED_AT,
    )


def _request(
    *,
    text: str,
    query: str,
    profile: TextMatchProfile,
    match_case: bool = False,
    whole_word: bool = False,
) -> TextMatchRequest:
    return TextMatchRequest(
        text=text,
        query=query,
        profile=profile,
        options=SearchOptions(
            match_case=match_case,
            whole_word=whole_word,
        ),
    )


class CapabilityHostMatcherGateOwnershipTests(unittest.TestCase):
    def test_foreign_repository_copy_cannot_publish_matcher_capability(
        self,
    ) -> None:
        required_paths = (
            "capability_gated_text_matcher.py",
            "matcher_capability.py",
            "matcher_validation.py",
            "text_matcher.py",
            "tm_contracts.py",
            "tm_gate_a.py",
            "unicode_word_break_data.py",
            "tests/fixtures/text_matcher_v1_vectors.json",
        )
        with tempfile.TemporaryDirectory(
            prefix="localcat-matcher-foreign-",
            dir="/private/tmp",
        ) as raw_root:
            foreign_root = Path(raw_root)
            for relative_path in required_paths:
                source = _REPOSITORY_ROOT / relative_path
                target = foreign_root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

            with self.assertRaises(TypeError):
                cast(Any, compose_capability_host)(
                    repository_root=foreign_root,
                    evaluated_at_utc=_EVALUATED_AT,
                )

    def test_replaced_factory_cannot_inject_foreign_validated_matcher(
        self,
    ) -> None:
        required_paths = (
            "capability_gated_text_matcher.py",
            "matcher_capability.py",
            "matcher_validation.py",
            "text_matcher.py",
            "tm_contracts.py",
            "tm_gate_a.py",
            "unicode_word_break_data.py",
            "tests/fixtures/text_matcher_v1_vectors.json",
        )
        with tempfile.TemporaryDirectory(
            prefix="localcat-matcher-foreign-factory-",
            dir="/private/tmp",
        ) as raw_root:
            foreign_root = Path(raw_root)
            for relative_path in required_paths:
                source = _REPOSITORY_ROOT / relative_path
                target = foreign_root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            foreign_matcher = build_validated_matcher_v1(
                repository_root=foreign_root,
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
                include_full=False,
            )
        self.assertIs(
            foreign_matcher.capability().state,
            TextMatcherState.BASIC_VALIDATED,
        )

        composition = _composition()
        with patch(
            "capability_host.build_validated_matcher_v1",
            return_value=foreign_matcher,
        ):
            snapshot = composition.matcher_validation_owner.validate_basic(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )

        self.assertIs(snapshot.display.state, TextMatcherState.UNAVAILABLE)
        self.assertIsNone(snapshot.matcher)

    def test_factory_code_defaults_module_and_core_global_are_bound(
        self,
    ) -> None:
        composition = _composition()
        owner = composition.matcher_validation_owner
        factory = build_validated_matcher_v1

        def validate_unavailable() -> None:
            snapshot = owner.validate_basic(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            self.assertIs(
                snapshot.display.state,
                TextMatcherState.UNAVAILABLE,
            )
            self.assertIsNone(snapshot.matcher)

        def replacement_factory(
            *,
            repository_root: Path,
            approved_roots_path: Path,
            generated_at_utc: datetime,
            valid_until_utc: datetime,
            evaluated_at_utc: datetime,
            include_full: bool,
        ) -> CapabilityGatedTextMatcherV1:
            del (
                repository_root,
                approved_roots_path,
                generated_at_utc,
                valid_until_utc,
                evaluated_at_utc,
                include_full,
            )
            raise AssertionError("replaced factory code must not execute")

        original_code = factory.__code__
        factory.__code__ = replacement_factory.__code__
        try:
            validate_unavailable()
        finally:
            factory.__code__ = original_code

        original_defaults = factory.__defaults__
        factory.__defaults__ = (object(),)
        try:
            validate_unavailable()
        finally:
            factory.__defaults__ = original_defaults

        original_kwdefaults = factory.__kwdefaults__
        factory.__kwdefaults__ = dict(original_kwdefaults or {})
        try:
            validate_unavailable()
        finally:
            factory.__kwdefaults__ = original_kwdefaults

        original_module = factory.__module__
        factory.__module__ = "foreign_matcher_validation"
        try:
            validate_unavailable()
        finally:
            factory.__module__ = original_module

        with patch(
            "matcher_validation.build_validated_matcher_v1",
            side_effect=AssertionError(
                "replaced Core module global must not execute"
            ),
        ) as replaced_core_global:
            validate_unavailable()
        replaced_core_global.assert_not_called()

        valid_release = recompute_matcher_validation(
            repository_root=_REPOSITORY_ROOT,
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            include_full=False,
        )
        self.assertIsNotNone(valid_release.manifest)
        with patch(
            "matcher_validation.recompute_matcher_validation",
            return_value=valid_release,
        ) as replaced_factory_dependency:
            validate_unavailable()
        replaced_factory_dependency.assert_not_called()

        self.assertIs(factory.__closure__, None)

    def test_factory_source_identity_drift_closes_before_invocation(self) -> None:
        composition = _composition()
        owner = cast(Any, composition.matcher_validation_owner)
        binding = owner._MatcherValidationOwner__factory_binding
        source_identity = binding.source
        original_path = source_identity.path
        with tempfile.TemporaryDirectory(
            prefix="localcat-matcher-factory-source-",
            dir="/private/tmp",
        ) as raw_root:
            foreign_source = Path(raw_root) / "matcher_validation.py"
            shutil.copy2(original_path, foreign_source)
            object.__setattr__(source_identity, "path", foreign_source)
            try:
                snapshot = owner.validate_basic(
                    generated_at_utc=_GENERATED_AT,
                    valid_until_utc=_VALID_UNTIL,
                    evaluated_at_utc=_EVALUATED_AT,
                )
            finally:
                object.__setattr__(source_identity, "path", original_path)

        self.assertIs(snapshot.display.state, TextMatcherState.UNAVAILABLE)
        self.assertIsNone(snapshot.matcher)

    def test_composition_separates_read_only_host_from_validation_owner(
        self,
    ) -> None:
        composition = _composition()
        host = composition.host
        owner = composition.matcher_validation_owner

        self.assertIs(type(host), CapabilityHost)
        self.assertIsInstance(owner, MatcherValidationOwnerPort)
        self.assertTrue(dataclasses.is_dataclass(composition))
        self.assertTrue(
            cast(Any, type(composition)).__dataclass_params__.frozen
        )
        self.assertFalse(hasattr(composition, "__dict__"))
        self.assertEqual(
            tuple(inspect.signature(compose_capability_host).parameters),
            ("evaluated_at_utc",),
        )
        self.assertEqual(
            tuple(
                name
                for name in dir(owner)
                if not name.startswith("_")
            ),
            ("validate_basic", "validate_text_v1"),
        )

        for forbidden in (
            "manifest",
            "publisher",
            "evaluator",
            "approved_roots_path",
            "include_full",
            "refresh",
            "publish",
            "set_state",
            "enable_text_v1",
        ):
            with self.subTest(owner_forbidden=forbidden):
                self.assertFalse(hasattr(owner, forbidden))
        for forbidden in (
            "start_validation",
            "validate_basic",
            "validate_text_v1",
            "refresh",
            "publish",
            "set_state",
            "enable_match_case",
            "enable_whole_word",
        ):
            with self.subTest(host_forbidden=forbidden):
                self.assertFalse(hasattr(host, forbidden))

        for method_name in ("validate_basic", "validate_text_v1"):
            parameters = inspect.signature(
                getattr(owner, method_name)
            ).parameters
            self.assertEqual(
                tuple(parameters),
                (
                    "generated_at_utc",
                    "valid_until_utc",
                    "evaluated_at_utc",
                ),
            )

    def test_generation_notification_port_is_observation_only(self) -> None:
        composition = _composition()
        port = composition.host.matcher_generation_notifications()

        self.assertIsInstance(port, MatcherGenerationNotificationPort)
        self.assertEqual(
            tuple(
                name
                for name in dir(port)
                if not name.startswith("_")
            ),
            ("current", "wait_for_change"),
        )
        self.assertFalse(hasattr(port, "__dict__"))
        for forbidden in (
            "publish",
            "refresh",
            "set_generation",
            "increment",
            "manifest",
            "publisher",
        ):
            self.assertFalse(hasattr(port, forbidden))

    def test_reflective_mutation_and_cross_host_owner_pairing_are_rejected(
        self,
    ) -> None:
        first = _composition()
        second = _composition()
        host = first.host
        notification = cast(
            Any,
            host.matcher_generation_notifications(),
        )

        with self.assertRaises(PermissionError):
            notification._publish_locked(object(), 1)
        self.assertEqual(notification.current(), 0)
        with self.assertRaises(PermissionError):
            cast(Any, host)._install_core_matcher(
                owner_identity=object(),
                matcher=None,
                capability=None,
            )
        self.assertEqual(host.matcher_snapshot().generation, 0)
        with self.assertRaises(PermissionError):
            cast(Any, host)._composition_matcher_owner(
                object(),
            )
        with self.assertRaises(ValueError):
            CapabilityHostComposition(
                host=host,
                matcher_validation_owner=(
                    second.matcher_validation_owner
                ),
            )

    def test_matcher_gate_has_no_retrieval_or_storage_inference_imports(
        self,
    ) -> None:
        source = inspect.getsource(
            type(_composition().matcher_validation_owner)
        )
        for forbidden in (
            "tm_sqlite_store",
            "tm_retrieval_validation",
            "tm_benchmark_gate",
            "FTS5_TRIGRAM",
            "GRAM_FALLBACK",
            "store_healthy",
            "fuzzy_passed",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_symlink_or_renamed_checkout_identity_closes_before_factory(
        self,
    ) -> None:
        composition = _composition()
        owner = cast(Any, composition.matcher_validation_owner)
        identity = owner._MatcherValidationOwner__checkout_identity
        root_identity = identity.root
        original_path = root_identity.path
        with tempfile.TemporaryDirectory(
            prefix="localcat-matcher-identity-",
            dir="/private/tmp",
        ) as raw_root:
            temp_root = Path(raw_root)
            symlink_root = temp_root / "checkout-link"
            symlink_root.symlink_to(_REPOSITORY_ROOT, target_is_directory=True)
            renamed_old_path = temp_root / "renamed-checkout-old-path"
            for invalid_path in (symlink_root, renamed_old_path):
                with self.subTest(path=invalid_path):
                    object.__setattr__(root_identity, "path", invalid_path)
                    try:
                        with patch(
                            "capability_host.build_validated_matcher_v1",
                            side_effect=AssertionError(
                                "foreign identity must close before factory"
                            ),
                        ) as core_factory:
                            snapshot = owner.validate_basic(
                                generated_at_utc=_GENERATED_AT,
                                valid_until_utc=_VALID_UNTIL,
                                evaluated_at_utc=_EVALUATED_AT,
                            )
                        core_factory.assert_not_called()
                    finally:
                        object.__setattr__(
                            root_identity,
                            "path",
                            original_path,
                        )
                    self.assertIs(
                        snapshot.display.state,
                        TextMatcherState.UNAVAILABLE,
                    )
                    self.assertIsNone(snapshot.matcher)

    def test_checkout_identity_is_rechecked_after_core_source_walk(
        self,
    ) -> None:
        composition = _composition()
        owner = cast(Any, composition.matcher_validation_owner)
        identity = owner._MatcherValidationOwner__checkout_identity

        with patch.object(
            type(identity),
            "is_current",
            side_effect=(True, False),
        ) as identity_check:
            snapshot = owner.validate_basic(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )

        self.assertEqual(identity_check.call_count, 2)
        self.assertIs(snapshot.display.state, TextMatcherState.UNAVAILABLE)
        self.assertIsNone(snapshot.matcher)


class CapabilityHostMatcherGateStateTests(unittest.TestCase):
    def test_core_factory_is_the_only_matcher_constructor_and_drives_state(
        self,
    ) -> None:
        composition = _composition()
        owner = cast(Any, composition.matcher_validation_owner)
        binding = owner._MatcherValidationOwner__factory_binding
        self.assertIs(binding.function, build_validated_matcher_v1)
        self.assertTrue(binding.is_current())
        self.assertEqual(
            binding.source.path.parent,
            _REPOSITORY_ROOT,
        )
        self.assertEqual(
            binding.approved_roots.path,
            _REPOSITORY_ROOT
            / "tests/fixtures/feature5_gate_a_v1.json",
        )

        basic = owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        factory_source = Path(
            build_validated_matcher_v1.__code__.co_filename
        ).resolve(strict=True)
        self.assertEqual(factory_source.parent, _REPOSITORY_ROOT)
        self.assertIs(type(basic.matcher), CapabilityGatedTextMatcherV1)
        self.assertIs(basic.display.state, TextMatcherState.BASIC_VALIDATED)
        self.assertEqual(
            basic.display.supported_profiles,
            (
                TextMatchProfile.LEGACY_COMPAT,
                TextMatchProfile.BASIC_CONTIGUOUS,
            ),
        )

        full = owner.validate_text_v1(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        self.assertIs(type(full.matcher), CapabilityGatedTextMatcherV1)
        self.assertIs(
            full.display.state,
            TextMatcherState.TEXT_V1_VALIDATED,
        )
        self.assertEqual(
            full.display.supported_profiles,
            (
                TextMatchProfile.LEGACY_COMPAT,
                TextMatchProfile.BASIC_CONTIGUOUS,
                TextMatchProfile.CONFIGURABLE_TEXT_V1,
            ),
        )

    def test_missing_expired_and_foreign_validation_close_the_handoff(
        self,
    ) -> None:
        base_release = recompute_matcher_validation(
            repository_root=_REPOSITORY_ROOT,
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            include_full=False,
        )
        self.assertIsNotNone(base_release.manifest)

        cases: list[tuple[str, Any, datetime]] = [
            (
                "missing",
                patch(
                    "matcher_validation.recompute_matcher_validation",
                    return_value=MatcherValidationRelease(
                        expectation=base_release.expectation,
                        manifest=None,
                    ),
                ),
                _EVALUATED_AT,
            ),
            ("expired", None, _EXPIRED_AT),
            (
                "foreign-artifact",
                patch(
                    "matcher_validation.aggregate_paths_digest",
                    return_value="9" * 64,
                ),
                _EVALUATED_AT,
            ),
        ]
        for name, context, evaluated_at in cases:
            with self.subTest(case=name):
                composition = _composition()
                owner = composition.matcher_validation_owner
                if context is None:
                    snapshot = owner.validate_basic(
                        generated_at_utc=_GENERATED_AT,
                        valid_until_utc=_VALID_UNTIL,
                        evaluated_at_utc=evaluated_at,
                    )
                else:
                    with context:
                        snapshot = owner.validate_basic(
                            generated_at_utc=_GENERATED_AT,
                            valid_until_utc=_VALID_UNTIL,
                            evaluated_at_utc=evaluated_at,
                        )
                self.assertIs(snapshot.display.state, TextMatcherState.UNAVAILABLE)
                self.assertIsNone(snapshot.matcher)
                self.assertEqual(snapshot.display.supported_profiles, ())
                self.assertEqual(
                    snapshot.display.safe_reason,
                    "MATCHER.VALIDATION_UNAVAILABLE",
                )

    def test_basic_only_executes_fixed_contiguous_profile(self) -> None:
        composition = _composition()
        snapshot = composition.matcher_validation_owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        matcher = snapshot.matcher
        self.assertIsNotNone(matcher)
        assert matcher is not None

        basic = matcher.match(
            _request(
                text="Straße STRASSE",
                query="strasse",
                profile=TextMatchProfile.BASIC_CONTIGUOUS,
            )
        )
        self.assertIsInstance(basic, TextMatchSuccess)
        assert isinstance(basic, TextMatchSuccess)
        self.assertEqual(
            tuple((hit.start_index, hit.end_index) for hit in basic.hits),
            ((0, 6), (7, 14)),
        )

        for match_case, whole_word in ((True, False), (False, True)):
            with self.subTest(match_case=match_case, whole_word=whole_word):
                rejected = matcher.match(
                    _request(
                        text="alpha alphabet",
                        query="alpha",
                        profile=TextMatchProfile.BASIC_CONTIGUOUS,
                        match_case=match_case,
                        whole_word=whole_word,
                    )
                )
                self.assertIsInstance(rejected, TextMatchRejected)
                assert isinstance(rejected, TextMatchRejected)
                self.assertIs(
                    rejected.code,
                    TextMatchRejectCode.OPTIONS_NOT_ALLOWED,
                )

        configurable = matcher.match(
            _request(
                text="alpha",
                query="alpha",
                profile=TextMatchProfile.CONFIGURABLE_TEXT_V1,
            )
        )
        self.assertIsInstance(configurable, TextMatchRejected)
        assert isinstance(configurable, TextMatchRejected)
        self.assertIs(
            configurable.code,
            TextMatchRejectCode.PROFILE_NOT_VALIDATED,
        )

    def test_text_v1_enables_configurable_options_and_cjk_contiguous_semantics(
        self,
    ) -> None:
        composition = _composition()
        snapshot = composition.matcher_validation_owner.validate_text_v1(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        matcher = snapshot.matcher
        self.assertIsNotNone(matcher)
        assert matcher is not None

        case_sensitive = matcher.match(
            _request(
                text="Alpha alpha",
                query="alpha",
                profile=TextMatchProfile.CONFIGURABLE_TEXT_V1,
                match_case=True,
            )
        )
        case_folded = matcher.match(
            _request(
                text="Alpha alpha",
                query="alpha",
                profile=TextMatchProfile.CONFIGURABLE_TEXT_V1,
                match_case=False,
            )
        )
        whole_word = matcher.match(
            _request(
                text="catalog cat",
                query="cat",
                profile=TextMatchProfile.CONFIGURABLE_TEXT_V1,
                whole_word=True,
            )
        )
        for outcome in (case_sensitive, case_folded, whole_word):
            self.assertIsInstance(outcome, TextMatchSuccess)
        assert isinstance(case_sensitive, TextMatchSuccess)
        assert isinstance(case_folded, TextMatchSuccess)
        assert isinstance(whole_word, TextMatchSuccess)
        self.assertEqual(
            tuple((hit.start_index, hit.end_index) for hit in case_sensitive.hits),
            ((6, 11),),
        )
        self.assertEqual(
            tuple((hit.start_index, hit.end_index) for hit in case_folded.hits),
            ((0, 5), (6, 11)),
        )
        self.assertEqual(
            tuple((hit.start_index, hit.end_index) for hit in whole_word.hits),
            ((8, 11),),
        )

        cjk_without_whole_word = matcher.match(
            _request(
                text="办公室里办公室",
                query="办公室",
                profile=TextMatchProfile.CONFIGURABLE_TEXT_V1,
                whole_word=False,
            )
        )
        cjk_with_whole_word = matcher.match(
            _request(
                text="办公室里办公室",
                query="办公室",
                profile=TextMatchProfile.CONFIGURABLE_TEXT_V1,
                whole_word=True,
            )
        )
        self.assertIsInstance(cjk_without_whole_word, TextMatchSuccess)
        self.assertIsInstance(cjk_with_whole_word, TextMatchSuccess)
        assert isinstance(cjk_without_whole_word, TextMatchSuccess)
        assert isinstance(cjk_with_whole_word, TextMatchSuccess)
        self.assertEqual(
            cjk_with_whole_word.hits,
            cjk_without_whole_word.hits,
        )


class CapabilityHostMatcherGateRefreshTests(unittest.TestCase):
    def _assert_notification_failure_is_atomic(
        self,
        *,
        fail_after_generation_assignment: bool,
    ) -> None:
        composition = _composition()
        host = composition.host
        owner = composition.matcher_validation_owner
        old_handoff = host.matcher_snapshot()
        old_status = host.status_snapshot()
        notification = host.matcher_generation_notifications()
        notification_type = cast(Any, type(notification))
        original_publish = cast(
            Any,
            notification_type._publish_prevalidated_locked,
        )
        failure = RuntimeError("matcher notification publication failed")

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
            with self.assertRaises(RuntimeError) as raised:
                owner.validate_basic(
                    generated_at_utc=_GENERATED_AT,
                    valid_until_utc=_VALID_UNTIL,
                    evaluated_at_utc=_EVALUATED_AT,
                )

        self.assertIs(raised.exception, failure)
        self.assertIs(host.matcher_snapshot(), old_handoff)
        self.assertIs(host.status_snapshot(), old_status)
        self.assertIs(host.status_snapshot().matcher, old_handoff.display)
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

    def test_refresh_atomically_increments_generation_and_notifies(self) -> None:
        composition = _composition()
        host = composition.host
        owner = composition.matcher_validation_owner
        notification = host.matcher_generation_notifications()
        observed: list[int | None] = []
        waiting = Event()

        def wait_for_generation() -> None:
            waiting.set()
            observed.append(
                notification.wait_for_change(
                    after_generation=0,
                    timeout=5.0,
                )
            )

        worker = Thread(target=wait_for_generation)
        worker.start()
        self.assertTrue(waiting.wait(timeout=5))
        basic = owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())

        self.assertEqual(basic.generation, 1)
        self.assertEqual(observed, [1])
        self.assertEqual(notification.current(), 1)
        self.assertIs(host.matcher_snapshot(), basic)
        self.assertIs(host.status_snapshot().matcher, basic.display)

        unavailable = owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EXPIRED_AT,
        )
        self.assertEqual(unavailable.generation, 2)
        self.assertEqual(notification.current(), 2)
        self.assertIsNone(unavailable.matcher)
        self.assertIs(host.matcher_snapshot(), unavailable)
        self.assertIs(host.status_snapshot().matcher, unavailable.display)
        self.assertIs(basic.display.state, TextMatcherState.BASIC_VALIDATED)
        self.assertIsNotNone(basic.matcher)

    def test_captured_snapshot_stays_stable_during_refresh(self) -> None:
        composition = _composition()
        owner = composition.matcher_validation_owner
        basic = owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        matcher = basic.matcher
        self.assertIsNotNone(matcher)
        assert matcher is not None

        entered = Event()
        release = Event()
        results: list[TextMatchSuccess | TextMatchRejected] = []
        original_match = CapabilityGatedTextMatcherV1.match

        def blocking_match(
            runtime: CapabilityGatedTextMatcherV1,
            request: TextMatchRequest,
        ):
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("test did not release matcher")
            return original_match(runtime, request)

        with patch.object(
            CapabilityGatedTextMatcherV1,
            "match",
            new=blocking_match,
        ):
            worker = Thread(
                target=lambda: results.append(
                    matcher.match(
                        _request(
                            text="alpha ALPHA",
                            query="alpha",
                            profile=TextMatchProfile.BASIC_CONTIGUOUS,
                        )
                    )
                )
            )
            worker.start()
            self.assertTrue(entered.wait(timeout=5))
            full = owner.validate_text_v1(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            release.set()
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())

        self.assertEqual(len(results), 1)
        outcome = results[0]
        self.assertIsInstance(outcome, TextMatchSuccess)
        assert isinstance(outcome, TextMatchSuccess)
        self.assertIs(
            outcome.capability.state,
            TextMatcherState.BASIC_VALIDATED,
        )
        self.assertIs(
            full.display.state,
            TextMatcherState.TEXT_V1_VALIDATED,
        )
        self.assertEqual(full.generation, basic.generation + 1)
        self.assertIsNot(full.matcher, basic.matcher)
        self.assertIs(composition.host.matcher_snapshot(), full)

    def test_notification_argument_validation_is_closed(self) -> None:
        port = _composition().host.matcher_generation_notifications()
        with self.assertRaises(TypeError):
            cast(Any, port).wait_for_change(
                after_generation=True,
                timeout=0.0,
            )
        with self.assertRaises(ValueError):
            port.wait_for_change(after_generation=-1, timeout=0.0)
        with self.assertRaises(TypeError):
            cast(Any, port).wait_for_change(
                after_generation=0,
                timeout=True,
            )
        with self.assertRaises(ValueError):
            port.wait_for_change(after_generation=0, timeout=-0.1)


class CapabilityHostMatcherGateContractShapeTests(unittest.TestCase):
    def test_matcher_handoff_shape_remains_frozen_and_closed(self) -> None:
        self.assertEqual(
            tuple(
                field.name
                for field in dataclasses.fields(MatcherHandoffSnapshot)
            ),
            ("generation", "matcher", "display"),
        )
        composition = _composition()
        snapshot = composition.host.matcher_snapshot()
        self.assertTrue(dataclasses.is_dataclass(snapshot))
        self.assertTrue(
            cast(Any, type(snapshot)).__dataclass_params__.frozen
        )
        self.assertFalse(hasattr(snapshot, "__dict__"))

    def test_handoff_rejects_display_that_disagrees_with_core_capability(
        self,
    ) -> None:
        composition = _composition()
        basic = composition.matcher_validation_owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )
        self.assertIsNotNone(basic.matcher)
        with self.assertRaisesRegex(ValueError, "Core capability"):
            MatcherHandoffSnapshot(
                generation=basic.generation,
                matcher=basic.matcher,
                display=TextMatcherDisplayState(
                    state=TextMatcherState.TEXT_V1_VALIDATED,
                    supported_profiles=(
                        TextMatchProfile.LEGACY_COMPAT,
                        TextMatchProfile.BASIC_CONTIGUOUS,
                        TextMatchProfile.CONFIGURABLE_TEXT_V1,
                    ),
                    safe_reason=None,
                ),
            )


if __name__ == "__main__":
    unittest.main()
