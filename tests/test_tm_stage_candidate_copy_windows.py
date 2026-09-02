"""Windows tests for sealed-stage exact copy into owner publication candidates."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

import tm_stage_sealer
from platform_fs_contracts import BoundRegularFile
from tm_contracts import CanonicalResourceIdentity
from tm_migration import TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator


_SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"other","target":"value"}\n'
)


def _identity(root: Path) -> CanonicalResourceIdentity:
    source = (root / "tm.primary.jsonl").resolve()
    source.write_bytes(_SOURCE_BYTES)
    return CanonicalResourceIdentity.from_configured_jsonl(
        "tm.primary",
        source,
    )


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class WindowsPortableStageCandidateCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.identity = _identity(self.root)
        self.coordinator = ResourceStoreCoordinator(
            canonical_store_id="store.primary",
            resource_identity=self.identity,
        )
        self.service = TMMigrationService(
            resource_identity=self.identity,
            canonical_store_id="store.primary",
            coordinator=self.coordinator,
        )
        with mock.patch("tm_sqlite_store._probe_fts5", return_value=True):
            build = self.service.build_mutable_stage(
                self.identity.configured_jsonl_path
            )
        self.stage = build.mutable_stage
        if self.stage is None:
            raise AssertionError("expected one mutable stage")
        self.reservation = self.service._acquire_initial_reservation()
        with mock.patch("tm_sqlite_store._probe_fts5", return_value=True):
            self.sealed = self.coordinator._seal_stage(
                self.stage,
                canonical_store_id="store.primary",
                expected_prior_generation=None,
                **self.reservation.stage_seal_inputs(),
            )
        self.registry = self.coordinator._sealed_registry

    def tearDown(self) -> None:
        try:
            entry = self.registry._entries.get(self.sealed.artifact.artifact_id)
            if entry is not None and entry.live_authority is not None:
                self.registry.retire_unissued_portable(self.sealed)
        finally:
            self.reservation.release()
            self._temporary.cleanup()

    def _borrow(self) -> tm_stage_sealer._PortableAuthorityBorrow:
        snapshot = self.registry.resolve_physical_readiness(self.sealed)
        if type(snapshot) is not tm_stage_sealer._PortablePhysicalReadinessSnapshot:
            raise AssertionError("expected portable physical readiness")
        return snapshot.live_reproof

    def _copy(
        self,
        asset: str,
        name: str,
    ) -> tuple[object, object]:
        return self._borrow().copy_asset_to_new_candidate(
            platform=self.reservation._backend,
            parent=self.reservation._root,
            asset=asset,
            candidate_name=name,
        )

    def _close_and_unlink(self, candidate: object, name: str) -> None:
        identity = candidate.identity()
        candidate.close()
        self.reservation._root.unlink_owned(name, identity)

    def test_database_and_manifest_copy_exact_bytes_and_consume_borrow(self) -> None:
        expected_paths = {
            "database": self.stage.staged_db_path,
            "manifest": self.stage.manifest_temp_path,
        }
        for asset, source_path in expected_paths.items():
            with self.subTest(asset=asset):
                expected = source_path.read_bytes()
                borrow = self._borrow()
                name = f"publication-{asset}.candidate"
                candidate, facts = borrow.copy_asset_to_new_candidate(
                    platform=self.reservation._backend,
                    parent=self.reservation._root,
                    asset=asset,
                    candidate_name=name,
                )
                try:
                    self.assertFalse(candidate.closed)
                    self.assertEqual(facts.byte_count, len(expected))
                    self.assertEqual(
                        facts.content_sha256,
                        hashlib.sha256(expected).digest(),
                    )
                    candidate.flush_content()
                    identity = candidate.identity()
                    candidate.close()
                    self.assertEqual((self.root / name).read_bytes(), expected)
                    self.reservation._root.unlink_owned(name, identity)
                    with self.assertRaisesRegex(
                        tm_stage_sealer.StageSealError,
                        "SEALER.ATTESTATION_UNAVAILABLE",
                    ):
                        borrow.reprove()
                finally:
                    if not candidate.closed:
                        self._close_and_unlink(candidate, name)

    def test_database_bulk_copy_uses_one_proof_window_and_short_reads(self) -> None:
        candidate_name = "publication-bulk.candidate"
        entry = self.registry._entries[self.sealed.artifact.artifact_id]
        live_authority = entry.live_authority
        if live_authority is None:
            raise AssertionError("expected portable live authority")
        source = live_authority._PortableStageLiveAuthority__database
        source_type = type(source)
        real_reprove = source_type._reprove
        real_read = source._api.ReadFile
        source_reproves = 0
        source_reads = 0

        def count_source_reproof(bound: object) -> object:
            nonlocal source_reproves
            if bound is source:
                source_reproves += 1
            return real_reprove(bound)

        def short_read(
            handle: int,
            buffer: object,
            requested: int,
            read: object,
            overlapped: object,
        ) -> int:
            nonlocal source_reads
            limited = min(int(requested), 97)
            result = real_read(handle, buffer, limited, read, overlapped)
            source_reads += 1
            return result

        borrow = self._borrow()
        expected = self.stage.staged_db_path.read_bytes()
        try:
            with mock.patch.object(
                source_type,
                "_reprove",
                new=count_source_reproof,
            ), mock.patch.object(
                source_type,
                "read_at",
                wraps=source_type.read_at,
            ) as read_at, mock.patch.object(
                source._api,
                "ReadFile",
                side_effect=short_read,
            ):
                candidate, facts = borrow.copy_asset_to_new_candidate(
                    platform=self.reservation._backend,
                    parent=self.reservation._root,
                    asset="database",
                    candidate_name=candidate_name,
                )
            try:
                self.assertEqual(source_reproves, 4)
                self.assertGreater(source_reads, 2)
                read_at.assert_not_called()
                self.assertEqual(facts.byte_count, len(expected))
                self.assertEqual(
                    facts.content_sha256,
                    hashlib.sha256(expected).digest(),
                )
                candidate.flush_content()
                identity = candidate.identity()
                candidate.close()
                self.assertEqual((self.root / candidate_name).read_bytes(), expected)
                self.reservation._root.unlink_owned(candidate_name, identity)
            finally:
                if not candidate.closed:
                    self._close_and_unlink(candidate, candidate_name)
        finally:
            candidate_path = self.root / candidate_name
            if candidate_path.exists():
                candidate_path.unlink()

    def test_mid_copy_source_snapshot_drift_fails_closed(self) -> None:
        candidate_name = "publication-mutated.candidate"
        entry = self.registry._entries[self.sealed.artifact.artifact_id]
        live_authority = entry.live_authority
        if live_authority is None:
            raise AssertionError("expected portable live authority")
        source = live_authority._PortableStageLiveAuthority__database
        source_type = type(source)
        real_reprove = source_type._reprove
        after_body = False

        def mark_after_body(phase: str) -> None:
            nonlocal after_body
            if phase == "windows_after_body_read":
                after_body = True

        def drift_after_copy(bound: object) -> object:
            proof = real_reprove(bound)
            if bound is not source or not after_body:
                return proof
            snapshot = tm_stage_sealer.EntrySnapshot(
                identity=proof.snapshot.identity,
                byte_count=proof.snapshot.byte_count,
                modified_token=hashlib.sha256(
                    proof.snapshot.modified_token + b"drift"
                ).digest(),
                reparse_free=proof.snapshot.reparse_free,
            )
            return type(proof)(proof.identity, snapshot, proof.final_path)

        source._fault_injector = mark_after_body
        try:
            with mock.patch.object(
                source_type,
                "_reprove",
                new=drift_after_copy,
            ):
                with self.assertRaises(tm_stage_sealer.StageSealError) as caught:
                    self._copy("database", candidate_name)
        finally:
            source._fault_injector = None
        self.assertIn(
            caught.exception.error_code,
            {
                "SEALER.ARTIFACT_MUTATED",
                "SEALER.ATTESTATION_UNAVAILABLE",
            },
        )
        self.assertTrue(after_body)
        self.assertEqual(self.registry._portable_borrows, {})
        candidate_path = self.root / candidate_name
        if candidate_path.exists():
            candidate_path.unlink()

    def test_consumer_fault_closes_stream_and_next_thread_rehashes(self) -> None:
        candidate_name = "publication-interrupted.candidate"
        entry = self.registry._entries[self.sealed.artifact.artifact_id]
        live_authority = entry.live_authority
        if live_authority is None:
            raise AssertionError("expected portable live authority")
        source = live_authority._PortableStageLiveAuthority__database
        expected_snapshot = source.snapshot()
        expected_facts = source.content_facts()
        borrow = self._borrow()

        def fail_after_first_chunk(
            candidate: object,
            chunks: object,
            *,
            maximum_bytes: int,
        ) -> object:
            del candidate, maximum_bytes
            first = next(iter(chunks))  # type: ignore[arg-type]
            self.assertTrue(first)
            raise RuntimeError("consumer interrupted exact stream")

        with mock.patch.object(
            tm_stage_sealer.CandidateFile,
            "write_chunks",
            new=fail_after_first_chunk,
        ), self.assertRaisesRegex(RuntimeError, "consumer interrupted"):
            borrow.copy_asset_to_new_candidate(
                platform=self.reservation._backend,
                parent=self.reservation._root,
                asset="database",
                candidate_name=candidate_name,
            )

        self.assertIsNone(source._content_facts_cache)
        acquired = source._read_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        if acquired:
            source._read_lock.release()

        results: dict[str, object] = {}
        failures: list[BaseException] = []

        def fresh_read() -> None:
            try:
                with mock.patch.object(
                    source._api,
                    "ReadFile",
                    wraps=source._api.ReadFile,
                ) as read_file:
                    results["facts"] = source.content_facts()
                    results["body_reads"] = read_file.call_count
                    results["prefix"] = source.read_at(0, 31, expected_snapshot)
            except BaseException as error:
                failures.append(error)

        reader = threading.Thread(target=fresh_read)
        reader.start()
        reader.join(5.0)
        self.assertFalse(reader.is_alive(), "interrupted stream retained its read lock")
        self.assertEqual(failures, [])
        self.assertEqual(results["facts"], expected_facts)
        self.assertGreater(results["body_reads"], 0)
        self.assertEqual(
            results["prefix"],
            self.stage.staged_db_path.read_bytes()[:31],
        )
        candidate_path = self.root / candidate_name
        if candidate_path.exists():
            candidate_path.unlink()

    def test_bulk_copy_tamper_is_not_accepted_or_published(self) -> None:
        candidate_name = "publication-tampered.candidate"
        entry = self.registry._entries[self.sealed.artifact.artifact_id]
        live_authority = entry.live_authority
        if live_authority is None:
            raise AssertionError("expected portable live authority")
        source = live_authority._PortableStageLiveAuthority__database
        real_stream = BoundRegularFile.iter_exact_chunks

        def tampered_stream(bound: object, expected: object) -> object:
            for ordinal, chunk in enumerate(real_stream(bound, expected)):
                if ordinal == 0:
                    yield bytes((chunk[0] ^ 1,)) + chunk[1:]
                else:
                    yield chunk

        with mock.patch.object(
            BoundRegularFile,
            "iter_exact_chunks",
            new=tampered_stream,
        ):
            with self.assertRaises(tm_stage_sealer.StageSealError) as caught:
                self._copy("database", candidate_name)
        self.assertEqual(
            caught.exception.error_code,
            "SEALER.ARTIFACT_MUTATED",
        )
        self.assertEqual(self.registry._portable_borrows, {})
        candidate_path = self.root / candidate_name
        if candidate_path.exists():
            candidate_path.unlink()

    def test_wrong_asset_candidate_and_owner_are_rejected(self) -> None:
        borrow = self._borrow()
        with self.assertRaisesRegex(ValueError, "asset is unsupported"):
            borrow.copy_asset_to_new_candidate(
                platform=self.reservation._backend,
                parent=self.reservation._root,
                asset="source",
                candidate_name="wrong-asset.candidate",
            )

        valid_name = "after-wrong-asset.candidate"
        candidate, _facts = borrow.copy_asset_to_new_candidate(
            platform=self.reservation._backend,
            parent=self.reservation._root,
            asset="manifest",
            candidate_name=valid_name,
        )
        self._close_and_unlink(candidate, valid_name)

        foreign_platform = type(self.reservation._backend)()
        with self.assertRaisesRegex(
            tm_stage_sealer.StageSealError,
            "SEALER.RESERVATION_MISMATCH",
        ):
            self._borrow().copy_asset_to_new_candidate(
                platform=foreign_platform,
                parent=self.reservation._root,
                asset="manifest",
                candidate_name="wrong-platform.candidate",
            )

        foreign_parent = self.reservation._backend.bind_root(self.root)
        try:
            with self.assertRaisesRegex(
                tm_stage_sealer.StageSealError,
                "SEALER.RESERVATION_MISMATCH",
            ):
                self._borrow().copy_asset_to_new_candidate(
                    platform=self.reservation._backend,
                    parent=foreign_parent,
                    asset="manifest",
                    candidate_name="wrong-owner.candidate",
                )
        finally:
            foreign_parent.close()

        collision_name = "foreign-candidate.candidate"
        collision = self.reservation._root.create_candidate(
            collision_name,
            private=False,
        )
        collision.write_all(b"foreign")
        collision.flush_content()
        collision_identity = collision.identity()
        try:
            with self.assertRaisesRegex(
                tm_stage_sealer.StageSealError,
                "SEALER.ATTESTATION_UNAVAILABLE",
            ):
                self._borrow().copy_asset_to_new_candidate(
                    platform=self.reservation._backend,
                    parent=self.reservation._root,
                    asset="database",
                    candidate_name=collision_name,
                )
            self.assertEqual(collision.identity(), collision_identity)
        finally:
            collision.close()
            self.assertEqual(
                (self.root / collision_name).read_bytes(),
                b"foreign",
            )
            self.reservation._root.unlink_owned(
                collision_name,
                collision_identity,
            )


if __name__ == "__main__":
    unittest.main()
