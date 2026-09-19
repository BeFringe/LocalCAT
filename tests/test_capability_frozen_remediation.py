"""Adversarial tests for the frozen consumer's own executed helper closure."""
from datetime import timedelta
from importlib.machinery import SourceFileLoader
from unittest import TestCase, mock

import capability_host as host
import capability_frozen_inputs as frozen
from tests.test_capability_host_frozen_inputs import ROOT, NOW, calls, entries, changed_attribute
from tests.test_tm_gate_input_composition import _frozen_entries


class FrozenHelperClosureTests(TestCase):
    def source(self):
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT)
        self.addCleanup(source.close)
        return source

    def test_missing_helper_input_rejects_composition(self):
        data = tuple(item for item in _frozen_entries() if item[0] != "capability_frozen_inputs.py")
        source = frozen._test_frozen_capability_source(data, ROOT)
        self.addCleanup(source.close)
        with self.assertRaises((KeyError, RuntimeError)):
            host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)

    def test_helper_filename_drift_before_composition_is_rejected(self):
        source = self.source()
        function = frozen._FrozenCapabilitySource._matches_module
        with changed_attribute(function, "__code__", function.__code__.replace(co_filename=str(ROOT / "foreign.py"))):
            with self.assertRaises(RuntimeError):
                host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)

    def test_helper_retained_source_must_match_executed_functions(self):
        data = tuple((name, content.replace(b"if source.owner is not self or not source.is_current():", b"if False:")) if name == "capability_frozen_inputs.py" else (name, content) for name, content in _frozen_entries())
        source = frozen._test_frozen_capability_source(data, ROOT)
        self.addCleanup(source.close)
        with self.assertRaises(RuntimeError):
            host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)

    def test_helper_drift_rejects_actual_matcher_gate_c_and_gate_d_consumers(self):
        source = self.source()
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        gate_c = composition.retrieval_gate_c_validation_owner
        gate_d = composition.retrieval_gate_d_owner
        assert gate_c is not None and gate_d is not None
        execution = getattr(gate_d, "_RetrievalGateDOwner__execution")
        # Materialize the actual graph before drift, exercising subsequent reuse.
        execution._capture_binding()
        gate_c.validate_gate_c(generated_at_utc=NOW, valid_until_utc=NOW + timedelta(hours=1), evaluated_at_utc=NOW)
        function = frozen._FrozenCapabilitySource._matches_module
        assert frozen.__spec__ is not None
        cases = (
            mock.patch.object(frozen, "__loader__", SourceFileLoader(frozen.__name__, str(ROOT / "foreign.py"))),
            mock.patch.object(frozen.__spec__, "origin", str(ROOT / "foreign.py")),
            changed_attribute(function, "__code__", function.__code__.replace(co_filename=str(ROOT / "foreign.py"))),
            mock.patch.object(frozen._FrozenCapabilitySource, "_matches_module", lambda self, source, module: True),
            mock.patch.object(frozen._OwnerWaitGuardLock, "require_worker_wait_allowed", lambda self: None),
            changed_attribute(frozen._OwnerWaitGuardLock.acquire, "__defaults__", (False, -1)),
        )
        for patcher in cases:
            with self.subTest(patcher=patcher), patcher, calls() as observed:
                result = composition.matcher_validation_owner.validate_text_v1(generated_at_utc=NOW, valid_until_utc=NOW + timedelta(hours=1), evaluated_at_utc=NOW)
                gate_c.validate_gate_c(generated_at_utc=NOW, valid_until_utc=NOW + timedelta(hours=1), evaluated_at_utc=NOW)
                for operation in ("run", "restore"):
                    args = dict(contract_path=execution.contract_path, publication_owner_identity=object(), publication_graph_nonce=object())
                    work = ROOT / "artifacts/windows/task36a-retry/never-created-remediation"
                    if operation == "run":
                        args.update(work_root=work, evidence_path=work / "evidence.json")
                    else:
                        args.update(state_root=work, base_manifest=None)
                    with self.assertRaises(host._GateDOperationalError):
                        getattr(execution, operation)(**args)
            self.assertIsNone(result.matcher)
            for module, function_name in (("matcher_validation", "build_validated_matcher_v1"), ("tm_retrieval_validation", "_recompute_retrieval_validation_from_session"), ("tm_benchmark_gate", "_run_benchmark_gate_d_from_session"), ("tm_benchmark_gate", "_restore_gate_d_attestation")):
                self.assertFalse(entries(observed, module, function_name), (module, function_name))

    def test_helper_replacement_cannot_mask_a_foreign_host_loader(self):
        composition = host.compose_capability_host(source_authority=self.source(), evaluated_at_utc=NOW)
        with mock.patch.object(frozen._FrozenCapabilitySource, "_matches_module", lambda self, source, module: True), mock.patch.object(host, "__loader__", object()), calls() as observed:
            result = composition.matcher_validation_owner.validate_text_v1(generated_at_utc=NOW, valid_until_utc=NOW + timedelta(hours=1), evaluated_at_utc=NOW)
        self.assertIsNone(result.matcher)
        self.assertFalse(entries(observed, "matcher_validation", "build_validated_matcher_v1"))
