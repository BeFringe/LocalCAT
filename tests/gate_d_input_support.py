"""Synthetic Gate D test fixtures, bound to real source input windows.

Only tests import this helper. It supplies no qualification of the synthetic
measurement data and is never used by the production runner or composition.
"""
from pathlib import Path

import tm_benchmark_gate as gate
from tm_benchmark import _benchmark_implementation_fingerprint_from_session
from tm_gate_inputs import _retain_source_input_owner


def issue_source_bound_fixture_result(
    *, bundle: gate.BenchmarkEvidenceBundle, bundle_digest: str,
    artifact_size: int, artifact_digest: str, test_mode: bool,
) -> gate.BenchmarkGateDRunResult:
    owner = _retain_source_input_owner(Path(__file__).absolute().parents[1])
    try:
        with owner.open_session() as session:
            _benchmark_implementation_fingerprint_from_session(session)
            session.terminal_reproof()
            return gate._issue_benchmark_gate_d_run_result(
                bundle=bundle, bundle_digest=bundle_digest,
                artifact_size=artifact_size, artifact_digest=artifact_digest,
                test_mode=test_mode, _input_session=session,
            )
    except BaseException:
        owner.close()
        raise
