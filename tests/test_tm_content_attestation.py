from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from typing import cast

from tm_content_attestation import (
    ContentSemanticFacts,
    ContentAttestationError,
    _active_content_attestation_from_mapping,
    _active_content_attestation_to_mapping,
    _capture_content_file,
    _content_file_proof_from_mapping,
    _content_file_proof_to_mapping,
    _create_active_content_attestation,
    _create_sealed_content_attestation,
    _sealed_content_attestation_from_mapping,
    _sealed_content_attestation_to_mapping,
)


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
            record_count=1,
            origin_batch_count=1,
            origin_batch_id="migration." + "2" * 64,
            origin_batch_kind="migration",
            exported_revision=1,
            fts_count=1,
            gram_counts=((1, 1), (2, 0)),
            exact_parity_digest="3" * 64,
            logical_closure_digest="4" * 64,
        )

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
                        "record_count": 2,
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


if __name__ == "__main__":
    unittest.main()
