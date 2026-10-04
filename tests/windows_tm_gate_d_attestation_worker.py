"""Independent-process Windows Gate D qualification owner worker."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

import tm_benchmark_gate
from tests.gate_d_input_support import issue_source_bound_fixture_result
from tests.test_tm_benchmark_gate import (
    _base_capability_manifest,
    _combined_bundle,
    _time_failure_bundle,
    _capability_publisher,
    _GENERATED_AT,
    _VALID_UNTIL,
    _EVALUATED_AT,
)


CONTRACT_PATH = (WORKSPACE_ROOT / "benchmark_tm_contract.json").resolve()
ISSUED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _run_result(*, time_failures: bool = False) -> tm_benchmark_gate.BenchmarkGateDRunResult:
    bundle = _time_failure_bundle() if time_failures else _combined_bundle(fts5_missing=0, fallback_missing=0)
    artifact = tm_benchmark_gate.benchmark_evidence_bundle_to_json(bundle).encode(
        "utf-8"
    )
    return issue_source_bound_fixture_result(
        bundle=bundle,
        bundle_digest=bundle.bundle_digest,
        artifact_size=len(artifact),
        artifact_digest=hashlib.sha256(artifact).hexdigest(),
        test_mode=False,
    )


def _qualification_facts(state_root: Path) -> dict[str, object]:
    payload = json.loads(
        (state_root / "qualification.json").read_text(encoding="utf-8")
    )
    proof = payload["windows_private_proof"]
    return {
        "artifact_digest": payload["artifact_digest"],
        "bundle_digest": payload["bundle_digest"],
        "private_proof_digest": hashlib.sha256(
            tm_benchmark_gate._gate_d_attestation_canonical_json(proof).encode(
                "utf-8"
            )
        ).hexdigest(),
        "private_proof_fields": sorted(proof),
        "schema_version": payload["schema_version"],
        "security_profile_id": proof["security_profile_id"],
    }


def main(arguments: list[str]) -> int:
    if len(arguments) != 2 or arguments[0] not in {"persist", "restore", "persist-time", "restore-time"}:
        return 2
    state_root = Path(arguments[1]).resolve()
    manifest = _base_capability_manifest()
    try:
        restored = None
        time_failures = arguments[0].endswith("-time")
        if arguments[0].startswith("persist"):
            run_result = _run_result(time_failures=time_failures)
            tm_benchmark_gate._persist_gate_d_attestation(
                contract_path=CONTRACT_PATH,
                state_root=state_root,
                base_manifest=manifest,
                run_result=run_result,
                issued_at_utc=ISSUED_AT,
            )
        else:
            restored = tm_benchmark_gate._restore_gate_d_attestation(
                contract_path=CONTRACT_PATH,
                state_root=state_root,
                base_manifest=manifest,
            )
            run_result = restored
        facts = _qualification_facts(state_root)
        facts["mode"] = arguments[0]
        facts["pid"] = os.getpid()
        if restored is not None:
            facts["restored_artifact_digest"] = restored.artifact_digest
            facts["restored_bundle_digest"] = restored.bundle_digest
        if time_failures:
            published = tm_benchmark_gate.publish_retrieval_capability_gate_d(
                manifest, run_result, _capability_publisher(),
                generated_at_utc=_GENERATED_AT, valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )
            facts["decisions"] = [
                {"available": decision.available, "warnings": decision.performance_warning_codes}
                for decision in (published.snapshot.fts5_trigram, published.snapshot.gram_fallback)
            ]
            facts["performance_passed"] = run_result.bundle.suite_report.passed
        sys.stdout.write(json.dumps(facts, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except tm_benchmark_gate.BenchmarkGateDError as error:
        sys.stdout.write(
            json.dumps(
                {"error_code": error.error_code, "mode": arguments[0], "pid": os.getpid()},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
