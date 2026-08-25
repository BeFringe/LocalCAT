from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

from resource_package import open_resource_package, write_resource_package
from resource_package_contracts import (
    CARRIER_PROFILE,
    MANIFEST_SCHEMA,
    PAYLOAD_PROFILE_SET,
    PortableResourceKind,
    ResourcePackageManifest,
    ResourcePayloadDescriptor,
    ResourcePayloadProfile,
    ResourcePortabilityError,
    ResourceProfileCounts,
)


class ResourcePackageCarrierTests(unittest.TestCase):
    def _manifest(self, payload: bytes) -> ResourcePackageManifest:
        return ResourcePackageManifest(
            schema=MANIFEST_SCHEMA,
            carrier_profile=CARRIER_PROFILE,
            payload_profile_set=PAYLOAD_PROFILE_SET,
            resource_kind=PortableResourceKind.TRANSLATION_MEMORY,
            payload_profile=ResourcePayloadProfile.TM_JSONL_V1,
            payload=ResourcePayloadDescriptor(
                path="payload/tm.jsonl",
                sha256=hashlib.sha256(payload).hexdigest(),
                byte_count=len(payload),
                record_count=2,
            ),
            profile_counts=ResourceProfileCounts(0, 0),
        )

    def test_deterministic_write_open_and_payload_copy(self) -> None:
        payload = b'{"source":"a","target":"b"}\n{"source":"c","target":"d"}\n'
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "tm.jsonl"
            first = root / "first.localcat-resource"
            second = root / "second.localcat-resource"
            copied = root / "copied.jsonl"
            source.write_bytes(payload)
            manifest = self._manifest(payload)
            first_report = write_resource_package(first, manifest, source)
            second_report = write_resource_package(second, manifest, source)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_report.artifact_digest, second_report.artifact_digest)
            with open_resource_package(first) as sealed:
                self.assertEqual(sealed.manifest, manifest)
                self.assertEqual(sealed.validation.payload_digest, manifest.payload.sha256)
                sealed.copy_payload_to(copied)
                artifact = sealed.transfer_artifact()
            with artifact:
                self.assertEqual(
                    artifact.metadata.artifact_sha256,
                    first_report.artifact_digest,
                )
                self.assertFalse(hasattr(artifact, "path"))
                with artifact.open_bounded_stream() as stream:
                    self.assertEqual(stream.read(), first.read_bytes())
            self.assertEqual(copied.read_bytes(), payload)

    def test_trailing_bytes_and_source_symlink_are_rejected(self) -> None:
        payload = b"{}\n"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "tm.jsonl"
            package = root / "package.localcat-resource"
            source.write_bytes(payload)
            write_resource_package(package, self._manifest(payload), source)
            package.write_bytes(package.read_bytes() + b"tail")
            with self.assertRaises(ResourcePortabilityError):
                open_resource_package(package)

            clean = root / "clean.localcat-resource"
            write_resource_package(clean, self._manifest(payload), source)
            alias = root / "alias.localcat-resource"
            alias.symlink_to(clean)
            with self.assertRaises(ResourcePortabilityError) as caught:
                open_resource_package(alias)
            self.assertEqual(caught.exception.code, "RESOURCE.PACKAGE.SOURCE_UNSAFE")

    def test_payload_drift_blocks_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "tm.jsonl"
            destination = root / "package.localcat-resource"
            source.write_bytes(b"old\n")
            manifest = self._manifest(b"new\n")
            with self.assertRaises(ResourcePortabilityError):
                write_resource_package(destination, manifest, source)
            self.assertFalse(destination.exists())

    def test_raw_profile_rejects_header_flag_method_crc_extra_and_prefix_mutations(self) -> None:
        payload = b"{}\n"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "tm.jsonl"
            original = root / "original.localcat-resource"
            source.write_bytes(payload)
            write_resource_package(original, self._manifest(payload), source)
            clean = original.read_bytes()
            central = clean.index(b"PK\x01\x02")
            mutations: dict[str, bytes] = {}

            changed = bytearray(clean)
            struct.pack_into("<H", changed, 6, 0x0008)
            mutations["local-data-descriptor"] = bytes(changed)

            changed = bytearray(clean)
            struct.pack_into("<H", changed, central + 8, 0x0008)
            mutations["central-data-descriptor"] = bytes(changed)

            changed = bytearray(clean)
            struct.pack_into("<H", changed, 8, 8)
            mutations["compressed-local"] = bytes(changed)

            changed = bytearray(clean)
            struct.pack_into("<I", changed, central + 16, 0)
            mutations["central-crc"] = bytes(changed)

            changed = bytearray(clean)
            struct.pack_into("<H", changed, 28, 1)
            mutations["local-extra"] = bytes(changed)
            mutations["prefix"] = b"prefix" + clean

            for name, data in mutations.items():
                with self.subTest(name=name):
                    candidate = root / f"{name}.localcat-resource"
                    candidate.write_bytes(data)
                    with self.assertRaises(ResourcePortabilityError):
                        open_resource_package(candidate)


if __name__ == "__main__":
    unittest.main()
