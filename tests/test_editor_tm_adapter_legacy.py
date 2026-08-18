"""Task 4.2 query-time legacy exact compatibility adapter tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

from capability_host import CapabilityHost
from editor_contracts import (
    EditorSegment,
    ResourceConfig,
    ResourceKind,
    TMPreferences,
)
from editor_tm_adapter import EditorTMAdapter
from renpy_tm_compat import build_dialogue_alias
from tm_application_composition import (
    LegacyOpenBinding,
    LegacyPortBackend,
    RuntimeOpenBinding,
    TMResourceResolver,
    TMRuntimeHost,
)
from tm_contracts import TMMatchType, TMRecordDraft
from tm_engine import TMMatch


_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


class _LegacyBackend:
    def __init__(self, outcomes: dict[str, TMMatch | None | BaseException]) -> None:
        self.outcomes = outcomes
        self.queries: list[tuple[str, str | None]] = []

    def query_exact(
        self,
        source: str,
        speaker_raw: str | None,
    ) -> TMMatch | None:
        self.queries.append((source, speaker_raw))
        outcome = self.outcomes.get(source)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def append(self, draft: TMRecordDraft) -> None:
        del draft


def _match(
    source: str,
    target: str,
    *,
    match_type: str = "EXACT",
    similarity: float = 1.0,
) -> TMMatch:
    return TMMatch(
        source=source,
        target=target,
        similarity=similarity,
        match_type=match_type,
        tm_source="legacy.jsonl",
    )


def _config(
    root: Path,
    resource_id: str,
    *,
    active: bool = True,
    lookup: bool = True,
) -> ResourceConfig:
    return ResourceConfig(
        id=resource_id,
        name=f"Name {resource_id}",
        kind=ResourceKind.TRANSLATION_MEMORY,
        path=(root / f"{resource_id}.jsonl").resolve(),
        active=active,
        lookup=lookup,
        update=False,
    )


def _adapter(
    configs: tuple[ResourceConfig, ...],
    backends: dict[Path, _LegacyBackend] | None = None,
) -> tuple[EditorTMAdapter, TMRuntimeHost]:
    resolver = TMResourceResolver()
    if backends is not None:
        def open_runtime(path: Path) -> RuntimeOpenBinding:
            return LegacyOpenBinding(
                backend=cast(LegacyPortBackend, backends[path])
            )

        resolver = TMResourceResolver(runtime_open=open_runtime)
    runtime_host = TMRuntimeHost(resolver=resolver, configs=configs)
    return (
        EditorTMAdapter(
            runtime_host=runtime_host,
            capability_host=CapabilityHost(evaluated_at_utc=_EVALUATED_AT),
        ),
        runtime_host,
    )


def _canonical_batch(
    adapter: EditorTMAdapter,
    *,
    source: str,
    speaker: str = "",
) -> Any:
    return cast(Any, adapter)._query_canonical(
        segment=EditorSegment(
            id="segment-1",
            source=source,
            speaker=speaker,
        ),
        project_session_id="project-session-1",
        query_epoch=1,
        preferences=TMPreferences(),
    )


class EditorTMAdapterLegacyTests(unittest.TestCase):
    def test_direct_exact_has_priority_and_preserves_raw_speaker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "legacy.direct")
            alias = build_dialogue_alias("alice", "Hello.")
            assert alias is not None
            backend = _LegacyBackend(
                {
                    "Hello.": _match("Hello.", "Direct target"),
                    alias: _match(alias, 'alice "Alias target"'),
                }
            )
            adapter, _runtime_host = _adapter(
                (config,),
                {config.path: backend},
            )
            canonical = _canonical_batch(
                adapter,
                source="Hello.",
                speaker="alice",
            )

            batch = cast(Any, adapter)._query_legacy_exact(
                canonical_batch=canonical
            )

            self.assertIs(batch.canonical_batch, canonical)
            self.assertEqual(backend.queries, [("Hello.", "alice")])
            self.assertEqual(batch.failures, ())
            self.assertEqual(len(batch.results), 1)
            result = batch.results[0]
            self.assertEqual(result.resource_id, "legacy.direct")
            self.assertEqual(result.resource_name, "Name legacy.direct")
            self.assertEqual(result.global_order, 0)
            self.assertEqual(result.query_source, "Hello.")
            self.assertEqual(result.matched_source, "Hello.")
            self.assertEqual(result.target, "Direct target")
            self.assertEqual(result.record_source, "Hello.")
            self.assertEqual(result.record_target, "Direct target")
            self.assertIs(result.match_type, TMMatchType.EXACT)
            self.assertEqual(result.similarity, 1.0)

    def test_direct_miss_uses_only_strict_same_speaker_renpy_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "legacy.alias")
            alias = build_dialogue_alias("NVLHED", "Hello.")
            assert alias is not None
            backend = _LegacyBackend(
                {
                    "Hello.": None,
                    alias: _match(alias, 'NVLHED "\u4f60\u597d\u3002"'),
                }
            )
            adapter, _runtime_host = _adapter(
                (config,),
                {config.path: backend},
            )
            canonical = _canonical_batch(
                adapter,
                source="Hello.",
                speaker="NVLHED",
            )

            batch = cast(Any, adapter)._query_legacy_exact(
                canonical_batch=canonical
            )

            self.assertEqual(
                backend.queries,
                [("Hello.", "NVLHED"), (alias, "NVLHED")],
            )
            self.assertEqual(batch.failures, ())
            self.assertEqual(len(batch.results), 1)
            result = batch.results[0]
            self.assertEqual(result.query_source, "Hello.")
            self.assertEqual(result.matched_source, "Hello.")
            self.assertEqual(result.target, "\u4f60\u597d\u3002")
            self.assertEqual(result.record_source, alias)
            self.assertEqual(result.record_target, 'NVLHED "\u4f60\u597d\u3002"')
            self.assertIs(result.match_type, TMMatchType.EXACT)
            self.assertEqual(result.similarity, 1.0)

    def test_unsafe_alias_wrapper_or_speaker_is_refused_without_a_hit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mismatch = _config(root, "legacy.mismatch")
            invalid_speaker = _config(root, "legacy.invalid-speaker")
            alias = build_dialogue_alias("alice", "Hello.")
            assert alias is not None
            mismatch_backend = _LegacyBackend(
                {
                    "Hello.": None,
                    alias: _match(alias, 'bob "\u4f60\u597d\u3002"'),
                }
            )
            invalid_backend = _LegacyBackend({"Hello.": None})
            mismatch_adapter, _ = _adapter(
                (mismatch,),
                {mismatch.path: mismatch_backend},
            )
            invalid_adapter, _ = _adapter(
                (invalid_speaker,),
                {invalid_speaker.path: invalid_backend},
            )

            mismatch_batch = cast(Any, mismatch_adapter)._query_legacy_exact(
                canonical_batch=_canonical_batch(
                    mismatch_adapter,
                    source="Hello.",
                    speaker="alice",
                )
            )
            invalid_batch = cast(Any, invalid_adapter)._query_legacy_exact(
                canonical_batch=_canonical_batch(
                    invalid_adapter,
                    source="Hello.",
                    speaker="bad speaker",
                )
            )

            self.assertEqual(mismatch_batch.results, ())
            self.assertEqual(mismatch_batch.failures, ())
            self.assertEqual(
                mismatch_backend.queries,
                [("Hello.", "alice"), (alias, "alice")],
            )
            self.assertEqual(invalid_batch.results, ())
            self.assertEqual(invalid_batch.failures, ())
            self.assertEqual(
                invalid_backend.queries,
                [("Hello.", "bad speaker")],
            )

    def test_default_legacy_port_preserves_source_lww(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "legacy.lww")
            config.path.write_text(
                '{"source":"Hello.","target":"old"}\n'
                '{"source":"Hello.","target":"winner"}\n',
                encoding="utf-8",
            )
            adapter, _runtime_host = _adapter((config,))

            batch = cast(Any, adapter)._query_legacy_exact(
                canonical_batch=_canonical_batch(
                    adapter,
                    source="Hello.",
                    speaker="alice",
                )
            )

            self.assertEqual(len(batch.results), 1)
            self.assertEqual(batch.results[0].target, "winner")
            self.assertIs(batch.results[0].match_type, TMMatchType.EXACT)
            self.assertEqual(batch.results[0].similarity, 1.0)

    def test_local_read_failure_is_body_safe_and_keeps_same_canonical_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            healthy = _config(root, "legacy.healthy")
            failing_direct = _config(root, "legacy.failing-direct")
            failing_alias = _config(root, "legacy.failing-alias")
            inactive = _config(root, "legacy.inactive", active=False)
            no_lookup = _config(root, "legacy.no-lookup", lookup=False)
            alias = build_dialogue_alias("alice", "Hello.")
            assert alias is not None
            healthy_backend = _LegacyBackend(
                {"Hello.": _match("Hello.", "Healthy target")}
            )
            failing_direct_backend = _LegacyBackend(
                {"Hello.": OSError("/secret/path: private body")}
            )
            failing_alias_backend = _LegacyBackend(
                {
                    "Hello.": None,
                    alias: UnicodeError("private alias body"),
                }
            )
            inactive_backend = _LegacyBackend(
                {"Hello.": AssertionError("inactive resource was queried")}
            )
            no_lookup_backend = _LegacyBackend(
                {"Hello.": AssertionError("no-lookup resource was queried")}
            )
            adapter, _runtime_host = _adapter(
                (
                    failing_direct,
                    healthy,
                    failing_alias,
                    inactive,
                    no_lookup,
                ),
                {
                    healthy.path: healthy_backend,
                    failing_direct.path: failing_direct_backend,
                    failing_alias.path: failing_alias_backend,
                    inactive.path: inactive_backend,
                    no_lookup.path: no_lookup_backend,
                },
            )
            canonical = _canonical_batch(
                adapter,
                source="Hello.",
                speaker="alice",
            )

            with patch.object(
                TMRuntimeHost,
                "capture_operation_snapshot",
                side_effect=AssertionError("legacy query recaptured runtime"),
            ):
                batch = cast(Any, adapter)._query_legacy_exact(
                    canonical_batch=canonical
                )

            self.assertIs(batch.canonical_batch, canonical)
            self.assertEqual(canonical.report.results, ())
            self.assertEqual(canonical.report.resource_failures, ())
            self.assertEqual(canonical.report.resource_metadata, ())
            self.assertEqual(
                tuple(result.resource_id for result in batch.results),
                ("legacy.healthy",),
            )
            self.assertEqual(batch.results[0].target, "Healthy target")
            self.assertEqual(
                tuple(
                    (
                        failure.resource_id,
                        failure.global_order,
                        failure.stage,
                        failure.error_code,
                        failure.retryable,
                    )
                    for failure in batch.failures
                ),
                (
                    (
                        "legacy.failing-direct",
                        0,
                        "DIRECT",
                        "TM.LEGACY.QUERY_FAILED",
                        True,
                    ),
                    (
                        "legacy.failing-alias",
                        2,
                        "ALIAS",
                        "TM.LEGACY.QUERY_FAILED",
                        False,
                    ),
                ),
            )
            self.assertNotIn("secret", repr(batch.failures))
            self.assertNotIn("private", repr(batch.failures))
            self.assertNotIn(str(failing_direct.path), repr(batch.failures))
            self.assertEqual(inactive_backend.queries, [])
            self.assertEqual(no_lookup_backend.queries, [])

    def test_programmer_error_is_not_laundered_as_resource_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "legacy.programmer")
            backend = _LegacyBackend(
                {"Hello.": AssertionError("programmer error")}
            )
            adapter, _runtime_host = _adapter(
                (config,),
                {config.path: backend},
            )

            with self.assertRaisesRegex(AssertionError, "programmer error"):
                cast(Any, adapter)._query_legacy_exact(
                    canonical_batch=_canonical_batch(
                        adapter,
                        source="Hello.",
                        speaker="alice",
                    )
                )

    def test_non_exact_legacy_backend_result_is_rejected_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root, "legacy.not-exact")
            backend = _LegacyBackend(
                {
                    "Hello.": _match(
                        "Hello.",
                        "Unsafe",
                        match_type="FUZZY",
                        similarity=0.9,
                    )
                }
            )
            adapter, _runtime_host = _adapter(
                (config,),
                {config.path: backend},
            )

            with self.assertRaisesRegex(ValueError, "legacy exact"):
                cast(Any, adapter)._query_legacy_exact(
                    canonical_batch=_canonical_batch(
                        adapter,
                        source="Hello.",
                        speaker="alice",
                    )
                )


if __name__ == "__main__":
    unittest.main()
