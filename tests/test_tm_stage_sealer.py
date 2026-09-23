"""Adversarial unit tests for the Task 5.3 immutable stage sealer."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import dataclass, replace as dataclass_replace
from typing import Any, cast
from unittest.mock import patch

import tm_contracts as contract_module
import tm_migration
import tm_sqlite_store
import tm_stage_sealer
from platform_fs import compose_platform_file_backend
from tm_content_attestation import (
    PortableSealedContentAttestation,
    _portable_sealed_content_attestation_to_mapping,
)
from tm_contracts import (
    SNAPSHOT_MANIFEST_VERSION,
    ActivationCapabilityState,
    CanonicalResourceIdentity,
    GenerationExpectation,
    MutableStageRef,
    SealedStage,
    SnapshotKind,
    SnapshotBinding,
    SnapshotManifest,
    SnapshotReceipt,
    StageValidationEvidence,
    TMRecordDraft,
    contract_to_json,
    snapshot_receipt_digest,
    stage_validation_evidence_digest,
)
from tm_migration import TMMigrationService
from tm_sqlite_store import (
    ResourceStoreCoordinator,
    SQLiteStoreSchemaError,
    SQLiteTMStore,
)
from tm_stage_sealer import (
    _SealedArtifactRegistry as SealedArtifactRegistry,
    StageSealError,
    StageSealer,
)


SOURCE_BYTES = (
    b'{"source":"same","target":"first","speaker":"alice",'
    b'"context_prev":"before","file_source":"chapter.json"}\n'
    b"{bad-json}\n"
    b'{"source":"same","target":"second","context_next":"after"}\n'
    b'{"source":"x","target":"short"}\n'
)


def _identity(root: Path) -> CanonicalResourceIdentity:
    return CanonicalResourceIdentity.from_configured_jsonl(
        "tm.primary",
        (root / "tm.primary.jsonl").resolve(),
    )


def _service(identity: CanonicalResourceIdentity) -> TMMigrationService:
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
    )


def _sealer() -> StageSealer:
    return StageSealer(
        registry=SealedArtifactRegistry(
            registry_namespace="coordinator.primary"
        ),
        canonical_store_id="store.primary",
    )


def _build_stage(
    root: Path,
    *,
    fts5_available: bool,
) -> tuple[CanonicalResourceIdentity, MutableStageRef]:
    identity = _identity(root)
    identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
    service = _service(identity)
    with patch(
        "tm_sqlite_store._probe_fts5",
        return_value=fts5_available,
    ):
        build = service.build_mutable_stage(identity.configured_jsonl_path)
    stage = build.mutable_stage
    if stage is None:
        raise AssertionError("expected a fresh mutable stage")
    return identity, stage


def _seal(
    sealer: StageSealer,
    stage: MutableStageRef,
    *,
    fts5_available: bool,
    expected_prior_generation: int | None = None,
    caller_borrow: object | None = None,
) -> SealedStage:
    with patch(
        "tm_sqlite_store._probe_fts5",
        return_value=fts5_available,
    ):
        return sealer.seal(
            stage,
            expected_prior_generation=expected_prior_generation,
            caller_borrow=cast(Any, caller_borrow),
        )


def _portable_sealer(
    root: Path,
    stage: MutableStageRef,
    *,
    namespace: str,
) -> tuple[StageSealer, object, object]:
    backend = compose_platform_file_backend(root)
    reservation = (
        tm_migration._InitialActivationResourceReservation.acquire_with_backend(
            stage.resource_identity,
            backend,
        )
    )
    inputs = reservation.stage_seal_inputs()
    sealer = StageSealer(
        registry=SealedArtifactRegistry(registry_namespace=namespace),
        canonical_store_id="store.primary",
        platform=cast(Any, inputs["platform"]),
    )
    return sealer, reservation, inputs["caller_borrow"]


def _registry(sealer: StageSealer) -> SealedArtifactRegistry:
    return cast(SealedArtifactRegistry, sealer._registry)


def _raw_connection(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path))
    connection.execute("PRAGMA foreign_keys=OFF")
    return connection


def _expect_seal_code(
    test: unittest.TestCase,
    function: Callable[[], object],
    error_code: str,
) -> StageSealError:
    with test.assertRaisesRegex(StageSealError, f"^{error_code}$") as raised:
        function()
    return raised.exception


def _assert_clean_unpublished(
    test: unittest.TestCase,
    sealer: StageSealer,
    stage: MutableStageRef,
) -> None:
    registry = _registry(sealer)
    test.assertEqual(len(registry._entries), 0)
    test.assertEqual(len(registry._reservations), 0)
    connection = sqlite3.connect(str(stage.staged_db_path))
    try:
        status_rows = connection.execute(
            "SELECT value FROM tm_meta "
            "WHERE key = 'activation_status'"
        ).fetchall()
    finally:
        connection.close()
    test.assertEqual(status_rows, [("UNPUBLISHED",)])
    test.assertTrue(stage.staged_db_path.is_file())
    test.assertTrue(stage.manifest_temp_path.is_file())


class _PathSubclass(Path):
    pass


class _FakeRegistry:
    def __init__(self, namespace: Any) -> None:
        self._namespace = namespace

    @property
    def registry_namespace(self) -> Any:
        return self._namespace

    def reserve(
        self,
        mutable_stage: object,
        *,
        database_identity: object,
        manifest_identity: object,
    ) -> object:
        raise AssertionError("reserve must not be reached")

    def commit(
        self,
        reservation: object,
        evidence: object,
        generation: object,
    ) -> object:
        raise AssertionError("commit must not be reached")

    def release(self, reservation: object) -> None:
        raise AssertionError("release must not be reached")


class StageSealerHappyPathTests(unittest.TestCase):
    def test_source_provenance_uses_builder_compatibility_without_losing_pairs(self) -> None:
        cases = (
            ({}, [["source", "legacy-jsonl"]]),
            ({"provenance": None}, [["source", "legacy-jsonl"]]),
            ({"provenance": [["source", 1]]}, [["source", "legacy-jsonl"]]),
            ({"provenance": [["source", "tmx"], ["broken"]]}, [["source", "legacy-jsonl"]]),
            ({"provenance": []}, []),
            ({"provenance": [["source", "local-write"]]}, [["source", "local-write"]]),
            ({"provenance": [["z", "值"], ["a", ""], ["z", "值"]]}, [["z", "值"], ["a", ""], ["z", "值"]]),
        )
        for supplied, expected in cases:
            with self.subTest(supplied=supplied), tempfile.TemporaryDirectory() as temporary:
                identity = _identity(Path(temporary))
                identity.configured_jsonl_path.write_text(
                    json.dumps({"source": "same", "target": "value", **supplied}) + "\n",
                    encoding="utf-8",
                )
                build = _service(identity).build_mutable_stage(identity.configured_jsonl_path)
                assert build.mutable_stage is not None
                facts = tm_stage_sealer._validate_stage_facts(
                    build.mutable_stage, canonical_store_id="store.primary",
                )
                self.assertEqual(facts.record_count, 1)
                with sqlite3.connect(build.mutable_stage.staged_db_path) as connection:
                    observed = connection.execute("SELECT provenance_json FROM tm_record").fetchone()[0]
                connection.close()
                self.assertEqual(json.loads(observed), expected)

    def test_exported_source_provenance_tamper_is_rejected_exactly(self) -> None:
        original = [["z", "first"], ["a", "second"], ["z", "first"]]
        for tampered in ([], original[:2], list(reversed(original[:2])) + original[2:], [["source", "legacy-jsonl"]]):
            with self.subTest(tampered=tampered), tempfile.TemporaryDirectory() as temporary:
                identity = _identity(Path(temporary))
                identity.configured_jsonl_path.write_text(
                    json.dumps({"source": "same", "target": "value", "provenance": original}) + "\n",
                    encoding="utf-8",
                )
                build = _service(identity).build_mutable_stage(identity.configured_jsonl_path)
                assert build.mutable_stage is not None
                with sqlite3.connect(build.mutable_stage.staged_db_path) as connection:
                    connection.execute("UPDATE tm_record SET provenance_json = ?", (json.dumps(tampered, separators=(",", ":")),))
                connection.close()
                _expect_seal_code(
                    self,
                    lambda: tm_stage_sealer._validate_stage_facts(build.mutable_stage, canonical_store_id="store.primary"),
                    "SEALER.PROVENANCE_MISMATCH",
                )

    @unittest.skipUnless(os.name == "nt", "Windows platform route")
    def test_portable_missing_caller_borrow_fails_before_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, stage = _build_stage(root, fts5_available=True)
            backend = compose_platform_file_backend(root)
            registry = SealedArtifactRegistry(
                registry_namespace="coordinator.portable-no-borrow"
            )
            sealer = StageSealer(
                registry=registry,
                canonical_store_id="store.primary",
                platform=cast(Any, backend),
            )
            with patch.object(
                type(backend),
                "_acquire",
                side_effect=AssertionError("StageSealer must not acquire a lock"),
            ) as acquire:
                with self.assertRaisesRegex(
                    StageSealError,
                    "^SEALER.ATTESTATION_UNAVAILABLE$",
                ):
                    sealer.seal(stage)
            acquire.assert_not_called()
            self.assertEqual(registry._entries, {})
            self.assertEqual(registry._reservations, {})
            connection = sqlite3.connect(str(stage.staged_db_path))
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM tm_meta "
                        "WHERE key = 'activation_status'"
                    ).fetchall(),
                    [("UNPUBLISHED",)],
                )
            finally:
                connection.close()

    @unittest.skipUnless(os.name == "nt", "Windows platform route")
    def test_portable_post_marker_failure_is_unregistered_fail_stop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, stage = _build_stage(root, fts5_available=True)
            sealer, reservation, caller_borrow = _portable_sealer(
                root,
                stage,
                namespace="coordinator.portable-failure",
            )
            self.addCleanup(reservation.release)  # type: ignore[attr-defined]
            with (
                patch(
                    "tm_stage_sealer._open_portable_stage_live_authority",
                    side_effect=StageSealError(
                        "SEALER.ATTESTATION_UNAVAILABLE"
                    ),
                ),
                patch(
                    "tm_stage_sealer._restore_stage_unpublished"
                ) as restore,
                patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=True,
                ),
            ):
                with self.assertRaisesRegex(
                    StageSealError,
                    "^SEALER.ATTESTATION_UNAVAILABLE$",
                ):
                    sealer.seal(
                        stage,
                        caller_borrow=cast(Any, caller_borrow),
                    )
            restore.assert_not_called()
            registry = _registry(sealer)
            self.assertEqual(registry._entries, {})
            self.assertEqual(registry._reservations, {})
            connection = sqlite3.connect(str(stage.staged_db_path))
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM tm_meta "
                        "WHERE key = 'activation_status'"
                    ).fetchall(),
                    [("SEALED",)],
                )
            finally:
                connection.close()
            reservation.release()  # type: ignore[attr-defined]

    @unittest.skipUnless(os.name == "nt", "Windows platform route")
    def test_portable_source_hardlink_is_rejected_without_authority_residue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, stage = _build_stage(root, fts5_available=True)
            alias = (root / "source-hardlink.jsonl").resolve()
            os.link(identity.configured_jsonl_path, alias)
            sealer, reservation, caller_borrow = _portable_sealer(
                root,
                stage,
                namespace="coordinator.portable-hardlink",
            )
            self.addCleanup(reservation.release)  # type: ignore[attr-defined]
            registry = _registry(sealer)
            with (
                patch(
                    "tm_stage_sealer._restore_stage_unpublished"
                ) as restore,
                patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=True,
                ),
            ):
                with self.assertRaisesRegex(
                    StageSealError,
                    "^SEALER.ATTESTATION_INVALID$",
                ):
                    sealer.seal(
                        stage,
                        caller_borrow=cast(Any, caller_borrow),
                    )
            restore.assert_not_called()
            self.assertEqual(registry._entries, {})
            self.assertEqual(registry._reservations, {})
            self.assertEqual(registry._portable_borrows, {})
            connection = sqlite3.connect(str(stage.staged_db_path))
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM tm_meta "
                        "WHERE key = 'activation_status'"
                    ).fetchall(),
                    [("SEALED",)],
                )
            finally:
                connection.close()
            reservation.release()  # type: ignore[attr-defined]

    @unittest.skipUnless(os.name == "nt", "Windows platform route")
    def test_portable_commit_fault_after_take_closes_and_rolls_back_registry(
        self,
    ) -> None:
        class _FailingSetDict(dict[tuple[str, str], str]):
            def __setitem__(self, key: tuple[str, str], value: str) -> None:
                del key, value
                raise RuntimeError("registry placement fault")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, stage = _build_stage(root, fts5_available=True)
            sealer, reservation, caller_borrow = _portable_sealer(
                root,
                stage,
                namespace="coordinator.portable-commit-fault",
            )
            self.addCleanup(reservation.release)  # type: ignore[attr-defined]
            registry = _registry(sealer)
            registry._sealed_paths = _FailingSetDict()
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=True,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "^registry placement fault$",
                ):
                    sealer.seal(
                        stage,
                        caller_borrow=cast(Any, caller_borrow),
                    )
            self.assertEqual(registry._entries, {})
            self.assertEqual(registry._reservations, {})
            self.assertEqual(registry._sealed_paths, {})
            self.assertEqual(registry._used_verified_commit_nonces, set())
            reservation.release()  # type: ignore[attr-defined]

    @unittest.skipUnless(os.name == "nt", "Windows platform route")
    def test_portable_platform_route_retains_authority_until_consume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, stage = _build_stage(root, fts5_available=True)
            sealer, reservation, caller_borrow = _portable_sealer(
                root,
                stage,
                namespace="coordinator.portable",
            )
            self.addCleanup(reservation.release)  # type: ignore[attr-defined]
            sealed = _seal(
                sealer,
                stage,
                fts5_available=True,
                caller_borrow=caller_borrow,
            )
            registry = _registry(sealer)
            entry = registry._entries[sealed.artifact.artifact_id]
            self.assertIs(type(entry), tm_stage_sealer._PortableRegistryEntry)
            portable = cast(
                tm_stage_sealer._PortableRegistryEntry,
                entry,
            )
            connection = sqlite3.connect(
                f"{stage.staged_db_path.as_uri()}?mode=ro",
                isolation_level=None,
                uri=True,
            )
            try:
                expected_projection_digest = (
                    tm_sqlite_store._candidate_proof_projection_digest(
                        connection,
                        fts5_available=True,
                    )
                )
            finally:
                connection.close()
            self.assertEqual(
                portable.candidate_projection_digest,
                expected_projection_digest,
            )
            readiness = registry.resolve_physical_readiness(sealed)
            try:
                self.assertIs(
                    type(readiness),
                    tm_stage_sealer._PortablePhysicalReadinessSnapshot,
                )
                self.assertEqual(
                    readiness.candidate_projection_digest,
                    expected_projection_digest,
                )
            finally:
                readiness.live_reproof._release()
            self.assertIs(
                type(portable.sealed_content_attestation),
                PortableSealedContentAttestation,
            )
            mapping = _portable_sealed_content_attestation_to_mapping(
                portable.sealed_content_attestation
            )
            for name in ("database", "manifest", "source"):
                self.assertEqual(set(cast(dict[str, object], mapping[name])), {
                    "sha256",
                    "size",
                })
            live_authority = portable.live_authority
            self.assertIsNotNone(live_authority)
            assert live_authority is not None
            self.assertFalse(live_authority.closed)
            token = registry.issue_token(
                sealed,
                current_generation=None,
            )
            registry.consume(token)
            self.assertTrue(live_authority.closed)
            self.assertIs(
                registry.state(sealed),
                ActivationCapabilityState.CONSUMED,
            )
            self.assertTrue(registry.contains(sealed))
            reservation.release()  # type: ignore[attr-defined]

    @unittest.skipUnless(os.name == "nt", "Windows platform route")
    def test_portable_close_failure_keeps_terminal_registry_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, stage = _build_stage(root, fts5_available=True)
            sealer, reservation, caller_borrow = _portable_sealer(
                root,
                stage,
                namespace="coordinator.portable-close",
            )
            self.addCleanup(reservation.release)  # type: ignore[attr-defined]
            sealed = _seal(
                sealer,
                stage,
                fts5_available=True,
                caller_borrow=caller_borrow,
            )
            registry = _registry(sealer)
            entry = cast(
                tm_stage_sealer._PortableRegistryEntry,
                registry._entries[sealed.artifact.artifact_id],
            )
            live_authority = entry.live_authority
            assert live_authority is not None
            token = registry.issue_token(sealed, current_generation=None)
            real_close = type(live_authority)._close_authority

            def close_then_fail(
                authority: tm_stage_sealer._PortableStageLiveAuthority,
            ) -> None:
                real_close(authority)
                raise RuntimeError("close fault")

            with patch.object(
                type(live_authority),
                "_close_authority",
                close_then_fail,
            ):
                with self.assertRaisesRegex(RuntimeError, "^close fault$"):
                    registry.cancel(token)
            self.assertTrue(live_authority.closed)
            self.assertIs(
                registry.state(sealed),
                ActivationCapabilityState.CANCELLED,
            )
            terminal = cast(
                tm_stage_sealer._PortableRegistryEntry,
                registry._entries[sealed.artifact.artifact_id],
            )
            self.assertIsNone(terminal.live_authority)
            with self.assertRaisesRegex(
                StageSealError,
                "^SEALER.TOKEN_NOT_ACTIVE$",
            ):
                registry.cancel(token)
            reservation.release()  # type: ignore[attr-defined]

    def test_projection_digest_uses_bounded_multichunk_readback(
        self,
    ) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    _, stage = _build_stage(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                    with patch.object(
                        tm_sqlite_store,
                        "_CANDIDATE_PROJECTION_DIGEST_GRAM_CHUNK_ROWS",
                        2,
                    ):
                        sealed = _seal(
                            _sealer(),
                            stage,
                            fts5_available=fts5_available,
                        )
                    self.assertEqual(sealed.evidence.record_count, 3)

    def test_logical_closure_v2_bytes_survive_fetch_batching(self) -> None:
        expected_by_fts = {
            False: (
                "5b9795742b1313efc6382d1975c78180"
                "493d1a827f81d7f07352ae25bdb94d8c"
            ),
            True: (
                "806bcf945fd5551c1e1a145434a430ac"
                "312b9cb49d8b12465045904f8eeb7f1f"
            ),
        }
        for fts5_available, expected in expected_by_fts.items():
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    _, stage = _build_stage(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        connection.execute(
                            "UPDATE tm_meta SET value = ? "
                            "WHERE key = 'target_identity'",
                            ("0" * 64,),
                        )
                        # Runtime metadata is part of this golden's input.
                        # Hash the fixed fixture directly; do not seal it.
                        connection.execute(
                            "UPDATE tm_meta SET value = ? "
                            "WHERE key = 'sqlite_runtime_version'",
                            ("3.50.4",),
                        )
                        connection.execute(
                            "UPDATE tm_origin_batch SET source_path = ?, "
                            "created_at = ?",
                            (
                                "C:/fixed/source.jsonl",
                                "2000-01-01T00:00:00Z",
                            ),
                        )
                        connection.execute(
                            "UPDATE tm_snapshot_receipt SET "
                            "destination_jsonl_path = ?, "
                            "destination_manifest_path = ?, created_at = ?",
                            (
                                "C:/fixed/source.jsonl",
                                "C:/fixed/manifest.json",
                                "2000-01-01T00:00:00Z",
                            ),
                        )
                        with patch.object(
                            tm_stage_sealer,
                            "_STAGE_CLOSURE_FETCH_ROWS",
                            1,
                        ):
                            one_row_batches = (
                                tm_stage_sealer._stage_closure_digests(
                                    connection,
                                    reconstruct_pre_activation=True,
                                )
                            )
                        default_batches = tm_stage_sealer._stage_closure_digests(
                            connection,
                            reconstruct_pre_activation=True,
                        )
                    finally:
                        connection.close()
                    self.assertEqual(
                        one_row_batches,
                        (expected, expected),
                    )
                    self.assertEqual(default_batches, one_row_batches)

    def test_unchanged_tm_record_cells_are_framed_once_for_both_closures(
        self,
    ) -> None:
        record = (
            910001,
            "closure-source-only",
            "closure-target-only",
            "closure-fold-only",
            910004,
            "closure-speaker-only",
            "closure-context-prev-only",
            "closure-context-next-only",
            "closure-file-source-only",
            "closure-provenance-only",
            910010,
            910011,
            "closure-last-used-only",
            "closure-origin-batch-only",
            910014,
        )

        class Cursor:
            def __init__(self, rows: list[tuple[object, ...]]) -> None:
                self._rows = rows

            def fetchmany(self, _size: int) -> list[tuple[object, ...]]:
                rows, self._rows = self._rows, []
                return rows

            def fetchone(self) -> tuple[object, ...] | None:
                return self._rows.pop(0) if self._rows else None

        class Connection:
            def execute(
                self,
                query: str,
                _parameters: object = (),
            ) -> Cursor:
                if "FROM tm_record ORDER BY record_id" in query:
                    return Cursor([record])
                return Cursor([])

        real_frame = tm_stage_sealer._stage_closure_row_frame
        record_frame_count = 0

        def count_frame(row: tuple[object, ...]) -> bytes:
            nonlocal record_frame_count
            if row == record:
                record_frame_count += 1
            return real_frame(row)

        with patch.object(
            tm_stage_sealer,
            "_stage_closure_row_frame",
            side_effect=count_frame,
        ):
            active, pre_activation = tm_stage_sealer._stage_closure_digests(
                cast(Any, Connection()),
                reconstruct_pre_activation=True,
            )

        self.assertEqual(active, pre_activation)
        self.assertEqual(record_frame_count, 1)

    def test_stage_closure_row_frame_is_exact_and_rejects_non_cells(self) -> None:
        row = (None, "", "é", -12, 0)
        self.assertEqual(
            tm_stage_sealer._stage_closure_row_frame(row),
            b"n;s0:;s2:\xc3\xa9;i3:-12;i1:0;\n",
        )
        self.assertEqual(
            tm_stage_sealer._stage_closure_row_frame(row),
            b"".join(
                tm_stage_sealer._stage_closure_cell_frame(cell)
                for cell in row
            )
            + b"\n",
        )

        class Text(str):
            pass

        class Integer(int):
            pass

        for invalid in (False, 1.0, Text("text"), Integer(1)):
            with self.subTest(value=invalid), self.assertRaisesRegex(
                StageSealError,
                "^SEALER.STAGE_INVALID$",
            ):
                tm_stage_sealer._stage_closure_row_frame((invalid,))
        with self.assertRaises(UnicodeEncodeError):
            tm_stage_sealer._stage_closure_row_frame(("\ud800",))

    def test_logical_closure_v2_composes_authority_and_validated_projection(
        self,
    ) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _, stage = _build_stage(
                        root,
                        fts5_available=fts5_available,
                    )
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        baseline = tm_stage_sealer._stage_closure_digests(
                            connection,
                            reconstruct_pre_activation=True,
                        )
                        gram_row = connection.execute(
                            "SELECT gram_size, gram, record_id, "
                            "term_frequency FROM tm_gram LIMIT 1"
                        ).fetchone()
                        self.assertIsNotNone(gram_row)
                        assert gram_row is not None
                        connection.execute(
                            "UPDATE tm_gram SET term_frequency = ? WHERE "
                            "gram_size = ? AND gram = ? AND record_id = ?",
                            (
                                int(gram_row[3]) + 1,
                                int(gram_row[0]),
                                str(gram_row[1]),
                                int(gram_row[2]),
                            ),
                        )
                        same_count = tm_stage_sealer._stage_closure_digests(
                            connection,
                            reconstruct_pre_activation=True,
                        )
                        self.assertEqual(same_count, baseline)
                        with self.assertRaises(
                            tm_sqlite_store.CandidateProofIndexError
                        ):
                            tm_sqlite_store.validate_candidate_proof_index(
                                connection,
                                required_sizes=(
                                    (1, 2)
                                    if fts5_available
                                    else (1, 2, 3)
                                ),
                                fts5_available=fts5_available,
                            )
                        connection.rollback()
                        connection.execute(
                            "UPDATE tm_record SET target_raw = 'changed' "
                            "WHERE record_id = 1"
                        )
                        authority_changed = (
                            tm_stage_sealer._stage_closure_digests(
                                connection,
                                reconstruct_pre_activation=True,
                            )
                        )
                        self.assertNotEqual(authority_changed, baseline)
                    finally:
                        connection.close()

    def test_fresh_stage_has_one_full_seal_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            real_validate = tm_stage_sealer._validate_stage_facts
            real_open_write = tm_stage_sealer._open_stage_write_connection
            real_open_read = tm_stage_sealer._open_stage_read_connection
            traced_sql: list[str] = []

            def trace_connection(connection: sqlite3.Connection) -> sqlite3.Connection:
                connection.set_trace_callback(traced_sql.append)
                return connection

            with patch(
                "tm_stage_sealer._validate_stage_facts",
                wraps=real_validate,
            ) as validate, patch(
                "tm_stage_sealer._open_stage_write_connection",
                side_effect=lambda path: trace_connection(real_open_write(path)),
            ), patch(
                "tm_stage_sealer._open_stage_read_connection",
                side_effect=lambda path: trace_connection(real_open_read(path)),
            ):
                sealed = _seal(_sealer(), stage, fts5_available=True)

            self.assertEqual(validate.call_count, 1)
            self.assertTrue(validate.call_args.kwargs["seal_stage"])
            self.assertTrue(sealed.evidence.integrity_ok)
            self.assertEqual(
                sum(
                    sql.strip().casefold() == "pragma integrity_check"
                    for sql in traced_sql
                ),
                1,
            )

    def test_seal_completes_frozen_artifact_in_both_index_modes(
        self,
    ) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    identity, stage = _build_stage(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                    sealer = _sealer()
                    sealed = _seal(
                        sealer,
                        stage,
                        fts5_available=fts5_available,
                    )

                    self.assertIs(type(sealed), SealedStage)
                    self.assertIsNone(sealed.expected_prior_generation)
                    evidence = sealed.evidence
                    self.assertEqual(evidence.resource_id, "tm.primary")
                    self.assertEqual(evidence.record_count, 3)
                    self.assertEqual(evidence.origin_batch_count, 1)
                    self.assertTrue(evidence.integrity_ok)
                    self.assertTrue(evidence.foreign_keys_ok)
                    self.assertEqual(
                        evidence.source_binding.receipt.jsonl_digest,
                        hashlib.sha256(SOURCE_BYTES).hexdigest(),
                    )
                    self.assertEqual(
                        evidence.source_binding.configured_jsonl_path,
                        identity.configured_jsonl_path,
                    )
                    self.assertEqual(
                        evidence.source_binding.manifest_path,
                        identity.snapshot_manifest_path,
                    )
                    if fts5_available:
                        self.assertEqual(evidence.fts_count, 3)
                        self.assertEqual(
                            evidence.gram_counts,
                            ((1, 9), (2, 6)),
                        )
                    else:
                        self.assertEqual(evidence.fts_count, 0)
                        self.assertEqual(
                            evidence.gram_counts,
                            ((1, 9), (2, 6), (3, 4)),
                        )
                    self.assertEqual(
                        evidence.stage_file_digest,
                        hashlib.sha256(
                            stage.staged_db_path.read_bytes()
                        ).hexdigest(),
                    )
                    self.assertEqual(
                        evidence.manifest_temp_digest,
                        hashlib.sha256(
                            stage.manifest_temp_path.read_bytes()
                        ).hexdigest(),
                    )
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        SOURCE_BYTES,
                    )
                    self.assertFalse(identity.canonical_sidecar_path.exists())
                    self.assertFalse(
                        identity.snapshot_manifest_path.exists()
                    )

    def test_expected_prior_generation_any_non_negative_value_closes(
        self,
    ) -> None:
        for expected in (None, 0, 1, 2, 7):
            with self.subTest(expected_prior_generation=expected):
                with tempfile.TemporaryDirectory() as temporary:
                    _, stage = _build_stage(
                        Path(temporary),
                        fts5_available=True,
                    )
                    sealer = _sealer()
                    sealed = _seal(
                        sealer,
                        stage,
                        fts5_available=True,
                        expected_prior_generation=expected,
                    )
                    self.assertEqual(
                        sealed.expected_prior_generation,
                        expected,
                    )
                    self.assertEqual(
                        sealed.generation.expected_prior_generation,
                        expected,
                    )
                    self.assertEqual(
                        sealed.generation.resource_id,
                        sealed.evidence.resource_id,
                    )
                    self.assertEqual(
                        sealed.generation.target_identity,
                        sealed.evidence.target_identity,
                    )
                    self.assertEqual(
                        sealed.generation.canonical_store_id,
                        sealed.evidence.source_binding.receipt
                        .canonical_store_id,
                    )
                    self.assertEqual(
                        sealed.generation.snapshot_receipt_digest,
                        sealed.evidence.snapshot_receipt_digest,
                    )

    def test_evidence_and_seal_digests_are_deterministic_and_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            sealed = _seal(sealer, stage, fts5_available=True)
            evidence = sealed.evidence

            self.assertEqual(
                stage_validation_evidence_digest(evidence),
                stage_validation_evidence_digest(evidence),
            )
            contract_module._validate_sealed_stage(sealed)
            artifact = sealed.artifact
            self.assertEqual(
                artifact.seal_digest,
                contract_module._artifact_seal_digest(
                    registry_namespace=artifact.registry_namespace,
                    artifact_id=artifact.artifact_id,
                    mutable_stage=stage,
                    evidence=evidence,
                ),
            )
            self.assertEqual(
                sealed.sealed_stage_digest,
                contract_module._sealed_stage_contract_digest(
                    artifact=artifact,
                    evidence=evidence,
                    generation=sealed.generation,
                    activation_nonce=sealed.activation_nonce,
                ),
            )

    def test_registry_tracks_sealed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertEqual(
                sealer.registry.registry_namespace,
                "coordinator.primary",
            )
            self.assertEqual(sealer.canonical_store_id, "store.primary")
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(
                sealer.registry.state(sealed),
                ActivationCapabilityState.SEALED,
            )


class StageSealerWriteAuthorityTests(unittest.TestCase):
    def test_post_seal_store_authority_revoked_but_raw_file_writable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            with patch(
                "tm_sqlite_store._probe_fts5",
                return_value=True,
            ):
                store = SQLiteTMStore(
                    stage,
                    canonical_store_id="store.primary",
                )
            sealer = _sealer()
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))

            draft = TMRecordDraft(
                source_raw="z",
                target_raw="z",
                speaker_raw=None,
                context_prev_raw=None,
                context_next_raw=None,
                file_source=None,
                provenance=(("source", "test"),),
            )
            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "^STORE\\.STAGE_SEALED$",
            ):
                store.append(draft)
            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "^STORE\\.STAGE_SEALED$",
            ):
                store.exact_records("same")

            connection = sqlite3.connect(str(stage.staged_db_path))
            connection.execute("PRAGMA user_version=99")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(
                SQLiteStoreSchemaError,
                "^STORE\\.STAGE_PUBLISHED$",
            ):
                SQLiteTMStore(
                    stage,
                    canonical_store_id="store.primary",
                )


class StageSealerTamperTests(unittest.TestCase):
    def test_record_target_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_record SET target_raw='tampered' "
                "WHERE record_id=1"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.RECORD_MISMATCH",
            )

    def test_authority_mutation_before_first_content_capture_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            real_capture = tm_stage_sealer._capture_content_file
            mutated = False

            def mutate_then_capture(path: Path) -> Any:
                nonlocal mutated
                if not mutated and path == stage.staged_db_path:
                    mutated = True
                    connection = _raw_connection(stage.staged_db_path)
                    try:
                        connection.execute(
                            "UPDATE tm_record SET usage_count = usage_count + 1 "
                            "WHERE record_id = 1"
                        )
                        connection.commit()
                    finally:
                        connection.close()
                return real_capture(path)

            sealer = _sealer()
            self.assertFalse(hasattr(sealer.registry, "seal"))
            with patch(
                "tm_stage_sealer._capture_content_file",
                side_effect=mutate_then_capture,
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.STAGE_MUTATED_AFTER_VALIDATION",
                )

            self.assertTrue(mutated)
            self.assertEqual(len(_registry(sealer)._entries), 0)
            connection = sqlite3.connect(stage.staged_db_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM tm_meta "
                        "WHERE key = 'activation_status'"
                    ).fetchone(),
                    ("UNPUBLISHED",),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT usage_count FROM tm_record WHERE record_id = 1"
                    ).fetchone(),
                    (1,),
                )
            finally:
                connection.close()
            self.assertEqual(len(_registry(sealer)._entries), 0)

    def test_candidate_mutation_before_first_content_capture_rejected(
        self,
    ) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    _, stage = _build_stage(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                    real_capture = tm_stage_sealer._capture_content_file
                    mutated = False

                    def mutate_then_capture(path: Path) -> Any:
                        nonlocal mutated
                        if not mutated and path == stage.staged_db_path:
                            mutated = True
                            connection = _raw_connection(
                                stage.staged_db_path
                            )
                            try:
                                row = connection.execute(
                                    "SELECT gram_size, gram, record_id, "
                                    "term_frequency FROM tm_gram "
                                    "ORDER BY gram_size, gram, record_id "
                                    "LIMIT 1"
                                ).fetchone()
                                self.assertIsNotNone(row)
                                assert row is not None
                                connection.execute(
                                    "UPDATE tm_gram SET term_frequency = ? "
                                    "WHERE gram_size = ? AND gram = ? "
                                    "AND record_id = ?",
                                    (
                                        int(row[3]) + 1,
                                        int(row[0]),
                                        str(row[1]),
                                        int(row[2]),
                                    ),
                                )
                                connection.commit()
                            finally:
                                connection.close()
                        return real_capture(path)

                    sealer = _sealer()
                    self.assertFalse(hasattr(sealer.registry, "seal"))
                    with patch(
                        "tm_stage_sealer._capture_content_file",
                        side_effect=mutate_then_capture,
                    ):
                        _expect_seal_code(
                            self,
                            lambda: _seal(
                                sealer,
                                stage,
                                fts5_available=fts5_available,
                            ),
                            "SEALER.STAGE_MUTATED_AFTER_VALIDATION",
                        )

                    self.assertTrue(mutated)
                    self.assertEqual(len(_registry(sealer)._entries), 0)
                    connection = sqlite3.connect(stage.staged_db_path)
                    try:
                        self.assertEqual(
                            connection.execute(
                                "SELECT value FROM tm_meta "
                                "WHERE key = 'activation_status'"
                            ).fetchone(),
                            ("UNPUBLISHED",),
                        )
                        with self.assertRaises(
                            tm_sqlite_store.CandidateProofIndexError
                        ):
                            tm_sqlite_store.validate_candidate_proof_index(
                                connection,
                                required_sizes=(
                                    (1, 2)
                                    if fts5_available
                                    else (1, 2, 3)
                                ),
                                fts5_available=fts5_available,
                            )
                    finally:
                        connection.close()

    def test_each_candidate_projection_domain_is_bound_post_fsync(
        self,
    ) -> None:
        base_mutations = (
            (
                "gram",
                "UPDATE tm_gram SET term_frequency = term_frequency + 1 "
                "WHERE rowid = (SELECT rowid FROM tm_gram LIMIT 1)",
            ),
            (
                "block",
                "UPDATE tm_candidate_block SET max_source_fold_length = "
                "max_source_fold_length + 1 WHERE block_id = "
                "(SELECT MIN(block_id) FROM tm_candidate_block)",
            ),
            (
                "block_maximum",
                "UPDATE tm_gram_block_max SET max_term_frequency = "
                "max_term_frequency + 1 WHERE rowid = "
                "(SELECT rowid FROM tm_gram_block_max LIMIT 1)",
            ),
        )
        for fts5_available in (False, True):
            mutations = base_mutations + (
                (
                    "fts",
                    "UPDATE tm_fts SET source_fold_v1 = "
                    "source_fold_v1 || 'x' WHERE rowid = "
                    "(SELECT MIN(rowid) FROM tm_fts)",
                ),
            ) if fts5_available else base_mutations
            for domain, statement in mutations:
                with self.subTest(
                    fts5_available=fts5_available,
                    domain=domain,
                ):
                    with tempfile.TemporaryDirectory() as temporary:
                        _, stage = _build_stage(
                            Path(temporary),
                            fts5_available=fts5_available,
                        )
                        real_capture = tm_stage_sealer._capture_content_file
                        mutated = False

                        def mutate_then_capture(path: Path) -> Any:
                            nonlocal mutated
                            if not mutated and path == stage.staged_db_path:
                                mutated = True
                                connection = _raw_connection(
                                    stage.staged_db_path
                                )
                                try:
                                    connection.execute(statement)
                                    connection.commit()
                                finally:
                                    connection.close()
                            return real_capture(path)

                        sealer = _sealer()
                        with patch(
                            "tm_stage_sealer._capture_content_file",
                            side_effect=mutate_then_capture,
                        ):
                            _expect_seal_code(
                                self,
                                lambda: _seal(
                                    sealer,
                                    stage,
                                    fts5_available=fts5_available,
                                ),
                                "SEALER.STAGE_MUTATED_AFTER_VALIDATION",
                            )
                        self.assertTrue(mutated)
                        self.assertEqual(
                            len(_registry(sealer)._entries),
                            0,
                        )

    def test_record_order_swap_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            rows = connection.execute(
                "SELECT record_id, source_raw, target_raw "
                "FROM tm_record ORDER BY record_id LIMIT 2"
            ).fetchall()
            connection.execute(
                "UPDATE tm_record SET source_raw=?, target_raw=? "
                "WHERE record_id=?",
                (rows[1][1], rows[1][2], rows[0][0]),
            )
            connection.execute(
                "UPDATE tm_record SET source_raw=?, target_raw=? "
                "WHERE record_id=?",
                (rows[0][1], rows[0][2], rows[1][0]),
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.RECORD_MISMATCH",
            )

    def test_legacy_line_number_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_record SET legacy_line_no=99 WHERE record_id=1"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.RECORD_LINEAGE_INVALID",
            )

    def test_provenance_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_record SET provenance_json="
                "'[[\"source\",\"other\"]]' WHERE record_id=1"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.PROVENANCE_MISMATCH",
            )

    def test_source_jsonl_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            with identity.configured_jsonl_path.open("ab") as stream:
                stream.write(b"{bad-json}\n")
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.SOURCE_DIGEST_MISMATCH",
            )

    def test_origin_batch_digest_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_origin_batch SET source_digest=? "
                "WHERE batch_id LIKE 'migration.%'",
                ("0" * 64,),
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.SOURCE_DIGEST_MISMATCH",
            )

    def test_missing_gram_row_rejected_in_both_index_modes(self) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    _, stage = _build_stage(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                    connection = _raw_connection(stage.staged_db_path)
                    connection.execute(
                        "DELETE FROM tm_gram "
                        "WHERE record_id=1 AND gram_size=1"
                    )
                    connection.commit()
                    connection.close()
                    sealer = _sealer()
                    _expect_seal_code(
                        self,
                        lambda: _seal(
                            sealer,
                            stage,
                            fts5_available=fts5_available,
                        ),
                        "SEALER.CANDIDATE_INDEX_INCOMPLETE",
                    )

    def test_extra_gram_row_rejected_in_both_index_modes(self) -> None:
        for fts5_available in (False, True):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    _, stage = _build_stage(
                        Path(temporary),
                        fts5_available=fts5_available,
                    )
                    connection = _raw_connection(stage.staged_db_path)
                    connection.execute(
                        "INSERT INTO tm_gram("
                        "gram_size, gram, record_id, term_frequency) "
                        "VALUES (1, 'q', 1, 1)"
                    )
                    connection.commit()
                    connection.close()
                    sealer = _sealer()
                    _expect_seal_code(
                        self,
                        lambda: _seal(
                            sealer,
                            stage,
                            fts5_available=fts5_available,
                        ),
                        "SEALER.CANDIDATE_INDEX_INCOMPLETE",
                    )

    def test_length_tf_and_block_proof_tamper_matrix_is_rejected(self) -> None:
        mutations = (
            (
                "length",
                "UPDATE tm_record SET source_fold_length = "
                "source_fold_length + 1 WHERE record_id = 1",
                "SEALER.FOLD_MISMATCH",
            ),
            (
                "term-frequency",
                "UPDATE tm_gram SET term_frequency = term_frequency + 1 "
                "WHERE record_id = 1 AND gram_size = 1",
                "SEALER.CANDIDATE_INDEX_INCOMPLETE",
            ),
            (
                "block-count",
                "UPDATE tm_candidate_block SET record_count = record_count + 1 "
                "WHERE block_id = 0",
                "SEALER.CANDIDATE_INDEX_INCOMPLETE",
            ),
            (
                "missing-maximum",
                "DELETE FROM tm_gram_block_max WHERE rowid IN "
                "(SELECT rowid FROM tm_gram_block_max LIMIT 1)",
                "SEALER.CANDIDATE_INDEX_INCOMPLETE",
            ),
            (
                "wrong-maximum",
                "UPDATE tm_gram_block_max SET max_term_frequency = "
                "max_term_frequency + 1 WHERE rowid IN "
                "(SELECT rowid FROM tm_gram_block_max LIMIT 1)",
                "SEALER.CANDIDATE_INDEX_INCOMPLETE",
            ),
            (
                "extra-block",
                "INSERT INTO tm_candidate_block VALUES (99, 25345, 25600, 1, 1, 1)",
                "SEALER.CANDIDATE_INDEX_INCOMPLETE",
            ),
        )
        for name, statement, expected_code in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                _, stage = _build_stage(Path(temporary), fts5_available=True)
                connection = _raw_connection(stage.staged_db_path)
                connection.execute(statement)
                connection.commit()
                connection.close()
                _expect_seal_code(
                    self,
                    lambda: _seal(_sealer(), stage, fts5_available=True),
                    expected_code,
                )

    def test_missing_fts_row_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute("DELETE FROM tm_fts WHERE record_id=1")
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.FTS_INDEX_INCOMPLETE",
            )

    def test_extra_fts_row_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "INSERT INTO tm_fts(record_id, source_fold_v1) "
                "VALUES (1, 'extra')"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.FTS_INDEX_INCOMPLETE",
            )

    def test_coordinated_fold_gram_fts_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_record SET source_fold_v1='x' WHERE record_id=1"
            )
            connection.execute(
                "UPDATE tm_gram SET gram=gram || 'x' WHERE record_id=1"
            )
            connection.execute(
                "UPDATE tm_fts SET source_fold_v1='x' WHERE record_id=1"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.FOLD_MISMATCH",
            )

    def test_manifest_replaced_with_foreign_contract_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            receipt = SnapshotReceipt(
                snapshot_id="snapshot.migration.000000000000000000000000",
                resource_id=identity.resource_id,
                canonical_store_id="store.primary",
                exported_revision=1,
                jsonl_digest="0" * 64,
                record_count=1,
            )
            stage.manifest_temp_path.write_text(
                contract_to_json(receipt),
                encoding="utf-8",
            )
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.MANIFEST_INVALID",
            )

    def test_manifest_receipt_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            connection = _raw_connection(stage.staged_db_path)
            receipt_row = connection.execute(
                "SELECT snapshot_id, resource_id, canonical_store_id, "
                "exported_revision, jsonl_digest, record_count, "
                "format_version FROM tm_snapshot_receipt"
            ).fetchone()
            connection.close()
            assert receipt_row is not None
            other_receipt = SnapshotReceipt(
                snapshot_id=receipt_row[0] + ".other",
                resource_id=receipt_row[1],
                canonical_store_id=receipt_row[2],
                exported_revision=receipt_row[3],
                jsonl_digest=receipt_row[4],
                record_count=receipt_row[5] + 1,
                format_version=receipt_row[6],
            )
            manifest = SnapshotManifest(
                manifest_version=SNAPSHOT_MANIFEST_VERSION,
                snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
                receipt=other_receipt,
                receipt_digest=snapshot_receipt_digest(other_receipt),
            )
            stage.manifest_temp_path.write_text(
                contract_to_json(manifest),
                encoding="utf-8",
            )
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.MANIFEST_MISMATCH",
            )


class StageSealerValidationRaceTests(unittest.TestCase):
    def test_committed_update_between_validation_and_marker_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            real_fsync_assets = tm_stage_sealer._fsync_stage_assets

            def commit_tamper_then_fsync(*args: Any) -> None:
                connection = _raw_connection(stage.staged_db_path)
                connection.execute(
                    "UPDATE tm_record SET target_raw='post-validation' "
                    "WHERE record_id=1"
                )
                connection.commit()
                connection.close()
                real_fsync_assets(*args)

            sealer = _sealer()
            with patch(
                "tm_stage_sealer._fsync_stage_assets",
                side_effect=commit_tamper_then_fsync,
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.STAGE_MUTATED_AFTER_VALIDATION",
                )
            self.assertEqual(len(_registry(sealer)._entries), 0)
            connection = sqlite3.connect(str(stage.staged_db_path))
            try:
                status_rows = connection.execute(
                    "SELECT value FROM tm_meta "
                    "WHERE key = 'activation_status'"
                ).fetchall()
                record_count = connection.execute(
                    "SELECT COUNT(*) FROM tm_record"
                ).fetchone()
                target_rows = connection.execute(
                    "SELECT target_raw FROM tm_record WHERE record_id=1"
                ).fetchall()
                integrity_rows = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(status_rows, [("UNPUBLISHED",)])
            self.assertEqual(record_count, (3,))
            self.assertEqual(target_rows, [("post-validation",)])
            self.assertEqual(integrity_rows, [("ok",)])
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.RECORD_MISMATCH",
            )


class StageSealerPhysicalFailureTests(unittest.TestCase):
    def test_integrity_corruption_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = sqlite3.connect(str(stage.staged_db_path))
            connection.execute("PRAGMA writable_schema=ON")
            indexes = connection.execute(
                "SELECT name, rootpage FROM sqlite_master "
                "WHERE type='index' AND name IN "
                "('idx_tm_exact','idx_tm_gram_lookup') ORDER BY name"
            ).fetchall()
            connection.execute(
                "UPDATE sqlite_master SET rootpage=? WHERE name=?",
                (indexes[1][1], indexes[0][0]),
            )
            connection.execute(
                "UPDATE sqlite_master SET rootpage=? WHERE name=?",
                (indexes[0][1], indexes[1][0]),
            )
            connection.execute("PRAGMA writable_schema=OFF")
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.INTEGRITY_FAILED",
            )
            self.assertEqual(len(_registry(sealer)._entries), 0)

    def test_foreign_key_violation_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "INSERT INTO tm_gram("
                "gram_size, gram, record_id, term_frequency) "
                "VALUES (1, '\u0001', 999999, 1)"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.FOREIGN_KEY_FAILED",
            )
            self.assertEqual(len(_registry(sealer)._entries), 0)
            self.assertTrue(stage.staged_db_path.is_file())
            self.assertTrue(stage.manifest_temp_path.is_file())

    def test_open_write_transaction_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            locker = sqlite3.connect(str(stage.staged_db_path))
            try:
                locker.execute("BEGIN EXCLUSIVE")
                sealer = _sealer()
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.STAGE_INVALID",
                )
                self.assertEqual(len(_registry(sealer)._entries), 0)
                self.assertTrue(stage.staged_db_path.is_file())
                self.assertTrue(stage.manifest_temp_path.is_file())
            finally:
                locker.close()

    def test_fsync_sequence_closes_marker_before_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            calls: list[tuple[str, str]] = []
            real_file_fsync = tm_stage_sealer._fsync_file
            real_directory_fsync = tm_stage_sealer._fsync_directory
            real_mark_sealed = tm_stage_sealer._mark_stage_sealed
            real_commit = SealedArtifactRegistry._commit_verified

            def record_file(path: Path, expected: object) -> None:
                calls.append(("file", path.name))
                real_file_fsync(path, cast(Any, expected))

            def record_directory(path: Path) -> None:
                calls.append(("dir", path.name))
                real_directory_fsync(path)

            def record_marker(
                connection: sqlite3.Connection,
                *,
                expected_closure_digest: str,
            ) -> None:
                self.assertTrue(connection.in_transaction)
                calls.append(("marker", stage.staged_db_path.name))
                real_mark_sealed(
                    connection,
                    expected_closure_digest=expected_closure_digest,
                )

            def record_commit(
                self: SealedArtifactRegistry,
                capability: tm_stage_sealer._VerifiedSealCommitCapability,
            ) -> SealedStage:
                calls.append(
                    (
                        "commit",
                        capability.reservation.mutable.staged_db_path.name,
                    )
                )
                return real_commit(self, capability)

            sealer = _sealer()
            with (
                patch(
                    "tm_stage_sealer._fsync_file",
                    side_effect=record_file,
                ),
                patch(
                    "tm_stage_sealer._fsync_directory",
                    side_effect=record_directory,
                ),
                patch(
                    "tm_stage_sealer._mark_stage_sealed",
                    side_effect=record_marker,
                ),
                patch.object(
                    SealedArtifactRegistry,
                    "_commit_verified",
                    autospec=True,
                    side_effect=record_commit,
                ),
            ):
                sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(
                calls,
                [
                    ("marker", stage.staged_db_path.name),
                    ("file", stage.staged_db_path.name),
                    ("file", stage.manifest_temp_path.name),
                    ("dir", stage.staged_db_path.parent.name),
                    ("commit", stage.staged_db_path.name),
                ],
            )

    def test_fsync_failure_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            with patch(
                "tm_stage_sealer.os.fsync",
                side_effect=OSError("injected fsync failure"),
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.FSYNC_FAILED",
                )
            self.assertEqual(len(_registry(sealer)._entries), 0)
            self.assertTrue(stage.staged_db_path.is_file())
            self.assertTrue(stage.manifest_temp_path.is_file())
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )

    def test_one_shot_fsync_failure_is_deterministically_retryable(
        self,
    ) -> None:
        real_fsync = tm_stage_sealer.os.fsync
        for failed_call in (1, 2, 3):
            with self.subTest(failed_call=failed_call):
                with tempfile.TemporaryDirectory() as temporary:
                    identity, stage = _build_stage(
                        Path(temporary),
                        fts5_available=True,
                    )
                    sealer = _sealer()
                    calls = [0]

                    def one_shot_fsync(descriptor: int) -> None:
                        calls[0] += 1
                        if calls[0] == failed_call:
                            raise OSError(
                                "injected one-shot fsync failure"
                            )
                        real_fsync(descriptor)

                    with patch(
                        "tm_stage_sealer.os.fsync",
                        side_effect=one_shot_fsync,
                    ):
                        _expect_seal_code(
                            self,
                            lambda: _seal(
                                sealer,
                                stage,
                                fts5_available=True,
                            ),
                            "SEALER.FSYNC_FAILED",
                        )
                    self.assertEqual(calls[0], failed_call + 2)
                    self.assertEqual(
                        len(_registry(sealer)._entries),
                        0,
                    )
                    self.assertEqual(
                        len(_registry(sealer)._reservations),
                        0,
                    )
                    self.assertTrue(stage.staged_db_path.is_file())
                    self.assertTrue(stage.manifest_temp_path.is_file())
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        SOURCE_BYTES,
                    )
                    connection = sqlite3.connect(
                        str(stage.staged_db_path)
                    )
                    try:
                        status_rows = connection.execute(
                            "SELECT value FROM tm_meta "
                            "WHERE key = 'activation_status'"
                        ).fetchall()
                    finally:
                        connection.close()
                    self.assertEqual(status_rows, [("UNPUBLISHED",)])

                    sealed = _seal(sealer, stage, fts5_available=True)
                    self.assertTrue(sealer.registry.contains(sealed))
                    self.assertEqual(
                        len(_registry(sealer)._entries),
                        1,
                    )
                    self.assertEqual(sealed.evidence.record_count, 3)
                    connection = sqlite3.connect(
                        str(stage.staged_db_path)
                    )
                    try:
                        record_count = connection.execute(
                            "SELECT COUNT(*) FROM tm_record"
                        ).fetchone()
                    finally:
                        connection.close()
                    self.assertEqual(record_count, (3,))
                    _expect_seal_code(
                        self,
                        lambda: _seal(
                            sealer,
                            stage,
                            fts5_available=True,
                        ),
                        "SEALER.STAGE_INVALID",
                    )



class StageSealerDurableRetryTests(unittest.TestCase):
    """Finding B: failure-injection retry determinism with disk-state proof."""

    def test_reservation_failure_leaves_no_state_and_retry_succeeds(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            with patch.object(
                SealedArtifactRegistry,
                "_reserve",
                side_effect=StageSealError("SEALER.ALREADY_RESERVED"),
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.ALREADY_RESERVED",
                )
            _assert_clean_unpublished(self, sealer, stage)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(len(_registry(sealer)._entries), 1)

    def test_post_marker_failures_restore_unpublished_and_retry_succeeds(
        self,
    ) -> None:
        real_verify_sealed = tm_stage_sealer._verify_sealed_stage
        real_verify_manifest = tm_stage_sealer._verify_manifest_at_digest
        real_capture = tm_stage_sealer._capture_content_file

        def one_shot(
            function: Any,
            *,
            code: str,
        ) -> Any:
            state = [0]

            def injected(*args: Any, **kwargs: Any) -> Any:
                state[0] += 1
                if state[0] == 1:
                    raise StageSealError(code)
                return function(*args, **kwargs)

            return injected

        cases: tuple[tuple[str, str, str], ...] = (
            (
                "post_marker_db_digest",
                "tm_stage_sealer._capture_content_file",
                "SEALER.DIGEST_UNREADABLE",
            ),
            (
                "post_marker_manifest",
                "tm_stage_sealer._verify_manifest_at_digest",
                "SEALER.MANIFEST_INVALID",
            ),
            (
                "post_marker_reopen_evidence",
                "tm_stage_sealer._verify_sealed_stage",
                "SEALER.INTEGRITY_FAILED",
            ),
        )
        functions = {
            "tm_stage_sealer._capture_content_file": real_capture,
            "tm_stage_sealer._verify_manifest_at_digest": real_verify_manifest,
            "tm_stage_sealer._verify_sealed_stage": real_verify_sealed,
        }
        for name, patch_target, expected_code in cases:
            with self.subTest(failure=name):
                with tempfile.TemporaryDirectory() as temporary:
                    identity, stage = _build_stage(
                        Path(temporary),
                        fts5_available=True,
                    )
                    sealer = _sealer()
                    with patch(
                        patch_target,
                        side_effect=one_shot(
                            functions[patch_target],
                            code=expected_code,
                        ),
                    ):
                        _expect_seal_code(
                            self,
                            lambda: _seal(
                                sealer,
                                stage,
                                fts5_available=True,
                            ),
                            expected_code,
                        )
                    _assert_clean_unpublished(self, sealer, stage)
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        SOURCE_BYTES,
                    )
                    sealed = _seal(sealer, stage, fts5_available=True)
                    self.assertTrue(sealer.registry.contains(sealed))
                    self.assertEqual(len(_registry(sealer)._entries), 1)
                    connection = sqlite3.connect(
                        str(stage.staged_db_path)
                    )
                    try:
                        record_count = connection.execute(
                            "SELECT COUNT(*) FROM tm_record"
                        ).fetchone()
                    finally:
                        connection.close()
                    self.assertEqual(record_count, (3,))

    def test_registry_commit_failure_restores_unpublished_and_retry_succeeds(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            real_commit = SealedArtifactRegistry._commit_verified
            state = [0]

            def one_shot_commit(
                self_registry: SealedArtifactRegistry,
                capability: tm_stage_sealer._VerifiedSealCommitCapability,
            ) -> SealedStage:
                state[0] += 1
                if state[0] == 1:
                    raise OSError("injected registry commit failure")
                return real_commit(self_registry, capability)

            with patch.object(
                SealedArtifactRegistry,
                "_commit_verified",
                autospec=True,
                side_effect=one_shot_commit,
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.STAGE_INVALID",
                )
            _assert_clean_unpublished(self, sealer, stage)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(len(_registry(sealer)._entries), 1)
            connection = sqlite3.connect(str(stage.staged_db_path))
            try:
                record_count = connection.execute(
                    "SELECT COUNT(*) FROM tm_record"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(record_count, (3,))


class StageSealerIdentitySwapTests(unittest.TestCase):
    """Finding A: creation-time identity enforcement at registration."""

    def _swap_with_identical_bytes(self, target: Path) -> None:
        payload = target.read_bytes()
        replacement = target.with_name(f"{target.name}.replacement")
        replacement.write_bytes(payload)
        os.replace(replacement, target)

    def _identity_of(self, path: Path) -> tuple[int, int]:
        observed = tm_stage_sealer._artifact_file_identity(
            path,
            missing_code="SEALER.STAGE_DATABASE_MISSING",
            unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
        )
        return (observed.device, observed.inode)

    def test_byte_identical_db_inode_swap_before_registration_denied(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            sealer = _sealer()
            original_identity = self._identity_of(stage.staged_db_path)
            swapped_bytes: list[bytes] = []
            real_build_binding = tm_stage_sealer._build_binding

            def swap_db_before_registration(
                identity: CanonicalResourceIdentity,
                receipt: SnapshotReceipt,
                manifest: SnapshotManifest,
            ) -> SnapshotBinding:
                swapped_bytes.append(stage.staged_db_path.read_bytes())
                self._swap_with_identical_bytes(stage.staged_db_path)
                return real_build_binding(identity, receipt, manifest)

            with patch(
                "tm_stage_sealer._build_binding",
                side_effect=swap_db_before_registration,
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.STAGE_DATABASE_UNSAFE",
                )
            registry = _registry(sealer)
            self.assertEqual(len(registry._entries), 0)
            self.assertEqual(len(registry._reservations), 0)
            self.assertNotEqual(
                self._identity_of(stage.staged_db_path),
                original_identity,
            )
            self.assertEqual(
                stage.staged_db_path.read_bytes(),
                swapped_bytes[0],
            )
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.STAGE_INVALID",
            )
            self.assertEqual(len(registry._entries), 0)
            self.assertEqual(len(registry._reservations), 0)

    def test_byte_identical_manifest_inode_swap_before_registration_denied(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            sealer = _sealer()
            original_identity = self._identity_of(stage.manifest_temp_path)
            real_build_binding = tm_stage_sealer._build_binding

            def swap_manifest_before_registration(
                identity: CanonicalResourceIdentity,
                receipt: SnapshotReceipt,
                manifest: SnapshotManifest,
            ) -> SnapshotBinding:
                self._swap_with_identical_bytes(stage.manifest_temp_path)
                return real_build_binding(identity, receipt, manifest)

            with patch(
                "tm_stage_sealer._build_binding",
                side_effect=swap_manifest_before_registration,
            ):
                _expect_seal_code(
                    self,
                    lambda: _seal(sealer, stage, fts5_available=True),
                    "SEALER.STAGE_MANIFEST_UNSAFE",
                )
            registry = _registry(sealer)
            self.assertEqual(len(registry._entries), 0)
            self.assertEqual(len(registry._reservations), 0)
            self.assertNotEqual(
                self._identity_of(stage.manifest_temp_path),
                original_identity,
            )
            _assert_clean_unpublished(self, sealer, stage)
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(len(registry._entries), 1)
            self.assertEqual(len(registry._reservations), 0)

    def test_registry_rejects_double_reservation_and_stale_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            registry = _registry(_sealer())
            database_identity = tm_stage_sealer._artifact_file_identity(
                stage.staged_db_path,
                missing_code="SEALER.STAGE_DATABASE_MISSING",
                unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
            )
            manifest_identity = tm_stage_sealer._artifact_file_identity(
                stage.manifest_temp_path,
                missing_code="SEALER.STAGE_MANIFEST_MISSING",
                unsafe_code="SEALER.STAGE_MANIFEST_UNSAFE",
            )
            reservation = registry._reserve(
                stage,
                database_identity=database_identity,
                manifest_identity=manifest_identity,
            )
            with self.assertRaisesRegex(
                StageSealError,
                "^SEALER.ALREADY_RESERVED$",
            ):
                registry._reserve(
                    stage,
                    database_identity=database_identity,
                    manifest_identity=manifest_identity,
                )
            registry._release(reservation)
            with self.assertRaisesRegex(
                StageSealError,
                "^SEALER.TYPE_INVALID$",
            ):
                registry._commit_verified(cast(Any, object()))
            self.assertEqual(len(registry._reservations), 0)
            self.assertEqual(len(registry._entries), 0)
            sealer = StageSealer(
                registry=registry,
                canonical_store_id="store.primary",
            )
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(len(registry._entries), 1)
            self.assertEqual(len(registry._reservations), 0)
            with self.assertRaisesRegex(
                StageSealError,
                "^SEALER.ALREADY_SEALED$",
            ):
                registry._reserve(
                    stage,
                    database_identity=database_identity,
                    manifest_identity=manifest_identity,
                )

    def test_release_is_idempotent_and_retry_seals_exactly_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            registry = _registry(_sealer())
            database_identity = tm_stage_sealer._artifact_file_identity(
                stage.staged_db_path,
                missing_code="SEALER.STAGE_DATABASE_MISSING",
                unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
            )
            manifest_identity = tm_stage_sealer._artifact_file_identity(
                stage.manifest_temp_path,
                missing_code="SEALER.STAGE_MANIFEST_MISSING",
                unsafe_code="SEALER.STAGE_MANIFEST_UNSAFE",
            )
            reservation = registry._reserve(
                stage,
                database_identity=database_identity,
                manifest_identity=manifest_identity,
            )
            registry._release(reservation)
            registry._release(reservation)
            self.assertEqual(len(registry._reservations), 0)
            sealer = StageSealer(
                registry=registry,
                canonical_store_id="store.primary",
            )
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(len(registry._entries), 1)
            self.assertEqual(len(registry._reservations), 0)


class StageSealerRegistryTests(unittest.TestCase):

    def _sealed_fixture(
        self,
    ) -> tuple[
        tempfile.TemporaryDirectory[str],
        StageSealer,
        MutableStageRef,
        SealedStage,
    ]:
        temporary = tempfile.TemporaryDirectory()
        try:
            _, stage = _build_stage(
                Path(temporary.name),
                fts5_available=True,
            )
            sealer = _sealer()
            sealed = _seal(sealer, stage, fts5_available=True)
        except BaseException:
            temporary.cleanup()
            raise
        return temporary, sealer, stage, sealed

    def test_post_seal_database_mutation_detected(self) -> None:
        temporary, sealer, stage, sealed = self._sealed_fixture()
        try:
            connection = sqlite3.connect(str(stage.staged_db_path))
            connection.execute("PRAGMA user_version=99")
            connection.commit()
            connection.close()
            self.assertFalse(sealer.registry.contains(sealed))
            _expect_seal_code(
                self,
                lambda: sealer.registry.state(sealed),
                "SEALER.ARTIFACT_MUTATED",
            )
        finally:
            temporary.cleanup()

    def test_post_seal_manifest_mutation_detected(self) -> None:
        temporary, sealer, _, sealed = self._sealed_fixture()
        try:
            mutable = _registry(sealer)._entries[
                sealed.artifact.artifact_id
            ].mutable
            with mutable.manifest_temp_path.open("ab") as stream:
                stream.write(b"\n")
            self.assertFalse(sealer.registry.contains(sealed))
            _expect_seal_code(
                self,
                lambda: sealer.registry.state(sealed),
                "SEALER.ARTIFACT_MUTATED",
            )
        finally:
            temporary.cleanup()

    def test_second_sealer_seal_of_same_stage_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            sealer = _sealer()
            _ = _seal(sealer, stage, fts5_available=True)
            _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.STAGE_INVALID",
            )

    def test_registry_exposes_no_direct_seal_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            sealer = _sealer()
            for name in (
                "seal",
                "reserve",
                "commit",
                "release",
                "_reserve",
                "_commit_verified",
                "_release",
                "issue_token",
                "consume",
                "cancel",
            ):
                self.assertFalse(hasattr(sealer.registry, name), name)
            self.assertNotIn("SealedArtifactRegistry", tm_stage_sealer.__all__)
            self.assertFalse(
                hasattr(tm_stage_sealer, "SealedArtifactRegistry")
            )
            sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertFalse(hasattr(sealer.registry, "seal"))

    def test_coordinator_exposes_no_registry_mutation_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            self.assertFalse(hasattr(coordinator, "sealed_registry"))
            public_names = {
                name
                for name in dir(coordinator)
                if not name.startswith("_")
            }
            self.assertNotIn("reserve", public_names)
            self.assertNotIn("commit", public_names)
            self.assertNotIn("release", public_names)

    def test_verified_commit_capability_is_exact_bound_and_single_use(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            sealer = _sealer()
            registry = _registry(sealer)
            captured: list[
                tm_stage_sealer._VerifiedSealCommitCapability
            ] = []
            real_commit = SealedArtifactRegistry._commit_verified

            def capture_commit(
                self_registry: SealedArtifactRegistry,
                capability: tm_stage_sealer._VerifiedSealCommitCapability,
            ) -> SealedStage:
                captured.append(capability)
                return real_commit(self_registry, capability)

            with patch.object(
                SealedArtifactRegistry,
                "_commit_verified",
                autospec=True,
                side_effect=capture_commit,
            ):
                sealed = _seal(sealer, stage, fts5_available=True)
            self.assertTrue(sealer.registry.contains(sealed))
            self.assertEqual(len(captured), 1)
            capability = captured[0]

            with self.assertRaises(TypeError):
                dataclass_replace(capability)
            with self.assertRaisesRegex(
                StageSealError,
                "^SEALER.RESERVATION_MISMATCH$",
            ):
                registry._commit_verified(capability)
            foreign = SealedArtifactRegistry(
                registry_namespace=registry.registry_namespace
            )
            with self.assertRaisesRegex(
                StageSealError,
                "^SEALER.RESERVATION_MISMATCH$",
            ):
                foreign._commit_verified(capability)

            second_root = Path(temporary) / "second"
            second_root.mkdir()
            _, second_stage = _build_stage(
                second_root,
                fts5_available=True,
            )
            second_reservation = registry._reserve(
                second_stage,
                database_identity=tm_stage_sealer._artifact_file_identity(
                    second_stage.staged_db_path,
                    missing_code="SEALER.STAGE_DATABASE_MISSING",
                    unsafe_code="SEALER.STAGE_DATABASE_UNSAFE",
                ),
                manifest_identity=tm_stage_sealer._artifact_file_identity(
                    second_stage.manifest_temp_path,
                    missing_code="SEALER.STAGE_MANIFEST_MISSING",
                    unsafe_code="SEALER.STAGE_MANIFEST_UNSAFE",
                ),
            )
            cross = object.__new__(
                tm_stage_sealer._VerifiedSealCommitCapability
            )
            for name, value in vars(capability).items():
                object.__setattr__(cross, name, value)
            object.__setattr__(cross, "reservation", second_reservation)
            object.__setattr__(
                cross,
                "nonce",
                "verified-seal.cross-reservation",
            )
            with self.assertRaises(StageSealError):
                registry._commit_verified(cross)
            registry._release(second_reservation)
            self.assertEqual(len(registry._reservations), 0)

            @dataclass(frozen=True)
            class ForgedCapability:
                reservation: object
                evidence: object
                generation: object
                attestation: object

            forged = ForgedCapability(
                capability.reservation,
                capability.evidence,
                capability.generation,
                capability.attestation,
            )
            with self.assertRaisesRegex(
                StageSealError,
                "^SEALER.TYPE_INVALID$",
            ):
                registry._commit_verified(cast(Any, forged))
            uninitialized = (
                tm_stage_sealer._VerifiedSealCommitCapability()
            )
            with self.assertRaisesRegex(
                StageSealError,
                "^SEALER.TYPE_INVALID$",
            ):
                registry._commit_verified(uninitialized)

    def test_forged_stage_rejected(self) -> None:
        temporary, sealer, _, sealed = self._sealed_fixture()
        try:
            forged = object.__new__(SealedStage)
            for field_name, value in vars(sealed).items():
                object.__setattr__(forged, field_name, value)
            self.assertFalse(sealer.registry.contains(forged))
            _expect_seal_code(
                self,
                lambda: sealer.registry.state(forged),
                "SEALER.REGISTRY_MISMATCH",
            )
        finally:
            temporary.cleanup()

    def test_foreign_registry_rejected(self) -> None:
        temporary, sealer, _, sealed = self._sealed_fixture()
        try:
            foreign = StageSealer(
                registry=SealedArtifactRegistry(
                    registry_namespace="coordinator.other"
                ),
                canonical_store_id="store.primary",
            )
            self.assertFalse(foreign.registry.contains(sealed))
            _expect_seal_code(
                self,
                lambda: foreign.registry.state(sealed),
                "SEALER.REGISTRY_MISMATCH",
            )
        finally:
            temporary.cleanup()

    def test_registry_entry_mutation_rejected(self) -> None:
        temporary, sealer, stage, sealed = self._sealed_fixture()
        try:
            entry = _registry(sealer)._entries[sealed.artifact.artifact_id]
            other_stage = MutableStageRef(
                stage_id=stage.stage_id,
                resource_identity=stage.resource_identity,
                staged_db_path=stage.staged_db_path.with_name("other.db"),
                manifest_temp_path=stage.manifest_temp_path.with_name(
                    "other.manifest.json"
                ),
            )
            object.__setattr__(entry, "mutable", other_stage)
            self.assertFalse(sealer.registry.contains(sealed))
            _expect_seal_code(
                self,
                lambda: sealer.registry.state(sealed),
                "SEALER.REGISTRY_MISMATCH",
            )
        finally:
            temporary.cleanup()

    def test_token_lifecycle_is_exact_single_use_and_terminal(self) -> None:
        temporary, sealer, _, sealed = self._sealed_fixture()
        try:
            registry = _registry(sealer)
            self.assertIs(
                registry.state(sealed),
                ActivationCapabilityState.SEALED,
            )
            _expect_seal_code(
                self,
                lambda: registry.issue_token(
                    sealed,
                    current_generation=2,
                ),
                "SEALER.GENERATION_MISMATCH",
            )
            token = registry.issue_token(
                sealed,
                current_generation=None,
            )
            self.assertIs(
                registry.state(sealed),
                ActivationCapabilityState.TOKEN_ISSUED,
            )
            _expect_seal_code(
                self,
                lambda: registry.issue_token(
                    sealed,
                    current_generation=None,
                ),
                "SEALER.TOKEN_ALREADY_ISSUED",
            )
            registry.consume(token)
            self.assertIs(
                registry.state(sealed),
                ActivationCapabilityState.CONSUMED,
            )
            _expect_seal_code(
                self,
                lambda: registry.consume(token),
                "SEALER.TOKEN_NOT_ACTIVE",
            )
            _expect_seal_code(
                self,
                lambda: registry.cancel(token),
                "SEALER.TOKEN_NOT_ACTIVE",
            )
            _expect_seal_code(
                self,
                lambda: registry.cancel(
                    cast(contract_module._ActivationToken, object())
                ),
                "SEALER.TOKEN_INVALID",
            )
        finally:
            temporary.cleanup()

    def test_activation_nonce_replay_is_global_across_registry_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = SealedArtifactRegistry(
                registry_namespace="coordinator.primary"
            )
            sealer = StageSealer(
                registry=registry,
                canonical_store_id="store.primary",
            )
            (root / "first").mkdir()
            _, first_stage = _build_stage(
                root / "first",
                fts5_available=True,
            )
            first = _seal(sealer, first_stage, fts5_available=True)
            first_token = registry.issue_token(
                first,
                current_generation=None,
            )
            registry.cancel(first_token)

            (root / "second").mkdir()
            _, second_stage = _build_stage(
                root / "second",
                fts5_available=True,
            )
            second = _seal(sealer, second_stage, fts5_available=True)
            second_entry = registry._entries[second.artifact.artifact_id]
            replay = contract_module._create_sealed_stage(
                registry_namespace=registry.registry_namespace,
                artifact_id=second.artifact.artifact_id,
                mutable_stage=second_entry.mutable,
                evidence=second.evidence,
                generation=second.generation,
                activation_nonce=first.activation_nonce,
            )
            object.__setattr__(second_entry, "stage", replay)

            _expect_seal_code(
                self,
                lambda: registry.issue_token(
                    replay,
                    current_generation=None,
                ),
                "SEALER.NONCE_REPLAY",
            )
            self.assertIs(
                registry.state(replay),
                ActivationCapabilityState.SEALED,
            )


class StageSealerInputValidationTests(unittest.TestCase):
    def test_init_rejects_invalid_registry_and_store_id(self) -> None:
        invalid_constructors: tuple[tuple[Any, Any], ...] = (
            (object(), "store.primary"),
            (_FakeRegistry(""), "store.primary"),
            (_FakeRegistry("   "), "store.primary"),
            (_FakeRegistry(7), "store.primary"),
            (_FakeRegistry("coordinator.primary"), ""),
            (_FakeRegistry("coordinator.primary"), "  "),
            (_FakeRegistry("coordinator.primary"), 7),
        )
        for registry_value, store_id in invalid_constructors:
            with self.subTest(
                registry_value=registry_value,
                store_id=store_id,
            ):
                _expect_seal_code(
                    self,
                    lambda: StageSealer(
                        registry=cast(Any, registry_value),
                        canonical_store_id=cast(Any, store_id),
                    ),
                    "SEALER.TYPE_INVALID",
                )
        with self.assertRaisesRegex(ValueError, "^registry_namespace"):
            SealedArtifactRegistry(registry_namespace="")
        with self.assertRaisesRegex(TypeError, "registry_namespace"):
            SealedArtifactRegistry(registry_namespace=cast(Any, 7))

    def test_seal_rejects_invalid_expected_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            sealer = _sealer()
            for bad_value in (True, "3", 3.0):
                with self.subTest(bad_value=bad_value):
                    _expect_seal_code(
                        self,
                        lambda: _seal(
                            sealer,
                            stage,
                            fts5_available=True,
                            expected_prior_generation=cast(int, bad_value),
                        ),
                        "SEALER.TYPE_INVALID",
                    )
            _expect_seal_code(
                self,
                lambda: _seal(
                    sealer,
                    stage,
                    fts5_available=True,
                    expected_prior_generation=-1,
                ),
                "SEALER.GENERATION_INVALID",
            )

    def test_seal_rejects_exact_type_violations_before_fs_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, stage = _build_stage(
                Path(temporary),
                fts5_available=True,
            )
            sealer = _sealer()
            with self.assertRaises(TypeError):
                _seal(
                    sealer,
                    cast(MutableStageRef, object()),
                    fts5_available=True,
                )
            subclass_stage = MutableStageRef(
                stage_id=stage.stage_id,
                resource_identity=stage.resource_identity,
                staged_db_path=_PathSubclass(stage.staged_db_path),
                manifest_temp_path=stage.manifest_temp_path,
            )
            with self.assertRaises(TypeError):
                _seal(sealer, subclass_stage, fts5_available=True)
            self.assertEqual(len(_registry(sealer)._entries), 0)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )

    def test_seal_error_codes_never_embed_paths_or_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, stage = _build_stage(Path(temporary), fts5_available=True)
            connection = _raw_connection(stage.staged_db_path)
            connection.execute(
                "UPDATE tm_record SET target_raw='tampered' "
                "WHERE record_id=1"
            )
            connection.commit()
            connection.close()
            sealer = _sealer()
            error = _expect_seal_code(
                self,
                lambda: _seal(sealer, stage, fts5_available=True),
                "SEALER.RECORD_MISMATCH",
            )
            self.assertEqual(str(error), "SEALER.RECORD_MISMATCH")
            self.assertNotIn(str(stage.staged_db_path), str(error))
            with self.assertRaises(TypeError):
                StageSealError(cast(str, cast(object, 7)))


if __name__ == "__main__":
    unittest.main()
