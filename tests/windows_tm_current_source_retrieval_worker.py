"""Fresh-process Windows current-source TM retrieval worker."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from capability_host import compose_capability_host
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationFailure,
    MigrationReport,
    QueryReport,
    TMQuery,
    TMResourceHandle,
)


_SOURCE_BYTES = (
    b'{"source":"same","target":"context target","speaker":"alice",'
    b'"context_prev":"before","context_next":"after"}\n'
    b'{"source":"same","target":"exact target"}\n'
    b'{"source":"Open the document now","target":"fuzzy target"}\n'
    b'{"source":"unrelated entry","target":"other target"}\n'
)


def _identity(root: Path, *, create_source: bool) -> CanonicalResourceIdentity:
    source = (root / "tm.primary.jsonl").resolve()
    if create_source:
        root.mkdir(parents=True, exist_ok=False)
        source.write_bytes(_SOURCE_BYTES)
    elif not source.is_file():
        raise AssertionError("configured source is missing")
    return CanonicalResourceIdentity.from_configured_jsonl("tm.primary", source)


def _activate(root: Path) -> dict[str, object]:
    from tm_engine import TMEngine
    from tm_migration import TMMigrationService
    from tm_sqlite_store import ResourceStoreCoordinator

    identity = _identity(root, create_source=True)
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
        coordinator=coordinator,
    )
    outcome = service.activate_initial(
        identity.configured_jsonl_path,
        identity.resource_id,
    )
    if type(outcome) is MigrationFailure:
        failure = outcome
        assert isinstance(failure, MigrationFailure)
        raise AssertionError(
            f"activation failed: {failure.stage}:{failure.error_code}"
        )
    if type(outcome) is not MigrationReport:
        raise AssertionError("activation returned an invalid public outcome")
    report = outcome
    assert isinstance(report, MigrationReport)
    engine = TMEngine(
        str(identity.configured_jsonl_path),
        update=False,
        expected_resource_id=identity.resource_id,
    )
    store = engine.canonical_store
    if not engine.canonical_active or store is None:
        raise AssertionError("activation did not publish a canonical store")
    health = store.health()
    return {
        "mode": "activate",
        "pid": os.getpid(),
        "generation": report.activated_generation,
        "record_count": report.snapshot_receipt.record_count,
        "store_health": {
            "generation": health.generation,
            "index_kind": health.index_kind,
            "record_count": health.record_count,
        },
    }


def _query_payload(report: QueryReport) -> dict[str, object]:
    return {
        "results": [
            {
                "match_type": result.match_type.value,
                "matched_source": result.matched_source,
                "record_id": result.record_id,
                "target": result.target,
            }
            for result in report.results
        ],
        "failures": [
            {
                "error_code": failure.error_code,
                "resource_id": failure.resource_id,
                "stage": failure.stage,
            }
            for failure in report.resource_failures
        ],
        "metadata": [
            {
                "context_available": metadata.context_available,
                "context_unavailable_code": metadata.context_unavailable_code,
                "fuzzy_available": metadata.recall.fuzzy_available,
                "fuzzy_unavailable_code": (
                    metadata.recall.fuzzy_unavailable_code
                ),
                "index_kind": metadata.recall.index_kind,
                "scored_count": metadata.scored_count,
            }
            for metadata in report.resource_metadata
        ],
    }


def _query(root: Path) -> dict[str, object]:
    identity = _identity(root, create_source=False)
    evaluated_at = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    composition = compose_capability_host(evaluated_at_utc=evaluated_at)
    gate_c_owner = composition.retrieval_gate_c_validation_owner
    if gate_c_owner is None:
        raise AssertionError("production composition did not expose Gate C")
    gate_c = gate_c_owner.validate_gate_c(
        generated_at_utc=evaluated_at.replace(hour=0),
        valid_until_utc=evaluated_at.replace(hour=0) + timedelta(days=1),
        evaluated_at_utc=evaluated_at,
    )
    gate_c_display = {
        "context_available": gate_c.display.context_available,
        "fuzzy_available": gate_c.display.fuzzy_available,
        "safe_codes": list(gate_c.display.safe_codes),
    }
    if not gate_c.display.context_available:
        raise AssertionError("fresh current-checkout Gate C did not pass")
    if gate_c.display.fuzzy_available:
        raise AssertionError("Gate C must not publish fuzzy availability")

    gate_d_owner = composition.retrieval_gate_d_owner
    if gate_d_owner is None:
        raise AssertionError("production composition did not expose Gate D")
    started = gate_d_owner.start_gate_d(evaluated_at_utc=evaluated_at)
    finished = gate_d_owner.wait(timeout=900.0)
    gate_d_display = composition.host.retrieval_operation_snapshot().display

    from tm_engine import TMEngine
    from tm_sqlite_store import detect_sqlite_runtime

    engine = TMEngine(
        str(identity.configured_jsonl_path),
        update=False,
        expected_resource_id=identity.resource_id,
    )
    store = engine.canonical_store
    if not engine.canonical_active or store is None:
        raise AssertionError("fresh process did not bind the canonical store")
    health = store.health()
    runtime = detect_sqlite_runtime()

    resource = TMResourceHandle(
        resource_id=identity.resource_id,
        store=store,
        active=True,
        lookup=True,
        update=False,
        order=0,
    )
    query_port = composition.host.retrieval_operation_snapshot().query_port

    def run(
        source: str,
        *,
        speaker: str | None = None,
        previous: str | None = None,
        following: str | None = None,
        minimum_similarity: float = 0.6,
    ) -> QueryReport:
        return query_port.query(
            (resource,),
            TMQuery(
                query_source=source,
                speaker_raw=speaker,
                context_prev_raw=previous,
                context_next_raw=following,
                minimum_similarity=minimum_similarity,
                limit=10,
                resource_order=(identity.resource_id,),
            ),
        )

    exact = run("same")
    context = run(
        "same",
        speaker="alice",
        previous="before",
        following="after",
    )
    fuzzy = run("Open the document soon", minimum_similarity=0.6)
    return {
        "mode": "query",
        "pid": os.getpid(),
        "cold_store": {
            "canonical_active": engine.canonical_active,
            "generation": health.generation,
            "index_kind": health.index_kind,
            "record_count": health.record_count,
        },
        "sqlite_runtime": {
            "fts5_available": runtime.fts5_available,
            "sqlite_version": runtime.sqlite_version,
        },
        "gate_c": gate_c_display,
        "gate_d": {
            "display": {
                "context_available": gate_d_display.context_available,
                "fuzzy_available": gate_d_display.fuzzy_available,
                "safe_codes": list(gate_d_display.safe_codes),
            },
            "finished_state": finished.state.value,
            "safe_code": finished.safe_code,
            "started_state": started.state.value,
        },
        "queries": {
            "exact": _query_payload(exact),
            "context": _query_payload(context),
            "fuzzy": _query_payload(fuzzy),
        },
    }


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("activate", "query"))
    parser.add_argument("root", type=Path)
    parsed = parser.parse_args(arguments)
    root = parsed.root.resolve()
    result = _activate(root) if parsed.mode == "activate" else _query(root)
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
