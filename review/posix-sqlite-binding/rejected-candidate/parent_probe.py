"""独立评审：在实际 Core builder 入口置换父目录，随后继续实际实现。"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))

from tests.test_tm_resource_snapshot_posix import TMResourceSnapshotPosixTests
from tm_migration import TMMigrationService, _InitialActivationResourceReservation
from tm_contracts import CanonicalResourceIdentity
from tm_engine import TMEngine
from tm_resource_port import TMResourceSnapshotPort
import tm_migration


def run() -> None:
    case = TMResourceSnapshotPosixTests()
    case.setUp()
    try:
        resource = case._active_resource(case.repository, "old", "旧")
        port = TMResourceSnapshotPort(case.repository.platform_backend)
        validated = port.validate_snapshot(case.payload)
        parent = resource.path.parent
        moved = parent.with_name("held-resources")
        real_build = TMMigrationService._build_stage
        real_initialize = tm_migration.initialize_stage_schema
        observations = []
        identity = CanonicalResourceIdentity.from_configured_jsonl(resource.id, resource.path)
        canonical_before = hashlib.sha256(identity.canonical_sidecar_path.read_bytes()).hexdigest()
        outcome_type = None
        real_reprove = _InitialActivationResourceReservation.reprove

        def reprove(reservation):
            if moved.exists():
                try:
                    real_reprove(reservation)
                except Exception as error:
                    observations.append({"phase": "reservation_reprove_failed", "type": type(error).__name__})
                    raise
                observations.append({"phase": "reservation_reprove_passed"})
                return
            return real_reprove(reservation)

        def shifted_build(service, *args, **kwargs):
            case._assert_locked(resource)
            parent.rename(moved)
            # 非协作外部替换，同字节克隆应仍无法继承旧根与 W1。
            shutil.copytree(moved, parent)
            observations.append({"phase": "parent_replaced"})
            with _InitialActivationResourceReservation.acquire_with_backend(identity, case.repository.platform_backend):
                observations.append({"phase": "second_owner_acquired_w1_at_configured_path"})
            return real_build(service, *args, **kwargs)

        def initialize(stage, *args, **kwargs):
            result = real_initialize(stage, *args, **kwargs)
            observations.append({
                "phase": "stage_created",
                "under_replacement_parent": stage.staged_db_path.parent == parent,
                "exists": stage.staged_db_path.exists(),
            })
            return result

        try:
            with (
                patch.object(TMMigrationService, "_build_stage", new=shifted_build),
                patch.object(tm_migration, "initialize_stage_schema", new=initialize),
                patch.object(_InitialActivationResourceReservation, "reprove", new=reprove),
            ):
                port.replace_snapshot(resource, case.payload, validated)
        except Exception as error:
            outcome_type = "failure"
            observations.append({"phase": "outcome", "type": type(error).__name__, "code": getattr(error, "code", None)})
        else:
            outcome_type = "success"
            observations.append({"phase": "outcome", "type": "success"})
        observations.append({
            "phase": "replacement_residue",
            "names": sorted(p.name for p in parent.iterdir()),
        })
        observations.append({
            "phase": "canonical_comparison",
            "held_canonical_unchanged": hashlib.sha256((moved / identity.canonical_sidecar_path.name).read_bytes()).hexdigest() == canonical_before,
            "named_canonical_changed": hashlib.sha256(identity.canonical_sidecar_path.read_bytes()).hexdigest() != canonical_before,
            "named_generation": TMEngine(str(resource.path), expected_resource_id=resource.id).canonical_store.canonical_revision().generation,
        })
        print(json.dumps(observations, ensure_ascii=False, indent=2))
        assert outcome_type == "failure", "父目录置换后不得签发成功结果"
    finally:
        case.doCleanups()


if __name__ == "__main__":
    run()
