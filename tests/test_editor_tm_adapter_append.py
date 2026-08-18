"""Task 4.4 confirmed-translation TM append adapter tests."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from typing import Iterator, cast
import unittest
from unittest.mock import patch

from capability_host import CapabilityHost
from editor_contracts import (
    EditorSegment,
    ResourceConfig,
    ResourceKind,
    TMResourceWriteOutcome,
    WriteReport,
)
from editor_tm_adapter import EditorTMAdapter
from tm_application_composition import (
    CanonicalOpenBinding,
    LegacyAppendOperationError,
    LegacyOpenBinding,
    LegacyPortBackend,
    RuntimeOpenBinding,
    TMResourceResolver,
    TMRuntimeHost,
)
from tm_contracts import (
    SourceBindingState,
    StoreHealth,
    TMRecord,
    TMRecordDraft,
    TMStore,
)
from tm_engine import TMEngine, TMMatch
from tm_sqlite_store import SQLiteStoreSchemaError, SourceBindingObservation


_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _SourceMonitor:
    def __init__(self, resource_id: str) -> None:
        self._resource_id = resource_id

    def observe(self) -> SourceBindingObservation:
        return SourceBindingObservation(
            resource_id=self._resource_id,
            canonical_store_id=f"store.{self._resource_id}",
            generation=0,
            head_revision=0,
            state=SourceBindingState.VERIFIED_CURRENT,
            binding_digest=None,
            diagnostic_codes=(),
        )


class _QueryView:
    def __init__(self, resource_id: str) -> None:
        self.resource_id = resource_id
        self.generation = 0

    def health(self) -> StoreHealth:
        return StoreHealth(
            healthy=True,
            schema_version=1,
            generation=0,
            record_count=0,
            index_kind="GRAM_FALLBACK",
            snapshot_binding_digest=None,
            source_binding_state=SourceBindingState.VERIFIED_CURRENT,
            exact_available=True,
            context_available=False,
            fuzzy_available=False,
            diagnostic_codes=(),
        )


class _RecordingStore:
    def __init__(
        self,
        resource_id: str,
        *,
        call_log: list[str],
        backing_path: Path,
        error: BaseException | None = None,
        callback: object | None = None,
    ) -> None:
        self.resource_id = resource_id
        self.call_log = call_log
        self.backing_path = backing_path
        self.error = error
        self.callback = callback
        self.appended: list[TMRecordDraft] = []
        self.source_binding_monitor = _SourceMonitor(resource_id)

    @contextmanager
    def query_lease(self) -> Iterator[_QueryView]:
        yield _QueryView(self.resource_id)

    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]:
        del source_raw
        return ()

    def records_by_id(self, record_ids: tuple[int, ...]) -> tuple[TMRecord, ...]:
        del record_ids
        return ()

    def append(self, draft: TMRecordDraft) -> TMRecord:
        self.call_log.append(self.resource_id)
        self.appended.append(draft)
        if callable(self.callback):
            self.callback()
        if self.error is not None:
            raise self.error
        self.backing_path.write_bytes(
            self.backing_path.read_bytes() + b"canonical append\n"
        )
        return cast(TMRecord, object())

    def export_records(self) -> Iterator[TMRecord]:
        return iter(())

    def health(self) -> StoreHealth:
        return _QueryView(self.resource_id).health()


class _RecordingLegacyBackend:
    def __init__(
        self,
        resource_id: str,
        *,
        call_log: list[str],
        backing_path: Path,
        error: BaseException | None = None,
        callback: object | None = None,
    ) -> None:
        self.resource_id = resource_id
        self.call_log = call_log
        self.backing_path = backing_path
        self.error = error
        self.callback = callback
        self.appended: list[TMRecordDraft] = []

    def query_exact(
        self,
        source: str,
        speaker_raw: str | None,
    ) -> TMMatch | None:
        del source, speaker_raw
        return None

    def append(self, draft: TMRecordDraft) -> None:
        self.call_log.append(self.resource_id)
        self.appended.append(draft)
        if callable(self.callback):
            self.callback()
        if self.error is not None:
            raise self.error
        self.backing_path.write_bytes(
            self.backing_path.read_bytes() + b"legacy append\n"
        )


def _config(
    root: Path,
    resource_id: str,
    *,
    active: bool = True,
    lookup: bool = False,
    update: bool = True,
) -> ResourceConfig:
    return ResourceConfig(
        id=resource_id,
        name=f"Name {resource_id}",
        kind=ResourceKind.TRANSLATION_MEMORY,
        path=(root / f"{resource_id}.tm").resolve(),
        active=active,
        lookup=lookup,
        update=update,
    )


def _adapter(
    configs: tuple[ResourceConfig, ...],
    bindings: dict[Path, RuntimeOpenBinding],
) -> tuple[EditorTMAdapter, TMRuntimeHost]:
    runtime = TMRuntimeHost(
        resolver=TMResourceResolver(runtime_open=lambda path: bindings[path]),
        configs=configs,
    )
    return (
        EditorTMAdapter(
            runtime_host=runtime,
            capability_host=CapabilityHost(evaluated_at_utc=_EVALUATED_AT),
        ),
        runtime,
    )


def _binding(backend: _RecordingLegacyBackend) -> LegacyOpenBinding:
    return LegacyOpenBinding(
        backend=cast(LegacyPortBackend, cast(object, backend))
    )


def _canonical_binding(store: _RecordingStore) -> CanonicalOpenBinding:
    return CanonicalOpenBinding(
        resource_id=store.resource_id,
        store=cast(TMStore, cast(object, store)),
    )


def _append(adapter: EditorTMAdapter) -> WriteReport:
    return adapter.append_confirmed(
        segment=EditorSegment(
            id="segment-7",
            source="Raw source",
            speaker="Narrator",
        ),
        target="Raw target",
        file_source="project.json",
    )


class EditorTMAdapterAppendTests(unittest.TestCase):
    def test_write_report_adds_strict_outcomes_without_breaking_legacy_shape(
        self,
    ) -> None:
        legacy = WriteReport(
            written_resource_ids=("legacy",),
            errors=("old human-readable error",),
        )
        self.assertEqual(legacy.outcomes, ())
        self.assertFalse(legacy.succeeded)

        success = TMResourceWriteOutcome(
            resource_id="tm.success",
            resource_name="Success",
            global_order=0,
            written=True,
            error_code=None,
            retryable=False,
        )
        failure = TMResourceWriteOutcome(
            resource_id="tm.failure",
            resource_name="Failure",
            global_order=1,
            written=False,
            error_code="TM.WRITE.CANONICAL_APPEND_FAILED",
            retryable=True,
        )
        report = WriteReport(
            written_resource_ids=("tm.success",),
            errors=("TM.WRITE.CANONICAL_APPEND_FAILED",),
            outcomes=(success, failure),
        )
        self.assertEqual(report.outcomes, (success, failure))
        self.assertFalse(report.succeeded)
        with self.assertRaises(ValueError):
            WriteReport(
                written_resource_ids=("tm.failure",),
                errors=report.errors,
                outcomes=report.outcomes,
            )
        with self.assertRaises(ValueError):
            TMResourceWriteOutcome(
                resource_id="tm.failure",
                resource_name="Failure",
                global_order=1,
                written=False,
                error_code="/secret/private body",
                retryable=True,
            )
        object.__setattr__(failure, "error_code", "/secret/nested tamper")
        with self.assertRaises(ValueError):
            WriteReport(
                written_resource_ids=("tm.success",),
                errors=("TM.WRITE.CANONICAL_APPEND_FAILED",),
                outcomes=(success, failure),
            )

    def test_mixed_append_uses_one_snapshot_and_preserves_captured_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = (
                _config(root, "canonical.first"),
                _config(root, "legacy.second"),
            )
            for config in configs:
                config.path.write_bytes(b"seed\n")
            call_log: list[str] = []
            refresh_once: list[object] = []
            runtime_box: list[TMRuntimeHost] = []

            def refresh_during_first_append() -> None:
                if refresh_once:
                    return
                refresh_once.append(object())
                runtime_box[0].refresh(
                    tuple(
                        ResourceConfig(
                            id=config.id,
                            name=config.name,
                            kind=config.kind,
                            path=config.path,
                            active=config.active,
                            lookup=config.lookup,
                            update=False,
                        )
                        for config in configs
                    )
                )

            canonical = _RecordingStore(
                "canonical.first",
                call_log=call_log,
                backing_path=configs[0].path,
                callback=refresh_during_first_append,
            )
            legacy = _RecordingLegacyBackend(
                "legacy.second",
                call_log=call_log,
                backing_path=configs[1].path,
            )
            adapter, runtime = _adapter(
                configs,
                {
                    configs[0].path: _canonical_binding(canonical),
                    configs[1].path: _binding(legacy),
                },
            )
            runtime_box.append(runtime)
            original_capture = TMRuntimeHost.capture_operation_snapshot
            captures: list[TMRuntimeHost] = []

            def capture_once(host: TMRuntimeHost):  # type: ignore[no-untyped-def]
                captures.append(host)
                return original_capture(host)

            with patch.object(
                TMRuntimeHost,
                "capture_operation_snapshot",
                autospec=True,
                side_effect=capture_once,
            ):
                report = _append(adapter)

            self.assertEqual(captures, [runtime])
            self.assertEqual(runtime.snapshot().generation, 1)
            self.assertEqual(call_log, ["canonical.first", "legacy.second"])
            expected_draft = TMRecordDraft(
                source_raw="Raw source",
                target_raw="Raw target",
                speaker_raw="Narrator",
                context_prev_raw=None,
                context_next_raw=None,
                file_source="project.json",
                provenance=(("source", "local-write"),),
            )
            self.assertEqual(canonical.appended, [expected_draft])
            self.assertEqual(legacy.appended, [expected_draft])
            self.assertEqual(
                report.written_resource_ids,
                ("canonical.first", "legacy.second"),
            )
            self.assertEqual(report.errors, ())
            self.assertEqual(
                tuple(outcome.resource_id for outcome in report.outcomes),
                ("canonical.first", "legacy.second"),
            )
            self.assertTrue(report.succeeded)

    def test_no_writable_and_update_false_leave_bytes_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = (
                _config(root, "legacy.no-update", update=False),
                _config(root, "canonical.no-update", update=False),
                _config(root, "legacy.inactive", active=False, update=True),
            )
            for config in configs:
                config.path.write_bytes(f"seed {config.id}\n".encode())
            before = {config.id: _digest(config.path) for config in configs}
            call_log: list[str] = []
            legacy_no_update = _RecordingLegacyBackend(
                "legacy.no-update",
                call_log=call_log,
                backing_path=configs[0].path,
            )
            canonical_no_update = _RecordingStore(
                "canonical.no-update",
                call_log=call_log,
                backing_path=configs[1].path,
            )
            legacy_inactive = _RecordingLegacyBackend(
                "legacy.inactive",
                call_log=call_log,
                backing_path=configs[2].path,
            )
            adapter, _runtime = _adapter(
                configs,
                {
                    configs[0].path: _binding(legacy_no_update),
                    configs[1].path: _canonical_binding(canonical_no_update),
                    configs[2].path: _binding(legacy_inactive),
                },
            )

            report = _append(adapter)

            self.assertEqual(report, WriteReport())
            self.assertTrue(report.succeeded)
            self.assertEqual(call_log, [])
            self.assertEqual(
                {config.id: _digest(config.path) for config in configs},
                before,
            )

    def test_partial_operational_failures_are_body_safe_and_do_not_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = (
                _config(root, "legacy.failed"),
                _config(root, "canonical.failed"),
                _config(root, "legacy.healthy"),
            )
            for config in configs:
                config.path.write_bytes(b"seed\n")
            call_log: list[str] = []
            legacy_failed = _RecordingLegacyBackend(
                "legacy.failed",
                call_log=call_log,
                backing_path=configs[0].path,
                error=OSError("/secret/legacy/path"),
            )
            canonical_failed = _RecordingStore(
                "canonical.failed",
                call_log=call_log,
                backing_path=configs[1].path,
                error=SQLiteStoreSchemaError("SECRET.STORE.BODY"),
            )
            legacy_healthy = _RecordingLegacyBackend(
                "legacy.healthy",
                call_log=call_log,
                backing_path=configs[2].path,
            )
            adapter, _runtime = _adapter(
                configs,
                {
                    configs[0].path: _binding(legacy_failed),
                    configs[1].path: _canonical_binding(canonical_failed),
                    configs[2].path: _binding(legacy_healthy),
                },
            )

            report = _append(adapter)

            self.assertEqual(
                call_log,
                ["legacy.failed", "canonical.failed", "legacy.healthy"],
            )
            self.assertEqual(report.written_resource_ids, ("legacy.healthy",))
            self.assertEqual(
                report.errors,
                (
                    "TM.WRITE.LEGACY_APPEND_FAILED",
                    "TM.WRITE.CANONICAL_APPEND_FAILED",
                ),
            )
            self.assertEqual(
                tuple(outcome.written for outcome in report.outcomes),
                (False, False, True),
            )
            self.assertFalse(report.succeeded)
            self.assertNotIn("secret", repr(report).lower())
            self.assertNotIn(str(root), repr(report))

    def test_programmer_error_propagates_instead_of_becoming_write_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = (
                _config(root, "legacy.programmer"),
                _config(root, "canonical.must-not-run"),
            )
            for config in configs:
                config.path.write_bytes(b"seed\n")
            call_log: list[str] = []
            legacy = _RecordingLegacyBackend(
                "legacy.programmer",
                call_log=call_log,
                backing_path=configs[0].path,
                error=TypeError("programmer invariant"),
            )
            canonical = _RecordingStore(
                "canonical.must-not-run",
                call_log=call_log,
                backing_path=configs[1].path,
            )
            adapter, _runtime = _adapter(
                configs,
                {
                    configs[0].path: _binding(legacy),
                    configs[1].path: _canonical_binding(canonical),
                },
            )

            with self.assertRaisesRegex(TypeError, "programmer invariant"):
                _append(adapter)

            self.assertEqual(call_log, ["legacy.programmer"])
            self.assertEqual(canonical.appended, [])

    def test_canonical_sqlite_usage_errors_are_not_resourceized(self) -> None:
        for error_type in (
            sqlite3.ProgrammingError,
            sqlite3.NotSupportedError,
        ):
            with self.subTest(error_type=error_type.__name__):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    resource_id = f"canonical.{error_type.__name__}"
                    configs = (
                        _config(root, resource_id),
                        _config(root, "legacy.must-not-run"),
                    )
                    for config in configs:
                        config.path.write_bytes(b"seed\n")
                    call_log: list[str] = []
                    canonical = _RecordingStore(
                        resource_id,
                        call_log=call_log,
                        backing_path=configs[0].path,
                        error=error_type("programmer misuse"),
                    )
                    legacy = _RecordingLegacyBackend(
                        "legacy.must-not-run",
                        call_log=call_log,
                        backing_path=configs[1].path,
                    )
                    adapter, _runtime = _adapter(
                        configs,
                        {
                            configs[0].path: _canonical_binding(canonical),
                            configs[1].path: _binding(legacy),
                        },
                    )

                    with self.assertRaisesRegex(
                        error_type,
                        "programmer misuse",
                    ):
                        _append(adapter)

                    self.assertEqual(call_log, [resource_id])
                    self.assertEqual(legacy.appended, [])

    def test_canonical_sqlite_runtime_errors_remain_resource_local(self) -> None:
        for error in (
            sqlite3.OperationalError("database is locked"),
            sqlite3.DatabaseError("database disk image is malformed"),
        ):
            with self.subTest(error_type=type(error).__name__):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    resource_id = f"canonical.{type(error).__name__}"
                    configs = (
                        _config(root, resource_id),
                        _config(root, "legacy.healthy-after-sqlite"),
                    )
                    for config in configs:
                        config.path.write_bytes(b"seed\n")
                    call_log: list[str] = []
                    canonical = _RecordingStore(
                        resource_id,
                        call_log=call_log,
                        backing_path=configs[0].path,
                        error=error,
                    )
                    legacy = _RecordingLegacyBackend(
                        "legacy.healthy-after-sqlite",
                        call_log=call_log,
                        backing_path=configs[1].path,
                    )
                    adapter, _runtime = _adapter(
                        configs,
                        {
                            configs[0].path: _canonical_binding(canonical),
                            configs[1].path: _binding(legacy),
                        },
                    )

                    report = _append(adapter)

                    self.assertEqual(
                        report.errors,
                        ("TM.WRITE.CANONICAL_APPEND_FAILED",),
                    )
                    self.assertEqual(
                        report.written_resource_ids,
                        ("legacy.healthy-after-sqlite",),
                    )
                    self.assertEqual(
                        call_log,
                        [resource_id, "legacy.healthy-after-sqlite"],
                    )

    def test_only_typed_formal_legacy_failure_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            typed_config = _config(root, "legacy.typed")
            typed_config.path.write_bytes(b"seed\n")
            typed_error = LegacyAppendOperationError()
            self.assertIs(type(typed_error), LegacyAppendOperationError)
            self.assertEqual(
                LegacyAppendOperationError.__slots__,
                ("error_code", "retryable"),
            )
            self.assertEqual(typed_error.args, ())
            self.assertEqual(str(typed_error), "")
            typed_backend = _RecordingLegacyBackend(
                "legacy.typed",
                call_log=[],
                backing_path=typed_config.path,
                error=typed_error,
            )
            typed_adapter, _runtime = _adapter(
                (typed_config,),
                {typed_config.path: _binding(typed_backend)},
            )

            report = _append(typed_adapter)

            self.assertEqual(
                report.errors,
                ("TM.WRITE.LEGACY_APPEND_FAILED",),
            )
            self.assertTrue(report.outcomes[0].retryable)

            tampered_config = _config(root, "legacy.tampered-formal")
            tampered_config.path.write_bytes(b"seed\n")
            tampered_error = LegacyAppendOperationError()
            tampered_error.error_code = "TM.WRITE.FORGED"
            tampered_backend = _RecordingLegacyBackend(
                "legacy.tampered-formal",
                call_log=[],
                backing_path=tampered_config.path,
                error=tampered_error,
            )
            tampered_adapter, _runtime = _adapter(
                (tampered_config,),
                {tampered_config.path: _binding(tampered_backend)},
            )
            with self.assertRaisesRegex(
                ValueError,
                "formal legacy append failure contract drift",
            ):
                _append(tampered_adapter)

            programmer_config = _config(root, "legacy.same-message-programmer")
            programmer_config.path.write_bytes(b"seed\n")
            programmer_backend = _RecordingLegacyBackend(
                "legacy.same-message-programmer",
                call_log=[],
                backing_path=programmer_config.path,
                error=RuntimeError("legacy TM append failed"),
            )
            programmer_adapter, _runtime = _adapter(
                (programmer_config,),
                {programmer_config.path: _binding(programmer_backend)},
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "legacy TM append failed",
            ):
                _append(programmer_adapter)

    def test_default_legacy_owner_converts_false_to_typed_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "legacy.default-owner")
            config.path.write_bytes(b'{"source":"old","target":"old"}\n')
            runtime = TMRuntimeHost(
                resolver=TMResourceResolver(),
                configs=(config,),
            )
            adapter = EditorTMAdapter(
                runtime_host=runtime,
                capability_host=CapabilityHost(
                    evaluated_at_utc=_EVALUATED_AT
                ),
            )

            with patch.object(TMEngine, "save_record", return_value=False):
                report = _append(adapter)

            self.assertEqual(report.written_resource_ids, ())
            self.assertEqual(
                report.errors,
                ("TM.WRITE.LEGACY_APPEND_FAILED",),
            )
            self.assertEqual(len(report.outcomes), 1)
            self.assertFalse(report.outcomes[0].written)
            self.assertTrue(report.outcomes[0].retryable)


if __name__ == "__main__":
    unittest.main()
