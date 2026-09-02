"""Independent Windows worker for portable schema-upgrade crash tests."""

from __future__ import annotations

import argparse
from pathlib import Path
import time
from unittest import mock

import tm_activation_recovery
from tm_contracts import CanonicalResourceIdentity
from tm_migration import TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--marker", type=Path, required=True)
    arguments = parser.parse_args()

    source = (arguments.root / "tm.primary.jsonl").resolve()
    identity = CanonicalResourceIdentity.from_configured_jsonl("tm.primary", source)
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
        coordinator=coordinator,
    )
    real_publish = tm_activation_recovery._PortableReplacementRecordOwner.publish

    def publish_and_pause(**kwargs: object) -> object:
        record = real_publish(**kwargs)
        unsigned = kwargs["unsigned"]
        if unsigned.phase == arguments.phase:
            arguments.marker.write_text(arguments.phase, encoding="ascii")
            while True:
                time.sleep(1.0)
        return record

    with mock.patch.object(
        tm_activation_recovery._PortableReplacementRecordOwner,
        "publish",
        side_effect=publish_and_pause,
    ):
        service.upgrade_schema(identity.canonical_sidecar_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
