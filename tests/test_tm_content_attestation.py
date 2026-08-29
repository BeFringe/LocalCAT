from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path, PurePath
import pickle
import tempfile
import unittest
from typing import cast

from platform_fs_contracts import (
    BoundDirectoryAuthority,
    BoundRegularFile,
    EntrySnapshot,
    FileObjectIdentity,
    PlatformFileError,
    PlatformFileErrorCode,
    RootedDirectoryAuthority,
)
from tm_content_attestation import (
    ContentFileProof,
    ContentSemanticFacts,
    LEGACY_CONTENT_ATTESTATION_VERSION,
    LOGICAL_CLOSURE_VERSION,
    PORTABLE_CONTENT_ATTESTATION_VERSION,
    ContentAttestationError,
    _active_content_attestation_from_mapping,
    _active_content_attestation_to_mapping,
    _capture_content_file,
    _capture_platform_content_file,
    _content_file_proof_from_mapping,
    _content_file_proof_to_mapping,
    _create_active_content_attestation,
    _create_portable_active_content_attestation,
    _create_portable_sealed_content_attestation,
    _create_sealed_content_attestation,
    _portable_active_content_attestation_from_mapping,
    _portable_active_content_attestation_to_mapping,
    _portable_content_file_proof_from_mapping,
    _portable_content_file_proof_to_mapping,
    _portable_sealed_content_attestation_from_mapping,
    _portable_sealed_content_attestation_to_mapping,
    _active_content_attestation_record_from_mapping,
    _active_content_attestation_record_to_mapping,
    _require_same_content_attestation_version,
    _sealed_content_attestation_record_from_mapping,
    _sealed_content_attestation_record_to_mapping,
    _sealed_content_attestation_from_mapping,
    _sealed_content_attestation_to_mapping,
    PortableContentFileProof,
)


class _FakeRoot(RootedDirectoryAuthority):
    def _close_authority(self) -> None:
        pass

    def _reprove(self) -> None:
        pass

    def _inspect_entry(self, name: str) -> object:
        del name
        raise AssertionError("not used")

    def _observe_ledger_entries(self, lease: object, limits: object) -> object:
        del lease, limits
        raise AssertionError("not used")

    def _create_candidate(self, name: str, *, private: bool) -> object:
        del name, private
        raise AssertionError("not used")

    def _rename_candidate(
        self,
        candidate: object,
        destination: str,
        *,
        mode: object,
        lease: object,
    ) -> object:
        del candidate, destination, mode, lease
        raise AssertionError("not used")

    def _open_published_destination(
        self,
        destination: str,
        preliminary_facts: object,
    ) -> object:
        del destination, preliminary_facts
        raise AssertionError("not used")

    def _create_pending_publication(
        self,
        preliminary_facts: object,
        retained_destination: object,
    ) -> object:
        del preliminary_facts, retained_destination
        raise AssertionError("not used")

    def _unlink_owned(self, name: str, expected: object) -> None:
        del name, expected
        raise AssertionError("not used")


class _FakeWindowsRegular(BoundRegularFile):
    def __init__(self, payload: bytes) -> None:
        super().__init__()
        self.payload = payload
        self._entry_identity = FileObjectIdentity(
            platform="windows",
            volume_id=b"volume-serial",
            file_id=bytes(range(16)),
            kind="regular",
            link_count=1,
        )

    def _close_authority(self) -> None:
        pass

    def _read_at(
        self,
        offset: int,
        maximum_bytes: int,
        expected: EntrySnapshot,
    ) -> bytes:
        if expected != self._snapshot():
            raise AssertionError("unexpected snapshot")
        return self.payload[offset : offset + maximum_bytes]

    def _identity(self) -> FileObjectIdentity:
        return self._entry_identity

    def _snapshot(self) -> EntrySnapshot:
        return EntrySnapshot(
            self._entry_identity,
            len(self.payload),
            b"stable-write-token",
            True,
        )


