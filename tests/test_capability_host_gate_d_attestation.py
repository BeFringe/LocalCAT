from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import capability_host
import tm_benchmark_gate
from capability_host import GateDRunState
from tests.test_capability_host_gate_d import _EVALUATED_AT, _gate_c
from tests.test_tm_benchmark_gate import _combined_bundle


ROOT = Path(__file__).resolve().parents[1]


class CapabilityHostGateDAttestationTests(unittest.TestCase):
    @staticmethod
    def _run_result():
        bundle = _combined_bundle(fts5_missing=0, fallback_missing=0)
        artifact = tm_benchmark_gate.benchmark_evidence_bundle_to_json(
            bundle
        ).encode("utf-8")
        return tm_benchmark_gate._issue_benchmark_gate_d_run_result(
            bundle=bundle,
            bundle_digest=bundle.bundle_digest,
            artifact_size=len(artifact),
            artifact_digest=hashlib.sha256(artifact).hexdigest(),
            test_mode=False,
        )

    def test_second_composition_restores_without_running_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = (Path(temporary) / "gate-d").resolve()
            first = capability_host.compose_capability_host(
                evaluated_at_utc=_EVALUATED_AT,
                gate_d_attestation_root=state_root,
            )
            _gate_c(first)
            owner_identity = cast(
                Any,
                first.host,
            )._CapabilityHost__retrieval_owner_identity
            graph = cast(Any, first.host)._capture_gate_d_graph(
                owner_identity=owner_identity,
            )
            self.assertIsNotNone(graph)
            assert graph is not None
            tm_benchmark_gate._persist_gate_d_attestation(
                contract_path=(ROOT / "benchmark_tm_contract.json").resolve(),
                state_root=state_root,
                base_manifest=graph.base_manifest,
                run_result=self._run_result(),
                issued_at_utc=_EVALUATED_AT,
            )

            second = capability_host.compose_capability_host(
                evaluated_at_utc=_EVALUATED_AT,
                gate_d_attestation_root=state_root,
            )
            gate_c = _gate_c(second)
            self.assertTrue(gate_c.display.context_available)
            self.assertFalse(gate_c.display.fuzzy_available)
            owner = second.retrieval_gate_d_owner
            assert owner is not None
            with patch.object(
                cast(Any, owner),
                "_RetrievalGateDOwner__execute",
                side_effect=AssertionError("restore must not execute Gate D"),
            ):
                status = owner.restore_gate_d(evaluated_at_utc=_EVALUATED_AT)

            self.assertIs(status.state, GateDRunState.SUCCEEDED)
            restored = second.host.retrieval_operation_snapshot()
            self.assertTrue(restored.display.context_available)
            self.assertTrue(restored.display.fuzzy_available)

    def test_explicit_real_run_path_persists_for_next_composition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = (Path(temporary) / "gate-d").resolve()
            first = capability_host.compose_capability_host(
                evaluated_at_utc=_EVALUATED_AT,
                gate_d_attestation_root=state_root,
            )
            _gate_c(first)
            host = cast(Any, first.host)
            owner_identity = host._CapabilityHost__retrieval_owner_identity
            binding = cast(Any, capability_host)._CoreGateDBinding.capture()

            def execute(**kwargs: object):
                return cast(Any, capability_host)._CoreGateDPublication(
                    _mint=cast(Any, capability_host)._GATE_D_PUBLICATION_MINT,
                    binding=binding,
                    run_result=self._run_result(),
                    publication_owner_identity=kwargs[
                        "publication_owner_identity"
                    ],
                    publication_graph_nonce=kwargs[
                        "publication_graph_nonce"
                    ],
                )

            owner = cast(Any, first.retrieval_gate_d_owner)
            with patch.object(
                owner,
                "_RetrievalGateDOwner__execute",
                side_effect=execute,
            ):
                started = owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
                completed = owner.wait(timeout=10.0)
            self.assertIs(started.state, GateDRunState.RUNNING)
            self.assertIs(completed.state, GateDRunState.SUCCEEDED)
            self.assertTrue((state_root / "qualification.json").is_file())

            second = capability_host.compose_capability_host(
                evaluated_at_utc=_EVALUATED_AT,
                gate_d_attestation_root=state_root,
            )
            _gate_c(second)
            second_owner = second.retrieval_gate_d_owner
            assert second_owner is not None
            restored = second_owner.restore_gate_d(
                evaluated_at_utc=_EVALUATED_AT
            )
            self.assertIs(restored.state, GateDRunState.SUCCEEDED)
            self.assertTrue(
                second.host.retrieval_operation_snapshot().display.fuzzy_available
            )


if __name__ == "__main__":
    unittest.main()
