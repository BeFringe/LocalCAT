"""Task 4.3 mixed aggregation and safe UI projection tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

from capability_host import CapabilityHost, CapabilityHostComposition
from editor_contracts import (
    EditorSegment,
    ResourceConfig,
    ResourceKind,
    RetrievalDisplayState,
    SuggestionQueryIdentity,
    TMPreferences,
    TMResourceDisplayMode,
    TMSuggestionReport,
)
from editor_tm_adapter import EditorTMAdapter
from tm_application_composition import (
    LegacyOpenBinding,
    LegacyPortBackend,
    TMResourceResolver,
    TMRuntimeHost,
)
from tm_contracts import (
    CANDIDATE_BUDGET_VERSION,
    CandidateRecallMetadata,
    QueryReport,
    ResourceQueryFailure,
    ResourceQueryMetadata,
    TMMatchType,
    TMRecordDraft,
    candidate_budget_v1,
)
from tm_engine import TMMatch
from tests.test_capability_host_gate_d import (
    _FakeGateDExecution,
    _composition as _gate_d_composition,
    _gate_c,
    _gate_d_owner,
)
from tests.test_editor_tm_adapter_canonical import _activate


_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _config(
    *,
    resource_id: str,
    name: str,
    path: Path,
    active: bool = True,
    lookup: bool = True,
) -> ResourceConfig:
    return ResourceConfig(
        id=resource_id,
        name=name,
        kind=ResourceKind.TRANSLATION_MEMORY,
        path=path,
        active=active,
        lookup=lookup,
        update=False,
    )


def _write_legacy(root: Path, resource_id: str, rows: tuple[str, ...]) -> Path:
    source = (root / f"{resource_id}.jsonl").resolve()
    source.write_text("".join(f"{row}\n" for row in rows), encoding="utf-8")
    return source


def _open_capability(test_case: unittest.TestCase) -> CapabilityHostComposition:
    execution = _FakeGateDExecution()
    composition = _gate_d_composition(test_case, execution)
    _ = _gate_c(composition)
    owner = _gate_d_owner(composition)
    _ = owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
    status = owner.wait(timeout=10.0)
    test_case.assertEqual(status.state.value, "SUCCEEDED")
    return composition


def _adapter(
    configs: tuple[ResourceConfig, ...],
    *,
    capability_host: CapabilityHost | None = None,
) -> tuple[EditorTMAdapter, TMRuntimeHost]:
    runtime = TMRuntimeHost(
        resolver=TMResourceResolver(),
        configs=configs,
    )
    return (
        EditorTMAdapter(
            runtime_host=runtime,
            capability_host=(
                capability_host
                if capability_host is not None
                else CapabilityHost(evaluated_at_utc=_EVALUATED_AT)
            ),
        ),
        runtime,
    )


def _query(
    adapter: EditorTMAdapter,
    *,
    source: str = "Hello.",
    speaker: str = "",
    session: str = "project-session-1",
    epoch: int = 3,
    minimum_similarity: float = 0.60,
) -> TMSuggestionReport:
    return adapter.query_current(
        segment=EditorSegment(
            id="segment-1",
            source=source,
            speaker=speaker,
        ),
        project_session_id=session,
        query_epoch=epoch,
        preferences=TMPreferences(minimum_similarity=minimum_similarity),
    )


def _closed_recall(resource_id: str, code: str) -> CandidateRecallMetadata:
    return CandidateRecallMetadata(
        resource_id=resource_id,
        index_kind="FTS5_TRIGRAM",
        fuzzy_available=False,
        fuzzy_unavailable_code=code,
        stages=(),
        union_unique_count=0,
        deduplicated_count=0,
        result_limit=10,
        candidate_budget_version=CANDIDATE_BUDGET_VERSION,
        candidate_budget=candidate_budget_v1(10),
        truncated=False,
    )


class EditorTMAdapterMixedTests(unittest.TestCase):
    def test_public_query_uses_one_operation_and_same_batch_for_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _write_legacy(
                root,
                "legacy",
                ('{"source":"Hello.","target":"你好。"}',),
            )
            adapter, _runtime = _adapter(
                (_config(
                    resource_id="tm.legacy",
                    name="Legacy",
                    path=source,
                ),)
            )
            canonical_batches: list[object] = []
            issued_batches: list[object] = []
            operation_calls: list[object] = []
            original_operation = CapabilityHost.query_retrieval_operation
            original_canonical = EditorTMAdapter._query_canonical
            original_legacy = EditorTMAdapter._query_legacy_exact

            def operation_spy(
                host: CapabilityHost,
                resources: object,
                query: object,
            ) -> object:
                operation_calls.append(query)
                return original_operation(
                    host,
                    cast(Any, resources),
                    cast(Any, query),
                )

            def canonical_spy(
                current: EditorTMAdapter,
                **kwargs: object,
            ) -> object:
                batch = original_canonical(current, **cast(Any, kwargs))
                issued_batches.append(batch)
                return batch

            def legacy_spy(
                current: EditorTMAdapter,
                *,
                canonical_batch: object,
            ) -> object:
                canonical_batches.append(canonical_batch)
                return original_legacy(
                    current,
                    canonical_batch=cast(Any, canonical_batch),
                )

            with patch.object(
                CapabilityHost,
                "query_retrieval_operation",
                new=operation_spy,
            ), patch.object(
                EditorTMAdapter,
                "_query_canonical",
                new=canonical_spy,
            ), patch.object(
                EditorTMAdapter,
                "_query_legacy_exact",
                new=legacy_spy,
            ):
                report = _query(adapter)

            self.assertEqual(len(operation_calls), 1)
            self.assertEqual(len(issued_batches), 1)
            self.assertEqual(len(canonical_batches), 1)
            self.assertIs(issued_batches[0], canonical_batches[0])
            self.assertEqual(len(report.suggestions), 1)
            self.assertIs(
                report.suggestions[0].query_identity,
                report.query_identity,
            )
            self.assertEqual(report.suggestions[0].target, "你好。")

    def test_legacy_identity_is_body_bound_and_query_identity_hashes_raw_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _write_legacy(
                root,
                "legacy",
                ('{"source":"Straße","target":"街道"}',),
            )
            adapter, _runtime = _adapter(
                (_config(
                    resource_id="tm.legacy",
                    name="Legacy",
                    path=source,
                ),)
            )

            first = _query(
                adapter,
                source="Straße",
                session="session-A",
                epoch=1,
            )
            second = _query(
                adapter,
                source="Straße",
                session="session-B",
                epoch=99,
            )

            first_suggestion = first.suggestions[0]
            second_suggestion = second.suggestions[0]
            self.assertEqual(
                first.query_identity.source_digest,
                hashlib.sha256("Straße".encode("utf-8")).hexdigest(),
            )
            self.assertRegex(
                first_suggestion.record_id,
                r"\Alegacy:[0-9a-f]{64}\Z",
            )
            self.assertEqual(
                first_suggestion.record_id,
                second_suggestion.record_id,
            )
            self.assertNotEqual(first.query_identity, second.query_identity)
            with self.assertRaises(FrozenInstanceError):
                cast(Any, first).suggestions = ()

    def test_mixed_exact_first_preserves_core_lanes_scores_ties_and_dual_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_first = _write_legacy(
                root,
                "legacy-first",
                ('{"source":"aabba","target":"duplicate"}',),
            )
            canonical_first = _activate(
                root,
                resource_id="canonical-first",
                rows=(
                    '{"source":"aabba","target":"context",'
                    '"speaker":"Narrator"}',
                    '{"source":"aabba","target":"duplicate",'
                    '"speaker":"Other"}',
                    '{"source":"bbbaa","target":"fuzzy-low"}',
                    '{"source":"AABBA","target":"fuzzy-tie-old"}',
                    '{"source":"AAbbA","target":"fuzzy-tie-new"}',
                ),
            )
            legacy_middle = _write_legacy(
                root,
                "legacy-middle",
                ('{"source":"aabba","target":"legacy-middle"}',),
            )
            canonical_last = _activate(
                root,
                resource_id="canonical-last",
                rows=(
                    '{"source":"aabba","target":"canonical-last-exact"}',
                    '{"source":"AABBA","target":"canonical-last-fuzzy"}',
                ),
            )
            capability = _open_capability(self)
            adapter, _runtime = _adapter(
                (
                    _config(
                        resource_id="legacy-first",
                        name="Legacy first",
                        path=legacy_first,
                    ),
                    _config(
                        resource_id="canonical-first",
                        name="Canonical first",
                        path=canonical_first,
                    ),
                    _config(
                        resource_id="legacy-middle",
                        name="Legacy middle",
                        path=legacy_middle,
                    ),
                    _config(
                        resource_id="canonical-last",
                        name="Canonical last",
                        path=canonical_last,
                    ),
                ),
                capability_host=capability.host,
            )

            first = _query(
                adapter,
                source="aabba",
                speaker="Narrator",
            )
            repeated = _query(
                adapter,
                source="aabba",
                speaker="Narrator",
            )

            observed = tuple(
                (
                    suggestion.resource_id,
                    suggestion.target,
                    suggestion.match_type,
                    suggestion.final_similarity,
                    suggestion.matched_source,
                )
                for suggestion in first.suggestions
            )
            self.assertEqual(first.suggestions, repeated.suggestions)
            self.assertEqual(
                tuple(item[:3] for item in observed[:4]),
                (
                    ("legacy-first", "duplicate", TMMatchType.EXACT),
                    ("canonical-first", "duplicate", TMMatchType.EXACT),
                    ("legacy-middle", "legacy-middle", TMMatchType.EXACT),
                    (
                        "canonical-last",
                        "canonical-last-exact",
                        TMMatchType.EXACT,
                    ),
                ),
            )
            self.assertEqual(observed[4][1:3], ("context", TMMatchType.CONTEXT))
            fuzzy = tuple(
                item for item in observed if item[2] is TMMatchType.FUZZY
            )
            self.assertEqual(
                tuple(item[1] for item in fuzzy[:3]),
                (
                    "fuzzy-tie-new",
                    "fuzzy-tie-old",
                    "canonical-last-fuzzy",
                ),
            )
            self.assertTrue(
                all(
                    left[3] >= right[3]
                    for left, right in zip(fuzzy, fuzzy[1:])
                )
            )
            self.assertTrue(all(item[4] != "aabba" for item in fuzzy))
            self.assertEqual(
                len(
                    {
                        (item.resource_id, item.record_id)
                        for item in first.suggestions
                    }
                ),
                len(first.suggestions),
            )
            self.assertEqual(
                tuple(
                    suggestion.record_id.split(":", 1)[0]
                    for suggestion in first.suggestions[:4]
                ),
                ("legacy", "canonical", "legacy", "canonical"),
            )
            self.assertTrue(first.retrieval_status.fuzzy_available)
            self.assertEqual(
                tuple(status.resource_id for status in first.resource_statuses),
                (
                    "legacy-first",
                    "canonical-first",
                    "legacy-middle",
                    "canonical-last",
                ),
            )

    def test_global_limit_is_applied_once_after_all_resources_are_aggregated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_configs = tuple(
                _config(
                    resource_id=f"legacy-{index:02d}",
                    name=f"Legacy {index:02d}",
                    path=_write_legacy(
                        root,
                        f"legacy-{index:02d}",
                        (
                            '{"source":"Hello.",'
                            f'"target":"target-{index:02d}"}}',
                        ),
                    ),
                )
                for index in range(11)
            )
            canonical = _activate(
                root,
                resource_id="canonical",
                rows=('{"source":"Hello.","target":"canonical"}',),
            )
            configs = (
                *legacy_configs[:5],
                _config(
                    resource_id="canonical",
                    name="Canonical",
                    path=canonical,
                ),
                *legacy_configs[5:],
            )
            adapter, _runtime = _adapter(configs)

            report = _query(adapter)

            self.assertEqual(len(report.suggestions), 10)
            self.assertEqual(
                tuple(item.resource_id for item in report.suggestions),
                (
                    "legacy-00",
                    "legacy-01",
                    "legacy-02",
                    "legacy-03",
                    "legacy-04",
                    "canonical",
                    "legacy-05",
                    "legacy-06",
                    "legacy-07",
                    "legacy-08",
                ),
            )
            self.assertEqual(len(report.resource_statuses), 12)

    def test_inactive_and_lookup_disabled_resources_are_not_queried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active = _write_legacy(
                root,
                "active",
                ('{"source":"Hello.","target":"active"}',),
            )
            inactive = _write_legacy(
                root,
                "inactive",
                ('{"source":"Hello.","target":"inactive"}',),
            )
            no_lookup = _write_legacy(
                root,
                "no-lookup",
                ('{"source":"Hello.","target":"no-lookup"}',),
            )
            adapter, _runtime = _adapter(
                (
                    _config(
                        resource_id="inactive",
                        name="Inactive",
                        path=inactive,
                        active=False,
                    ),
                    _config(
                        resource_id="active",
                        name="Active",
                        path=active,
                    ),
                    _config(
                        resource_id="no-lookup",
                        name="No lookup",
                        path=no_lookup,
                        lookup=False,
                    ),
                )
            )

            report = _query(adapter)

            self.assertEqual(
                tuple(item.resource_id for item in report.suggestions),
                ("active",),
            )
            self.assertEqual(
                tuple(status.resource_id for status in report.resource_statuses),
                ("inactive", "active", "no-lookup"),
            )

    def test_unavailable_resource_and_no_match_are_not_conflated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = (root / "missing.jsonl").resolve()
            healthy = _write_legacy(root, "healthy", ())
            adapter, _runtime = _adapter(
                (
                    _config(
                        resource_id="missing",
                        name="Missing",
                        path=missing,
                    ),
                    _config(
                        resource_id="healthy",
                        name="Healthy",
                        path=healthy,
                    ),
                )
            )

            report = _query(adapter)

            self.assertEqual(report.suggestions, ())
            missing_status, healthy_status = report.resource_statuses
            self.assertIs(missing_status.mode, TMResourceDisplayMode.UNAVAILABLE)
            self.assertTrue(missing_status.safe_codes)
            self.assertIs(
                healthy_status.mode,
                TMResourceDisplayMode.LEGACY_EXACT_ONLY,
            )
            self.assertEqual(healthy_status.safe_codes, ())

    def test_legacy_query_failure_projects_body_free_degraded_status(self) -> None:
        class FailingLegacyBackend:
            def query_exact(
                self,
                source: str,
                speaker_raw: str | None,
            ) -> TMMatch | None:
                del source, speaker_raw
                raise OSError("/secret/legacy/path: private body")

            def append(self, draft: TMRecordDraft) -> None:
                del draft

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _write_legacy(root, "legacy", ())
            backend = FailingLegacyBackend()
            resolver = TMResourceResolver(
                runtime_open=lambda _path: LegacyOpenBinding(
                    backend=cast(LegacyPortBackend, cast(object, backend))
                )
            )
            runtime = TMRuntimeHost(
                resolver=resolver,
                configs=(
                    _config(
                        resource_id="legacy",
                        name="Legacy",
                        path=source,
                    ),
                ),
            )
            adapter = EditorTMAdapter(
                runtime_host=runtime,
                capability_host=CapabilityHost(evaluated_at_utc=_EVALUATED_AT),
            )

            report = _query(adapter)

            self.assertEqual(report.suggestions, ())
            status = report.resource_statuses[0]
            self.assertIs(status.mode, TMResourceDisplayMode.DEGRADED)
            self.assertFalse(status.exact_available)
            self.assertEqual(
                status.safe_codes,
                ("DIRECT", "TM.LEGACY.QUERY_FAILED"),
            )
            self.assertTrue(status.retryable)
            self.assertNotIn("secret", repr(report))
            self.assertNotIn(str(source), repr(report))

    def test_core_local_failure_projects_only_safe_codes_and_keeps_other_results(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failed = _activate(
                root,
                resource_id="canonical-failed",
                rows=('{"source":"Hello.","target":"failed"}',),
            )
            healthy = _activate(
                root,
                resource_id="canonical-healthy",
                rows=('{"source":"Hello.","target":"healthy"}',),
            )
            adapter, _runtime = _adapter(
                (
                    _config(
                        resource_id="canonical-failed",
                        name="Failed",
                        path=failed,
                    ),
                    _config(
                        resource_id="canonical-healthy",
                        name="Healthy",
                        path=healthy,
                    ),
                )
            )
            original = cast(Any, adapter)._query_canonical(
                segment=EditorSegment(id="segment-1", source="Hello."),
                project_session_id="project-session-1",
                query_epoch=3,
                preferences=TMPreferences(),
            )
            healthy_results = tuple(
                result
                for result in original.report.results
                if result.resource_id == "canonical-healthy"
            )
            healthy_metadata = tuple(
                metadata
                for metadata in original.report.resource_metadata
                if metadata.resource_id == "canonical-healthy"
            )
            altered = replace(
                original,
                report=QueryReport(
                    results=healthy_results,
                    resource_failures=(
                        ResourceQueryFailure(
                            resource_id="canonical-failed",
                            stage="QUERY",
                            error_code="STORE.QUERY_VIEW_EXPIRED",
                            retryable=True,
                        ),
                    ),
                    resource_metadata=healthy_metadata,
                ),
            )

            with patch.object(
                EditorTMAdapter,
                "_query_canonical",
                return_value=altered,
            ):
                report = _query(adapter)

            self.assertEqual(
                tuple(item.resource_id for item in report.suggestions),
                ("canonical-healthy",),
            )
            failed_status = report.resource_statuses[0]
            self.assertIs(failed_status.mode, TMResourceDisplayMode.DEGRADED)
            self.assertFalse(failed_status.exact_available)
            self.assertEqual(
                failed_status.safe_codes,
                ("QUERY", "STORE.QUERY_VIEW_EXPIRED"),
            )
            self.assertTrue(failed_status.retryable)
            self.assertNotIn(str(failed), repr(report))

    def test_partial_proof_keeps_exact_context_and_closes_only_resource_fuzzy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _activate(
                root,
                resource_id="canonical",
                rows=(
                    '{"source":"aabba","target":"context",'
                    '"speaker":"Narrator"}',
                    '{"source":"aabba","target":"exact",'
                    '"speaker":"Other"}',
                    '{"source":"AABBA","target":"fuzzy"}',
                ),
            )
            capability = _open_capability(self)
            adapter, _runtime = _adapter(
                (_config(
                    resource_id="canonical",
                    name="Canonical",
                    path=source,
                ),),
                capability_host=capability.host,
            )
            segment = EditorSegment(
                id="segment-1",
                source="aabba",
                speaker="Narrator",
            )
            original = cast(Any, adapter)._query_canonical(
                segment=segment,
                project_session_id="project-session-1",
                query_epoch=3,
                preferences=TMPreferences(),
            )
            retained = tuple(
                result
                for result in original.report.results
                if result.match_type is not TMMatchType.FUZZY
            )
            code = "CANDIDATE.PROOF_BUDGET_EXHAUSTED"
            altered = replace(
                original,
                report=QueryReport(
                    results=retained,
                    resource_failures=(
                        ResourceQueryFailure(
                            resource_id="canonical",
                            stage="PROOF",
                            error_code=code,
                            retryable=False,
                        ),
                    ),
                    resource_metadata=(
                        ResourceQueryMetadata(
                            resource_id="canonical",
                            context_available=True,
                            context_unavailable_code=None,
                            recall=_closed_recall("canonical", code),
                            scored_count=0,
                            returned_count=len(retained),
                        ),
                    ),
                ),
            )

            with patch.object(
                EditorTMAdapter,
                "_query_canonical",
                return_value=altered,
            ):
                report = adapter.query_current(
                    segment=segment,
                    project_session_id="project-session-1",
                    query_epoch=3,
                    preferences=TMPreferences(),
                )

            self.assertEqual(
                tuple(item.match_type for item in report.suggestions),
                (TMMatchType.EXACT, TMMatchType.CONTEXT),
            )
            status = report.resource_statuses[0]
            self.assertIs(status.mode, TMResourceDisplayMode.DEGRADED)
            self.assertTrue(status.exact_available)
            self.assertTrue(status.context_available)
            self.assertFalse(status.fuzzy_available)
            self.assertEqual(status.safe_codes, ("PROOF", code))
            self.assertTrue(report.retrieval_status.fuzzy_available)

    def test_projection_deduplicates_record_identity_and_omits_core_bodies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _activate(
                root,
                resource_id="canonical",
                rows=('{"source":"Hello.","target":"target"}',),
            )
            adapter, _runtime = _adapter(
                (_config(
                    resource_id="canonical",
                    name="Canonical",
                    path=source,
                ),)
            )
            original = cast(Any, adapter)._query_canonical(
                segment=EditorSegment(id="segment-1", source="Hello."),
                project_session_id="project-session-1",
                query_epoch=3,
                preferences=TMPreferences(),
            )
            result = original.report.results[0]
            object.__setattr__(
                result,
                "provenance",
                (("private", "/secret/proof/path"),),
            )
            metadata = original.report.resource_metadata[0]
            duplicate_report = QueryReport(
                results=(result, result),
                resource_failures=(),
                resource_metadata=(replace(metadata, returned_count=2),),
            )
            altered = replace(original, report=duplicate_report)

            with patch.object(
                EditorTMAdapter,
                "_query_canonical",
                return_value=altered,
            ):
                report = _query(adapter)

            self.assertEqual(len(report.suggestions), 1)
            self.assertEqual(report.suggestions[0].record_id, "canonical:1")
            self.assertNotIn("secret", repr(report))
            self.assertNotIn("proof", repr(report).lower())
            self.assertNotIn(str(source), repr(report))

    def test_resource_metadata_cannot_exceed_captured_core_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _activate(
                root,
                resource_id="canonical",
                rows=(
                    '{"source":"aabba","target":"context",'
                    '"speaker":"Narrator"}',
                    '{"source":"AABBA","target":"fuzzy"}',
                ),
            )
            capability = _open_capability(self)
            adapter, _runtime = _adapter(
                (_config(
                    resource_id="canonical",
                    name="Canonical",
                    path=source,
                ),),
                capability_host=capability.host,
            )
            segment = EditorSegment(
                id="segment-1",
                source="aabba",
                speaker="Narrator",
            )
            original = cast(Any, adapter)._query_canonical(
                segment=segment,
                project_session_id="project-session-1",
                query_epoch=3,
                preferences=TMPreferences(),
            )
            tampered = replace(
                original,
                retrieval=replace(
                    original.retrieval,
                    display=RetrievalDisplayState(
                        context_available=False,
                        fuzzy_available=False,
                        safe_codes=("RETRIEVAL.TEST_CLOSED",),
                    ),
                ),
            )

            with patch.object(
                EditorTMAdapter,
                "_query_canonical",
                return_value=tampered,
            ), self.assertRaisesRegex(ValueError, "authority"):
                adapter.query_current(
                    segment=segment,
                    project_session_id="project-session-1",
                    query_epoch=3,
                    preferences=TMPreferences(),
                )

    def test_private_batch_cannot_drift_from_public_query_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _write_legacy(root, "legacy", ())
            adapter, _runtime = _adapter(
                (_config(
                    resource_id="legacy",
                    name="Legacy",
                    path=source,
                ),)
            )
            original = cast(Any, adapter)._query_canonical(
                segment=EditorSegment(id="segment-1", source="Hello."),
                project_session_id="project-session-1",
                query_epoch=3,
                preferences=TMPreferences(),
            )

            with patch.object(
                EditorTMAdapter,
                "_query_canonical",
                return_value=original,
            ), self.assertRaisesRegex(ValueError, "public query inputs"):
                _query(adapter, source="Different source")

    def test_projection_does_not_apply_a_second_threshold_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _activate(
                root,
                resource_id="canonical",
                rows=('{"source":"bbaab","target":"boundary"}',),
            )
            capability = _open_capability(self)
            adapter, _runtime = _adapter(
                (_config(
                    resource_id="canonical",
                    name="Canonical",
                    path=source,
                ),),
                capability_host=capability.host,
            )

            report = _query(
                adapter,
                source="aabba",
                minimum_similarity=0.60,
            )

            self.assertEqual(len(report.suggestions), 1)
            self.assertIs(report.suggestions[0].match_type, TMMatchType.FUZZY)
            self.assertEqual(report.suggestions[0].final_similarity, 0.60)


class TMSuggestionReportContractTests(unittest.TestCase):
    def test_report_requires_the_same_query_identity_object(self) -> None:
        identity = SuggestionQueryIdentity(
            project_session_id="session",
            segment_id="segment",
            source_digest=hashlib.sha256(b"Hello.").hexdigest(),
            query_epoch=1,
        )
        equal_but_foreign = replace(identity)
        with tempfile.TemporaryDirectory() as temporary:
            source = _write_legacy(
                Path(temporary),
                "legacy",
                ('{"source":"Hello.","target":"target"}',),
            )
            adapter, _runtime = _adapter(
                (_config(
                    resource_id="legacy",
                    name="Legacy",
                    path=source,
                ),)
            )
            honest = _query(adapter, session="session", epoch=1)
        suggestion = replace(
            honest.suggestions[0],
            query_identity=equal_but_foreign,
        )

        with self.assertRaisesRegex(ValueError, "share"):
            TMSuggestionReport(
                suggestions=(suggestion,),
                resource_statuses=honest.resource_statuses,
                retrieval_status=RetrievalDisplayState(
                    context_available=False,
                    fuzzy_available=False,
                    safe_codes=honest.retrieval_status.safe_codes,
                ),
                query_identity=identity,
            )


if __name__ == "__main__":
    unittest.main()
