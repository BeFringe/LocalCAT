"""Windows tests for sealed-stage exact copy into owner publication candidates."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import tm_stage_sealer
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
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

    def test_mid_copy_source_mutation_fails_closed(self) -> None:
        candidate_name = "publication-mutated.candidate"
        entry = self.registry._entries[self.sealed.artifact.artifact_id]
        live_authority = entry.live_authority
        if live_authority is None:
            raise AssertionError("expected portable live authority")
        source = live_authority._PortableStageLiveAuthority__database
        source_type = type(source)
        real_read_at = source_type.read_at
        real_write_chunks = tm_stage_sealer.CandidateFile.write_chunks
        copying = False
        copy_reads = 0

        def track_copy(
            candidate: object,
            chunks: object,
            *,
            maximum_bytes: int,
        ) -> object:
            nonlocal copying
            copying = True
            try:
                return real_write_chunks(
                    candidate,
                    chunks,
                    maximum_bytes=maximum_bytes,
                )
            finally:
                copying = False

        def become_stale_after_first_chunk(
            bound: object,
            offset: int,
            maximum_bytes: int,
            expected: object,
        ) -> bytes:
            nonlocal copy_reads
            if copying and bound is source:
                copy_reads += 1
                if copy_reads == 2:
                    raise PlatformFileError(
                        PlatformFileErrorCode.IDENTITY_STALE,
                        retryable=True,
                    )
            return real_read_at(bound, offset, maximum_bytes, expected)

        with (
            mock.patch.object(
                tm_stage_sealer.CandidateFile,
                "write_chunks",
                new=track_copy,
            ),
            mock.patch.object(
                source_type,
                "read_at",
                new=become_stale_after_first_chunk,
            ),
        ):
            with self.assertRaises(tm_stage_sealer.StageSealError) as caught:
                self._copy("database", candidate_name)
        self.assertIn(
            caught.exception.error_code,
            {
                "SEALER.ARTIFACT_MUTATED",
                "SEALER.ATTESTATION_UNAVAILABLE",
            },
        )
        self.assertEqual(copy_reads, 2)
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
