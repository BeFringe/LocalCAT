from __future__ import annotations

from dataclasses import replace
import hashlib
import unittest

from resource_package_contracts import (
    CARRIER_PROFILE,
    MANIFEST_SCHEMA,
    PAYLOAD_PROFILE_SET,
    RECEIPT_SCHEMA,
    PortableResourceKind,
    ResourceDurableState,
    ResourceOperationKind,
    ResourceOperationReceipt,
    ResourcePackageManifest,
    ResourcePayloadDescriptor,
    ResourcePayloadProfile,
    ResourcePortabilityError,
    ResourceProfileCounts,
    manifest_from_bytes,
    manifest_to_bytes,
    receipt_from_bytes,
    receipt_to_bytes,
)


_DIGEST = hashlib.sha256(b"payload").hexdigest()


def _manifest() -> ResourcePackageManifest:
    return ResourcePackageManifest(
        schema=MANIFEST_SCHEMA,
        carrier_profile=CARRIER_PROFILE,
        payload_profile_set=PAYLOAD_PROFILE_SET,
        resource_kind=PortableResourceKind.TRANSLATION_MEMORY,
        payload_profile=ResourcePayloadProfile.TM_JSONL_V1,
        payload=ResourcePayloadDescriptor(
            path="payload/tm.jsonl",
            sha256=_DIGEST,
            byte_count=7,
            record_count=1,
        ),
        profile_counts=ResourceProfileCounts(0, 0),
    )


class ResourcePackageContractTests(unittest.TestCase):
    def test_manifest_codec_is_exact_and_canonical(self) -> None:
        manifest = _manifest()
        encoded = manifest_to_bytes(manifest)
        self.assertEqual(manifest_from_bytes(encoded), manifest)
        self.assertTrue(encoded.endswith(b"\n"))

        duplicate = encoded.replace(
            b'"schema":',
            b'"schema":"localcat-resource-package-manifest-v1","schema":',
            1,
        )
        with self.assertRaises(ResourcePortabilityError) as caught:
            manifest_from_bytes(duplicate)
        self.assertEqual(caught.exception.code, "RESOURCE.PACKAGE.MANIFEST_INVALID")

    def test_profile_kind_and_counts_fail_closed(self) -> None:
        manifest = _manifest()
        with self.assertRaises(ValueError):
            replace(
                manifest,
                resource_kind=PortableResourceKind.TERMBASE,
            )
        with self.assertRaises(TypeError):
            ResourcePayloadDescriptor(
                path="payload/tm.jsonl",
                sha256=_DIGEST,
                byte_count=True,
                record_count=1,
            )

    def test_receipt_codec_uses_receipt_error_domain(self) -> None:
        receipt = ResourceOperationReceipt(
            receipt_schema=RECEIPT_SCHEMA,
            operation_id="op-1",
            operation_kind=ResourceOperationKind.EXPORT_PACKAGE,
            resource_kind=PortableResourceKind.TRANSLATION_MEMORY,
            payload_profile=ResourcePayloadProfile.TM_JSONL_V1,
            source_resource_id="tm-1",
            destination_resource_id=None,
            package_artifact_digest=_DIGEST,
            payload_digest=_DIGEST,
            destination_before_digest=None,
            destination_after_digest=_DIGEST,
            record_count=1,
            legacy_record_count=0,
            v1_record_count=0,
            skipped_count=0,
            safe_warnings=(),
            durable_state=ResourceDurableState.COMMITTED,
        )
        encoded = receipt_to_bytes(receipt)
        self.assertEqual(receipt_from_bytes(encoded), receipt)
        malformed = encoded.replace(b'"operation_id":', b'"extra":0,"operation_id":', 1)
        with self.assertRaises(ResourcePortabilityError) as caught:
            receipt_from_bytes(malformed)
        self.assertEqual(caught.exception.code, "RESOURCE.RECEIPT.INVALID")


if __name__ == "__main__":
    unittest.main()
