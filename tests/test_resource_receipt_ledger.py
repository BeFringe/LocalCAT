from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from resource_package_contracts import (
    RECEIPT_SCHEMA,
    PortableResourceKind,
    ResourceDurableState,
    ResourceOperationKind,
    ResourceOperationReceipt,
    ResourcePayloadProfile,
    ResourcePortabilityError,
)
from resource_receipt_ledger import ResourceReceiptLedger


_DIGEST = hashlib.sha256(b"payload").hexdigest()


def _receipt() -> ResourceOperationReceipt:
    return ResourceOperationReceipt(
        receipt_schema=RECEIPT_SCHEMA,
        operation_id="operation-1",
        operation_kind=ResourceOperationKind.EXPORT_DIRECT,
        resource_kind=PortableResourceKind.TRANSLATION_MEMORY,
        payload_profile=ResourcePayloadProfile.TM_JSONL_V1,
        source_resource_id="tm-1",
        destination_resource_id=None,
        package_artifact_digest=None,
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


class ResourceReceiptLedgerTests(unittest.TestCase):
    def test_append_is_durable_idempotent_and_cold_readable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            first = ResourceReceiptLedger(root)
            path = first.append(_receipt())
            self.assertEqual(first.append(_receipt()), path)
            cold = ResourceReceiptLedger(root)
            self.assertEqual(cold.get("operation-1"), _receipt())
            self.assertEqual(cold.list_receipts(), (_receipt(),))

    def test_tampered_existing_receipt_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            ledger = ResourceReceiptLedger(root)
            path = ledger.append(_receipt())
            path.write_bytes(b"{}")
            with self.assertRaises(ResourcePortabilityError) as caught:
                ledger.append(_receipt())
            self.assertEqual(caught.exception.code, "RESOURCE.RECEIPT.LEDGER_FAILED")
            self.assertEqual(path.read_bytes(), b"{}")


if __name__ == "__main__":
    unittest.main()