class _FakePlatformBackend:
    def __init__(self, payload: bytes) -> None:
        self.root = _FakeRoot()
        self.file = _FakeWindowsRegular(payload)

    def bind_root(self, root: Path) -> RootedDirectoryAuthority:
        if not root.is_absolute():
            raise AssertionError("root must be absolute")
        return self.root

    def open_regular(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundRegularFile:
        if root is not self.root or relative.is_absolute():
            raise AssertionError("capture lost its rooted binding")
        return self.file


class _FailingPlatformBackend(_FakePlatformBackend):
    def __init__(self, code: PlatformFileErrorCode) -> None:
        super().__init__(b"unreachable")
        self.code = code

    def open_regular(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundRegularFile:
        del root, relative
        retryable = self.code is PlatformFileErrorCode.IDENTITY_STALE
        raise PlatformFileError(self.code, retryable=retryable)


class ContentFileProofTests(unittest.TestCase):
    def test_capture_binds_exact_bytes_and_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "artifact.bin"
            path.write_bytes(b"sealed bytes")

            first = _capture_content_file(path)
            path.write_bytes(b"mutated bytes")
            second = _capture_content_file(path)

            self.assertEqual(second.device, first.device)
            self.assertEqual(second.inode, first.inode)
            self.assertNotEqual(second.sha256, first.sha256)

            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(b"sealed bytes")
            os.replace(replacement, path)
            third = _capture_content_file(path)

            self.assertEqual(third.sha256, first.sha256)
            self.assertNotEqual(third.inode, first.inode)

    def test_codec_rejects_extra_missing_and_wrong_exact_types(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "artifact.bin"
            path.write_bytes(b"sealed bytes")
            proof = _capture_content_file(path)
            payload = _content_file_proof_to_mapping(proof)

            self.assertEqual(
                _content_file_proof_from_mapping(payload),
                proof,
            )
            for malformed in (
                {**payload, "extra": "forbidden"},
                {key: value for key, value in payload.items() if key != "inode"},
                {**payload, "device": False},
                {**payload, "sha256": "0" * 63},
            ):
                with self.subTest(malformed=malformed):
                    with self.assertRaises((TypeError, ValueError)):
                        _content_file_proof_from_mapping(malformed)

    def test_portable_codec_roundtrips_without_identity_fields(self) -> None:
        proof = PortableContentFileProof(
            size=12,
            sha256=hashlib.sha256(b"sealed bytes").hexdigest(),
        )
        payload = _portable_content_file_proof_to_mapping(proof)

        self.assertEqual(payload, {"sha256": proof.sha256, "size": 12})
        self.assertEqual(_portable_content_file_proof_from_mapping(payload), proof)
        for malformed in (
            {**payload, "file_id": "not-persistable"},
            {**payload, "device": 1, "inode": 2},
            {"sha256": proof.sha256},
            {**payload, "size": False},
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaises((TypeError, ValueError)):
                    _portable_content_file_proof_from_mapping(malformed)

    def test_platform_capture_retains_windows_identity_only_while_live(self) -> None:
        payload = b"portable exact bytes"
        backend = _FakePlatformBackend(payload)
        capture = _capture_platform_content_file(
            backend,  # type: ignore[arg-type]
            Path.cwd().resolve(),
            PurePath("artifact.bin"),
        )

        proof = capture.persisted_proof()
        self.assertEqual(proof.size, len(payload))
        self.assertEqual(proof.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(capture.live_identity().platform, "windows")
        self.assertEqual(capture.reprove().snapshot.identity.file_id, bytes(range(16)))
        encoded = json.dumps(_portable_content_file_proof_to_mapping(proof))
        self.assertNotIn("file_id", encoded)
        self.assertNotIn("volume_id", encoded)
        self.assertNotIn("device", encoded)
        self.assertNotIn("inode", encoded)
        with self.assertRaises(TypeError):
            pickle.dumps(capture)
        with self.assertRaises(TypeError):
            json.dumps(capture)
        backend.file.payload = b"mutated exact bytes!"
        with self.assertRaisesRegex(
            ContentAttestationError,
            "CONTENT_ATTESTATION.CONTENT_MISMATCH",
        ):
            capture.reprove()

        capture.close()
        self.assertTrue(backend.file.closed)
        self.assertTrue(backend.root.closed)
        with self.assertRaises(PlatformFileError) as raised:
            capture.live_identity()
        self.assertEqual(
            raised.exception.code,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
        )

    def test_platform_capture_maps_failures_to_body_safe_owner_codes(self) -> None:
        expected_codes = {
            PlatformFileErrorCode.ENTRY_UNAVAILABLE: (
                "CONTENT_ATTESTATION.FILE_MISSING"
            ),
            PlatformFileErrorCode.REPARSE_REJECTED: (
                "CONTENT_ATTESTATION.FILE_UNSAFE"
            ),
            PlatformFileErrorCode.IDENTITY_STALE: (
                "CONTENT_ATTESTATION.FILE_MUTATED"
            ),
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE: (
                "CONTENT_ATTESTATION.CAPABILITY_UNAVAILABLE"
            ),
        }
        for platform_code, expected in expected_codes.items():
            with self.subTest(platform_code=platform_code):
                backend = _FailingPlatformBackend(platform_code)
                with self.assertRaises(ContentAttestationError) as raised:
                    _capture_platform_content_file(
                        backend,  # type: ignore[arg-type]
                        Path.cwd().resolve(),
                        PurePath("artifact.bin"),
                    )
                self.assertEqual(raised.exception.error_code, expected)
                self.assertEqual(str(raised.exception), expected)
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(backend.root.closed)

    def test_platform_capture_does_not_normalize_programmer_faults(self) -> None:
        for fault in (TypeError("programmer type"), AssertionError("invariant")):
            with self.subTest(fault=type(fault).__name__):
                backend = _FakePlatformBackend(b"unreachable")

                def fail_open(
                    root: RootedDirectoryAuthority,
                    relative: PurePath,
                ) -> BoundRegularFile:
                    del root, relative
                    raise fault

                backend.open_regular = fail_open  # type: ignore[method-assign]
                with self.assertRaises(type(fault)) as raised:
                    _capture_platform_content_file(
                        backend,  # type: ignore[arg-type]
                        Path.cwd().resolve(),
                        PurePath("artifact.bin"),
                    )
                self.assertIs(raised.exception, fault)
                self.assertTrue(backend.root.closed)

    def test_live_reproof_uses_the_same_platform_error_closure(self) -> None:
        expected_codes = {
            PlatformFileErrorCode.ENTRY_UNAVAILABLE: (
                "CONTENT_ATTESTATION.FILE_MISSING"
            ),
            PlatformFileErrorCode.REPARSE_REJECTED: (
                "CONTENT_ATTESTATION.FILE_UNSAFE"
            ),
            PlatformFileErrorCode.IDENTITY_STALE: (
                "CONTENT_ATTESTATION.FILE_MUTATED"
            ),
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE: (
                "CONTENT_ATTESTATION.CAPABILITY_UNAVAILABLE"
            ),
        }
        for platform_code, expected in expected_codes.items():
            with self.subTest(platform_code=platform_code):
                backend = _FakePlatformBackend(b"captured")
                capture = _capture_platform_content_file(
                    backend,  # type: ignore[arg-type]
                    Path.cwd().resolve(),
                    PurePath("artifact.bin"),
                )

                def fail_content_facts() -> object:
                    retryable = (
                        platform_code is PlatformFileErrorCode.IDENTITY_STALE
                    )
                    raise PlatformFileError(
                        platform_code,
                        retryable=retryable,
                    )

                backend.file._content_facts = (  # type: ignore[method-assign]
                    fail_content_facts
                )
                with self.assertRaises(ContentAttestationError) as raised:
                    capture.reprove()
                self.assertEqual(raised.exception.error_code, expected)
                self.assertIsNone(raised.exception.__cause__)
                capture.close()

    def test_live_reproof_does_not_normalize_programmer_faults(self) -> None:
        for fault in (TypeError("programmer type"), AssertionError("invariant")):
            with self.subTest(fault=type(fault).__name__):
                backend = _FakePlatformBackend(b"captured")
                capture = _capture_platform_content_file(
                    backend,  # type: ignore[arg-type]
                    Path.cwd().resolve(),
                    PurePath("artifact.bin"),
                )

                def fail_content_facts() -> object:
                    raise fault

                backend.file._content_facts = (  # type: ignore[method-assign]
                    fail_content_facts
                )
                with self.assertRaises(type(fault)) as raised:
                    capture.reprove()
                self.assertIs(raised.exception, fault)
                capture.close()

    def test_capture_rejects_hard_links_and_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "artifact.bin"
            path.write_bytes(b"sealed bytes")
            os.link(path, root / "alias.bin")
            with self.assertRaisesRegex(
                ContentAttestationError,
                "CONTENT_ATTESTATION.FILE_UNSAFE",
            ):
                _capture_content_file(path)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            real = root / "real"
            real.mkdir()
            (real / "artifact.bin").write_bytes(b"sealed bytes")
            os.symlink(real, root / "linked")
            with self.assertRaisesRegex(
                ContentAttestationError,
                "CONTENT_ATTESTATION.PARENT_UNSAFE",
            ):
                _capture_content_file(root / "linked" / "artifact.bin")

    def test_capture_distinguishes_missing_leaf_from_unsafe_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(
                ContentAttestationError,
                "CONTENT_ATTESTATION.FILE_MISSING",
            ):
                _capture_content_file(root / "missing.bin")
            with self.assertRaisesRegex(
                ContentAttestationError,
                "CONTENT_ATTESTATION.PARENT_UNSAFE",
            ):
                _capture_content_file(
                    root / "missing-parent" / "artifact.bin"
                )


class ContentAttestationCodecTests(unittest.TestCase):
    def _semantic(self) -> ContentSemanticFacts:
        return ContentSemanticFacts(
            schema_version=2,
            schema_digest="1" * 64,
            fold_version="fold-v1",
            index_version="candidate-v1",
            candidate_index_kind="FTS5_TRIGRAM",
            fts5_available=True,
            sqlite_runtime_version="3.0",
            unicode_runtime_version="15.0",
            journal_mode="delete",
            synchronous="FULL",
            foreign_keys=True,
            busy_timeout_ms=5000,
            wal_enabled=False,
            extension_loading_enabled=False,
            record_count=3,
            receipt_boundary_record_count=1,
            origin_batch_count=1,
            origin_batch_id="migration." + "2" * 64,
            origin_batch_kind="migration",
            exported_revision=1,
            fts_count=3,
            receipt_boundary_fts_count=1,
            gram_counts=((1, 1), (2, 0)),
            exact_parity_digest="3" * 64,
            logical_closure_version=LOGICAL_CLOSURE_VERSION,
            logical_closure_digest="4" * 64,
        )

    def test_semantic_count_domains_are_strict_and_ordered(self) -> None:
        semantic = self._semantic()
        with self.assertRaises(TypeError):
            replace(semantic, receipt_boundary_record_count=False)
        with self.assertRaises(ValueError):
            replace(
                semantic,
                receipt_boundary_record_count=semantic.record_count + 1,
            )
        with self.assertRaises(ValueError):
            replace(
                semantic,
                receipt_boundary_fts_count=(
                    semantic.receipt_boundary_record_count + 1
                ),
            )
        with self.assertRaises(ValueError):
            replace(semantic, fts5_available=False)
        fallback = replace(
            semantic,
            fts5_available=False,
            fts_count=0,
            receipt_boundary_fts_count=0,
        )
        self.assertEqual(fallback.fts_count, 0)
        self.assertEqual(fallback.receipt_boundary_fts_count, 0)

    def test_sealed_and_active_codecs_are_exact_and_digest_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "artifact.bin"
            path.write_bytes(b"attested")
            proof = _capture_content_file(path)
            sealed = _create_sealed_content_attestation(
                resource_id="tm.primary",
                target_identity="5" * 64,
                canonical_store_id="store.primary",
                snapshot_receipt_digest="6" * 64,
                expected_prior_generation=None,
                evidence_digest="7" * 64,
                database=proof,
                manifest=proof,
                source=proof,
                semantic_facts=self._semantic(),
            )
            active = _create_active_content_attestation(
                sealed_attestation_digest=sealed.attestation_digest,
                journal_id="journal.test",
                resource_id="tm.primary",
                target_identity="5" * 64,
                canonical_store_id="store.primary",
                snapshot_receipt_digest="6" * 64,
                generation=0,
                activation_digest="8" * 64,
                database=proof,
                manifest=proof,
                source=proof,
                semantic_facts=self._semantic(),
            )

            sealed_payload = _sealed_content_attestation_to_mapping(sealed)
            active_payload = _active_content_attestation_to_mapping(active)
            self.assertEqual(
                _sealed_content_attestation_from_mapping(sealed_payload),
                sealed,
            )
            self.assertEqual(
                _active_content_attestation_from_mapping(active_payload),
                active,
            )
            active_semantic_payload = cast(
                dict[str, object], active_payload["semantic_facts"]
            )
            malformed_payloads = (
                {**sealed_payload, "extra": "forbidden"},
                {key: value for key, value in active_payload.items() if key != "phase"},
                {**active_payload, "generation": False},
                {**active_payload, "attestation_version": "stale-v0"},
                {**active_payload, "attested_journal_phase": "DB_REPLACED"},
                {**active_payload, "attestation_digest": "0" * 64},
                {
                    **active_payload,
                    "semantic_facts": {
                        **active_semantic_payload,
                        "record_count": 4,
                    },
                },
                {
                    **active_payload,
                    "semantic_facts": {
                        key: value
                        for key, value in active_semantic_payload.items()
                        if key != "receipt_boundary_record_count"
                    },
                },
                {
                    **active_payload,
                    "semantic_facts": {
                        key: value
                        for key, value in active_semantic_payload.items()
                        if key != "receipt_boundary_fts_count"
                    },
                },
                {
                    **active_payload,
                    "semantic_facts": {
                        **active_semantic_payload,
                        "receipt_boundary_record_count": False,
                    },
                },
                {
                    **active_payload,
                    "semantic_facts": {
                        **active_semantic_payload,
                        "receipt_boundary_fts_count": -1,
                    },
                },
                {
                    **active_payload,
                    "semantic_facts": {
                        **active_semantic_payload,
                        "record_count": 1,
                        "receipt_boundary_record_count": 3,
                    },
                },
                {
                    **active_payload,
                    "semantic_facts": {
                        **active_semantic_payload,
                        "receipt_boundary_record_count": 2,
                    },
                },
                {
                    **active_payload,
                    "semantic_facts": {
                        **active_semantic_payload,
                        "logical_closure_digest": "9" * 64,
                    },
                },
            )
            for malformed in malformed_payloads:
                with self.subTest(malformed=malformed):
                    decoder = (
                        _sealed_content_attestation_from_mapping
                        if "evidence_digest" in malformed
                        else _active_content_attestation_from_mapping
                    )
                    with self.assertRaises((TypeError, ValueError)):
                        decoder(malformed)

    def test_portable_v3_sealed_and_active_codecs_are_strict(self) -> None:
        proof = PortableContentFileProof(
            size=8,
            sha256=hashlib.sha256(b"attested").hexdigest(),
        )
        sealed = _create_portable_sealed_content_attestation(
            resource_id="tm.primary",
            target_identity="5" * 64,
            canonical_store_id="store.primary",
            snapshot_receipt_digest="6" * 64,
            expected_prior_generation=None,
            evidence_digest="7" * 64,
            database=proof,
            manifest=proof,
            source=proof,
            semantic_facts=self._semantic(),
        )
        active = _create_portable_active_content_attestation(
            sealed_attestation_digest=sealed.attestation_digest,
            journal_id="journal.test",
            resource_id="tm.primary",
            target_identity="5" * 64,
            canonical_store_id="store.primary",
            snapshot_receipt_digest="6" * 64,
            generation=0,
            activation_digest="8" * 64,
            database=proof,
            manifest=proof,
            source=proof,
            semantic_facts=self._semantic(),
        )

        sealed_payload = _portable_sealed_content_attestation_to_mapping(sealed)
        active_payload = _portable_active_content_attestation_to_mapping(active)
        self.assertEqual(
            sealed_payload["attestation_version"],
            PORTABLE_CONTENT_ATTESTATION_VERSION,
        )
        self.assertEqual(
            set(cast(dict[str, object], sealed_payload["database"])),
            {"sha256", "size"},
        )
        self.assertEqual(
            _portable_sealed_content_attestation_from_mapping(sealed_payload),
            sealed,
        )
        self.assertEqual(
            _portable_active_content_attestation_from_mapping(active_payload),
            active,
        )
        self.assertEqual(
            _sealed_content_attestation_record_from_mapping(sealed_payload),
            sealed,
        )
        self.assertEqual(
            _active_content_attestation_record_from_mapping(active_payload),
            active,
        )
        self.assertEqual(
            _sealed_content_attestation_record_to_mapping(sealed),
            sealed_payload,
        )
        self.assertEqual(
            _active_content_attestation_record_to_mapping(active),
            active_payload,
        )

        legacy_proof = {
            "device": 1,
            "inode": 2,
            "sha256": proof.sha256,
            "size": proof.size,
        }
        mixed_v3 = {**sealed_payload, "database": legacy_proof}
        mixed_active_v3 = {**active_payload, "database": legacy_proof}
        unknown = {**sealed_payload, "attestation_version": "future-v4"}
        with self.assertRaises(ValueError):
            _portable_sealed_content_attestation_from_mapping(mixed_v3)
        with self.assertRaises(ValueError):
            _portable_active_content_attestation_from_mapping(mixed_active_v3)
        with self.assertRaises(ValueError):
            _portable_sealed_content_attestation_from_mapping(unknown)
        with self.assertRaises(ValueError):
            _sealed_content_attestation_record_from_mapping(unknown)
        with self.assertRaises(ValueError):
            _sealed_content_attestation_from_mapping(sealed_payload)
        with self.assertRaises(ValueError):
            _active_content_attestation_from_mapping(active_payload)
        with self.assertRaises(TypeError):
            _sealed_content_attestation_to_mapping(sealed)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _active_content_attestation_to_mapping(active)  # type: ignore[arg-type]

        v2_proof = ContentFileProof(
            device=1,
            inode=2,
            size=6,
            sha256=hashlib.sha256(b"legacy").hexdigest(),
        )
        v2_sealed = _create_sealed_content_attestation(
            resource_id="tm.primary",
            target_identity="5" * 64,
            canonical_store_id="store.primary",
            snapshot_receipt_digest="6" * 64,
            expected_prior_generation=None,
            evidence_digest="7" * 64,
            database=v2_proof,
            manifest=v2_proof,
            source=v2_proof,
            semantic_facts=self._semantic(),
        )
        v2_active = _create_active_content_attestation(
            sealed_attestation_digest=v2_sealed.attestation_digest,
            journal_id="journal.legacy",
            resource_id="tm.primary",
            target_identity="5" * 64,
            canonical_store_id="store.primary",
            snapshot_receipt_digest="6" * 64,
            generation=0,
            activation_digest="8" * 64,
            database=v2_proof,
            manifest=v2_proof,
            source=v2_proof,
            semantic_facts=self._semantic(),
        )
        v2_payload = _sealed_content_attestation_to_mapping(v2_sealed)
        v2_active_payload = _active_content_attestation_to_mapping(v2_active)
        self.assertEqual(
            set(cast(dict[str, object], v2_payload["database"])),
            {"device", "inode", "sha256", "size"},
        )
        self.assertEqual(
            _sealed_content_attestation_from_mapping(v2_payload),
            v2_sealed,
        )
        self.assertEqual(
            _active_content_attestation_from_mapping(v2_active_payload),
            v2_active,
        )
        self.assertEqual(
            _sealed_content_attestation_record_from_mapping(v2_payload),
            v2_sealed,
        )
        self.assertEqual(
            _active_content_attestation_record_from_mapping(v2_active_payload),
            v2_active,
        )
        self.assertEqual(
            _require_same_content_attestation_version(v2_sealed, v2_active),
            v2_sealed.attestation_version,
        )
        self.assertEqual(
            _require_same_content_attestation_version(sealed, active),
            sealed.attestation_version,
        )
        with self.assertRaises(ValueError):
            _require_same_content_attestation_version(v2_sealed, active)
        with self.assertRaises(ValueError):
            _require_same_content_attestation_version(sealed, v2_active)
        forged_cross_family = object.__new__(type(active))
        object.__setattr__(
            forged_cross_family,
            "attestation_version",
            LEGACY_CONTENT_ATTESTATION_VERSION,
        )
        with self.assertRaises(ValueError):
            _require_same_content_attestation_version(
                v2_sealed,
                forged_cross_family,
            )
        with self.assertRaises(ValueError):
            _portable_sealed_content_attestation_from_mapping(v2_payload)
        with self.assertRaises(ValueError):
            _portable_active_content_attestation_from_mapping(v2_active_payload)
        with self.assertRaises(TypeError):
            _portable_sealed_content_attestation_to_mapping(  # type: ignore[arg-type]
                v2_sealed
            )
        with self.assertRaises(TypeError):
            _portable_active_content_attestation_to_mapping(  # type: ignore[arg-type]
                v2_active
            )
        with self.assertRaises(TypeError):
            replace(v2_sealed, database=proof)
        with self.assertRaises(TypeError):
            replace(sealed, database=v2_proof)
        mixed_v2 = {
            **v2_payload,
            "database": _portable_content_file_proof_to_mapping(proof),
        }
        with self.assertRaises(ValueError):
            _portable_sealed_content_attestation_from_mapping(mixed_v2)


if __name__ == "__main__":
    unittest.main()
