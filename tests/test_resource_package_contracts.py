from __future__ import annotations

from dataclasses import replace
import hashlib
import unittest

from resource_package_contracts import (
    CARRIER_PROFILE,
    CARRIER_PROFILE_V2,
    LIMIT_PROFILE,
    LIMIT_PROFILE_V2,
    MANIFEST_SCHEMA,
    MANIFEST_SCHEMA_V2,
    PAYLOAD_PROFILE_SET,
    PAYLOAD_PROFILE_SET_V2,
    RECEIPT_SCHEMA,
    PortableResourceKind,
    ResourceDurableState,
    ResourceOperationKind,
    ResourceOperationReceipt,
    ResourcePackageManifest,
    ResourcePackageLimitProfile,
    ResourcePackageSourceScope,
    ResourcePayloadDescriptor,
    ResourcePayloadProfile,
    ResourcePortabilityError,
    ResourceProfileCounts,
    manifest_from_bytes,
    manifest_to_bytes,
    package_profile_triple_for_payload,
    resource_package_capability,
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

    def test_v1_manifest_bytes_and_default_limit_profile_remain_exact(self) -> None:
        encoded = manifest_to_bytes(_manifest())
        self.assertEqual(
            encoded,
            (
                b'{"schema":"localcat-resource-package-manifest-v1",'
                b'"carrier_profile":"localcat-resource-package-zip-v1",'
                b'"payload_profile_set":"localcat-resource-payload-set-v1",'
                b'"resource":{"kind":"translation_memory",'
                b'"payload_profile":"localcat-tm-jsonl-v1",'
                b'"payload":{"path":"payload/tm.jsonl",'
                b'"sha256":"' + _DIGEST.encode("ascii") + b'",'
                b'"byte_count":7,"record_count":1},'
                b'"profile_counts":{"legacy_record_count":0,'
                b'"v1_record_count":0}}}\n'
            ),
        )
        self.assertEqual(ResourcePackageLimitProfile().profile, LIMIT_PROFILE)

    def test_tmx_profile_requires_new_exact_triple_and_independent_limits(self) -> None:
        profile = ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1
        self.assertEqual(
            package_profile_triple_for_payload(profile),
            (MANIFEST_SCHEMA_V2, CARRIER_PROFILE_V2, PAYLOAD_PROFILE_SET_V2),
        )
        manifest = ResourcePackageManifest(
            schema=MANIFEST_SCHEMA_V2,
            carrier_profile=CARRIER_PROFILE_V2,
            payload_profile_set=PAYLOAD_PROFILE_SET_V2,
            resource_kind=PortableResourceKind.TRANSLATION_MEMORY,
            payload_profile=profile,
            payload=ResourcePayloadDescriptor(
                path="payload/resource.tmx",
                sha256=_DIGEST,
                byte_count=7,
                record_count=1,
            ),
            profile_counts=ResourceProfileCounts(0, 0),
        )
        self.assertEqual(manifest_from_bytes(manifest_to_bytes(manifest)), manifest)
        self.assertEqual(
            ResourcePackageLimitProfile(profile=LIMIT_PROFILE_V2).profile,
            LIMIT_PROFILE_V2,
        )
        with self.assertRaises(ValueError):
            replace(manifest, schema=MANIFEST_SCHEMA)
        with self.assertRaises(ValueError):
            replace(manifest, carrier_profile=CARRIER_PROFILE)
        with self.assertRaises(ValueError):
            replace(manifest, payload_profile_set=PAYLOAD_PROFILE_SET)

    def test_capability_matrix_is_managed_resource_export_only_for_tmx(self) -> None:
        profile = ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1
        self.assertTrue(
            resource_package_capability(
                ResourcePackageSourceScope.MANAGED_RESOURCE,
                PortableResourceKind.TRANSLATION_MEMORY,
                profile,
                importing=False,
            )
        )
        for scope in (
            ResourcePackageSourceScope.ENTIRE_PROJECT,
            ResourcePackageSourceScope.SELECTED_CHUNK,
        ):
            self.assertFalse(
                resource_package_capability(
                    scope,
                    PortableResourceKind.TRANSLATION_MEMORY,
                    profile,
                    importing=False,
                )
            )
        self.assertFalse(
            resource_package_capability(
                ResourcePackageSourceScope.MANAGED_RESOURCE,
                PortableResourceKind.TERMBASE,
                profile,
                importing=False,
            )
        )
        self.assertFalse(
            resource_package_capability(
                ResourcePackageSourceScope.MANAGED_RESOURCE,
                PortableResourceKind.TRANSLATION_MEMORY,
                profile,
                importing=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
