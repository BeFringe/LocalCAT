from __future__ import annotations

from dataclasses import dataclass, fields, replace
import inspect
from pathlib import Path
from typing import cast
import unittest

import tm_contracts as contract_module
from tm_contracts import (
    ACTIVATION_TOKEN_VERSION,
    CANONICAL_RESOURCE_IDENTITY_VERSION,
    GENERATION_EXPECTATION_VERSION,
    SNAPSHOT_BINDING_VERSION,
    SNAPSHOT_FORMAT_VERSION,
    SNAPSHOT_MANIFEST_VERSION,
    STAGE_VALIDATION_EVIDENCE_VERSION,
    ActivationCapabilityState,
    CanonicalResourceIdentity,
    GenerationExpectation,
    MutableStageRef,
    ResourceStoreCoordinatorPort,
    SealedStage,
    SnapshotBinding,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    SourceBindingState,
    StageValidationEvidence,
    contract_from_json,
    contract_to_json,
    snapshot_receipt_digest,
)


_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_DIGEST_D = "d" * 64
_DIGEST_E = "e" * 64


def _identity(
    resource_id: str = "tm.primary",
    configured_path: Path = Path("/catalog/tm.jsonl"),
) -> CanonicalResourceIdentity:
    return CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        configured_path,
    )


def _receipt(
    resource_id: str = "tm.primary",
    *,
    canonical_store_id: str = "store.primary.v1",
) -> SnapshotReceipt:
    return SnapshotReceipt(
        snapshot_id="snapshot.primary.4",
        resource_id=resource_id,
        canonical_store_id=canonical_store_id,
        exported_revision=4,
        jsonl_digest=_DIGEST_A,
        record_count=8,
        format_version=SNAPSHOT_FORMAT_VERSION,
    )


def _manifest(receipt: SnapshotReceipt | None = None) -> SnapshotManifest:
    bound_receipt = receipt or _receipt()
    return SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=bound_receipt,
        receipt_digest=snapshot_receipt_digest(bound_receipt),
    )


def _binding(receipt: SnapshotReceipt | None = None) -> SnapshotBinding:
    bound_receipt = receipt or _receipt()
    return SnapshotBinding(
        configured_jsonl_path=Path("/catalog/tm.jsonl"),
        manifest_path=Path("/catalog/tm.jsonl.localcat-snapshot.json"),
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=bound_receipt,
        manifest=_manifest(bound_receipt),
        binding_version=SNAPSHOT_BINDING_VERSION,
    )


def _evidence(
    identity: CanonicalResourceIdentity | None = None,
    binding: SnapshotBinding | None = None,
) -> StageValidationEvidence:
    resource_identity = identity or _identity()
    source_binding = binding or _binding()
    return StageValidationEvidence(
        evidence_version=STAGE_VALIDATION_EVIDENCE_VERSION,
        resource_id=resource_identity.resource_id,
        target_identity=resource_identity.target_identity,
        source_binding=source_binding,
        snapshot_receipt_digest=snapshot_receipt_digest(
            source_binding.receipt
        ),
        manifest_temp_digest=_DIGEST_B,
        schema_version=1,
        fold_version="fold-v1",
        index_version="candidate-index-v1",
        record_count=8,
        origin_batch_count=1,
        fts_count=8,
        gram_counts=((1, 80), (2, 72)),
        exact_parity_digest=_DIGEST_C,
        integrity_ok=True,
        foreign_keys_ok=True,
        stage_file_digest=_DIGEST_D,
    )


def _mutable(
    identity: CanonicalResourceIdentity | None = None,
    stage_id: str = "stage.primary.1",
) -> MutableStageRef:
    resource_identity = identity or _identity()
    stem = resource_identity.configured_jsonl_path.stem
    return MutableStageRef(
        stage_id=stage_id,
        resource_identity=resource_identity,
        staged_db_path=Path(f"/catalog/{stem}.stage.sqlite3"),
        manifest_temp_path=Path(f"/catalog/{stem}.manifest.stage.json"),
    )


def _generation(
    evidence: StageValidationEvidence,
    expected_prior_generation: int | None = None,
) -> GenerationExpectation:
    return GenerationExpectation(
        resource_id=evidence.resource_id,
        target_identity=evidence.target_identity,
        canonical_store_id=(
            evidence.source_binding.receipt.canonical_store_id
        ),
        snapshot_receipt_digest=evidence.snapshot_receipt_digest,
        expected_prior_generation=expected_prior_generation,
        expectation_version=GENERATION_EXPECTATION_VERSION,
    )


@dataclass
class _FakeEntry:
    mutable: MutableStageRef
    stage: SealedStage
    state: ActivationCapabilityState
    token: contract_module._ActivationToken | None = None


class _FakeRegistry(contract_module._SealedArtifactRegistryPort):
    """Test-only authority; production state transitions belong to task 5.5."""

    def __init__(self, namespace: str = "coordinator.test") -> None:
        self._namespace = namespace
        self._entries: dict[str, _FakeEntry] = {}
        self._tokens: dict[str, _FakeEntry] = {}

    @property
    def registry_namespace(self) -> str:
        return self._namespace

    def seal(
        self,
        mutable_stage: MutableStageRef,
        evidence: StageValidationEvidence,
        generation: GenerationExpectation,
    ) -> SealedStage:
        if not isinstance(mutable_stage, MutableStageRef):
            raise TypeError("registry sealing requires MutableStageRef")
        if any(
            entry.mutable.stage_id == mutable_stage.stage_id
            for entry in self._entries.values()
        ):
            raise ValueError("mutable stage is already sealed")
        artifact_id = f"artifact.{len(self._entries) + 1}"
        stage = contract_module._create_sealed_stage(
            registry_namespace=self._namespace,
            artifact_id=artifact_id,
            mutable_stage=mutable_stage,
            evidence=evidence,
            generation=generation,
            activation_nonce=f"nonce.{artifact_id}",
        )
        self._entries[artifact_id] = _FakeEntry(
            mutable=mutable_stage,
            stage=stage,
            state=ActivationCapabilityState.SEALED,
        )
        return stage

    def _entry(self, stage: SealedStage) -> _FakeEntry:
        entry = self._entries.get(stage.artifact.artifact_id)
        if (
            entry is None
            or entry.stage is not stage
            or stage.artifact.registry_namespace != self._namespace
        ):
            raise ValueError("sealed stage is not a registry member")
        return entry

    def contains(self, stage: SealedStage) -> bool:
        try:
            self._entry(stage)
        except (AttributeError, ValueError):
            return False
        return True

    def state(self, stage: SealedStage) -> ActivationCapabilityState:
        return self._entry(stage).state

    def issue_token(
        self,
        stage: SealedStage,
        *,
        current_generation: int | None,
    ) -> contract_module._ActivationToken:
        entry = self._entry(stage)
        if entry.state is not ActivationCapabilityState.SEALED:
            raise ValueError("token already issued or stage terminal")
        if current_generation is not None and (
            not isinstance(current_generation, int)
            or isinstance(current_generation, bool)
        ):
            raise TypeError("current generation must be an integer or None")
        if current_generation is not None and current_generation < 0:
            raise ValueError("current generation must be non-negative")
        if current_generation != stage.expected_prior_generation:
            raise ValueError("expected prior generation is stale")
        token = contract_module._create_activation_token(
            token_id=f"token.{stage.artifact.artifact_id}",
            stage=stage,
        )
        entry.token = token
        entry.state = ActivationCapabilityState.TOKEN_ISSUED
        self._tokens[token.token_id] = entry
        return token

    def _token_entry(
        self,
        token: contract_module._ActivationToken,
    ) -> _FakeEntry:
        entry = self._tokens.get(token.token_id)
        if entry is None or entry.token is not token:
            raise ValueError("activation token is not a registry member")
        contract_module._validate_activation_token_for_stage(
            token,
            entry.stage,
        )
        return entry

    def consume(self, token: contract_module._ActivationToken) -> None:
        entry = self._token_entry(token)
        if entry.state is not ActivationCapabilityState.TOKEN_ISSUED:
            raise ValueError("activation token is not issuable")
        entry.state = ActivationCapabilityState.CONSUMED

    def cancel(self, token: contract_module._ActivationToken) -> None:
        entry = self._token_entry(token)
        if entry.state is not ActivationCapabilityState.TOKEN_ISSUED:
            raise ValueError("activation token is not cancellable")
        entry.state = ActivationCapabilityState.CANCELLED


class CanonicalActivationContractTests(unittest.TestCase):
    def test_portable_contracts_round_trip_with_stable_versions(self) -> None:
        identity = _identity()
        receipt = _receipt()
        contracts = (
            identity,
            receipt,
            _manifest(receipt),
            _binding(receipt),
            _evidence(identity, _binding(receipt)),
        )

        self.assertEqual(
            identity.identity_version,
            CANONICAL_RESOURCE_IDENTITY_VERSION,
        )
        self.assertEqual(
            identity.canonical_sidecar_path,
            Path("/catalog/tm.jsonl.sqlite3"),
        )
        for contract in contracts:
            with self.subTest(contract=type(contract).__name__):
                encoded = contract_to_json(contract)
                decoded = contract_from_json(encoded)
                self.assertEqual(decoded, contract)
                self.assertEqual(contract_to_json(decoded), encoded)

    def test_source_binding_states_are_closed(self) -> None:
        self.assertEqual(
            tuple(SourceBindingState),
            (
                SourceBindingState.VERIFIED_CURRENT,
                SourceBindingState.VERIFIED_HISTORY,
                SourceBindingState.SOURCE_DIVERGED,
            ),
        )

    def test_identity_rejects_non_deterministic_sidecar_or_manifest(self) -> None:
        identity = _identity()
        with self.assertRaisesRegex(ValueError, "canonical sidecar"):
            CanonicalResourceIdentity(
                resource_id=identity.resource_id,
                configured_jsonl_path=identity.configured_jsonl_path,
                canonical_sidecar_path=Path("/catalog/other.sqlite3"),
                snapshot_manifest_path=identity.snapshot_manifest_path,
                target_identity=identity.target_identity,
                identity_version=CANONICAL_RESOURCE_IDENTITY_VERSION,
            )
        with self.assertRaisesRegex(ValueError, "snapshot manifest"):
            CanonicalResourceIdentity(
                resource_id=identity.resource_id,
                configured_jsonl_path=identity.configured_jsonl_path,
                canonical_sidecar_path=identity.canonical_sidecar_path,
                snapshot_manifest_path=Path("/catalog/other.json"),
                target_identity=identity.target_identity,
                identity_version=CANONICAL_RESOURCE_IDENTITY_VERSION,
            )

    def test_receipt_manifest_binding_and_ancestry_must_close(self) -> None:
        receipt = _receipt()
        with self.assertRaisesRegex(ValueError, "receipt digest"):
            SnapshotManifest(
                manifest_version=SNAPSHOT_MANIFEST_VERSION,
                snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
                receipt=receipt,
                receipt_digest=_DIGEST_E,
            )

        other_receipt = _receipt(canonical_store_id="other.store")
        with self.assertRaisesRegex(ValueError, "same receipt"):
            SnapshotBinding(
                configured_jsonl_path=Path("/catalog/tm.jsonl"),
                manifest_path=Path(
                    "/catalog/tm.jsonl.localcat-snapshot.json"
                ),
                snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
                receipt=receipt,
                manifest=_manifest(other_receipt),
                binding_version=SNAPSHOT_BINDING_VERSION,
            )

        with self.assertRaisesRegex(ValueError, "record count"):
            replace(_evidence(), record_count=7)

    def test_invalid_versions_digests_and_validation_flags_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "format version"):
            replace(_receipt(), format_version="snapshot-v2")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            replace(_receipt(), jsonl_digest="not-a-digest")
        with self.assertRaisesRegex(ValueError, "integrity"):
            replace(_evidence(), integrity_ok=False)

    def test_runtime_capability_types_are_private_and_not_directly_constructible(
        self,
    ) -> None:
        self.assertFalse(hasattr(contract_module, "SealedArtifactRef"))
        self.assertFalse(hasattr(contract_module, "ActivationToken"))
        self.assertNotIn("SealedArtifactRef", contract_module.__all__)
        self.assertNotIn("ActivationToken", contract_module.__all__)

        private_artifact_type = getattr(
            contract_module,
            "_SealedArtifactRef",
        )
        private_token_type = getattr(contract_module, "_ActivationToken")
        with self.assertRaisesRegex(TypeError, "private factory"):
            private_artifact_type(
                registry_namespace="coordinator.test",
                artifact_id="artifact.direct",
                seal_digest=_DIGEST_A,
            )
        with self.assertRaisesRegex(TypeError, "private factory"):
            private_token_type(
                token_id="token.direct",
                registry_namespace="coordinator.test",
                resource_id="tm.primary",
                target_identity=_DIGEST_A,
                canonical_store_id="store.primary.v1",
                artifact_id="artifact.direct",
                artifact_seal_digest=_DIGEST_B,
                sealed_stage_digest=_DIGEST_C,
                snapshot_receipt_digest=_DIGEST_D,
                expected_prior_generation=None,
                activation_nonce="nonce.direct",
                token_version=ACTIVATION_TOKEN_VERSION,
            )

        registry = _FakeRegistry()
        evidence = _evidence()
        stage = registry.seal(
            _mutable(),
            evidence,
            _generation(evidence),
        )
        self.assertFalse(hasattr(stage.artifact, "staged_db_path"))
        self.assertFalse(hasattr(stage.artifact, "manifest_temp_path"))
        token = registry.issue_token(stage, current_generation=None)
        self.assertEqual(token.token_version, ACTIVATION_TOKEN_VERSION)
        with self.assertRaisesRegex(TypeError, "unsupported"):
            contract_to_json(
                cast(
                    object,
                    stage,
                )  # pyright: ignore[reportArgumentType]
            )

    def test_public_coordinator_surface_hides_token_lifecycle(self) -> None:
        self.assertFalse(
            hasattr(contract_module, "SealedArtifactRegistryPort")
        )
        self.assertNotIn(
            "SealedArtifactRegistryPort",
            contract_module.__all__,
        )
        self.assertNotIn("_ActivationToken", contract_module.__all__)

        self.assertTrue(hasattr(ResourceStoreCoordinatorPort, "activate"))
        for hidden_method in (
            "prepare_activation",
            "issue_token",
            "consume",
            "cancel",
        ):
            with self.subTest(hidden_method=hidden_method):
                self.assertFalse(
                    hasattr(ResourceStoreCoordinatorPort, hidden_method)
                )
        annotations = inspect.get_annotations(
            ResourceStoreCoordinatorPort.activate,
            eval_str=False,
        )
        self.assertNotIn("_ActivationToken", repr(annotations))

    def test_registry_membership_rejects_object_new_and_cross_registry_forgery(
        self,
    ) -> None:
        registry = _FakeRegistry()
        evidence = _evidence()
        stage = registry.seal(
            _mutable(),
            evidence,
            _generation(evidence),
        )
        forged_equal = object.__new__(SealedStage)
        for item in fields(stage):
            object.__setattr__(
                forged_equal,
                item.name,
                getattr(stage, item.name),
            )
        self.assertEqual(forged_equal, stage)
        self.assertFalse(registry.contains(forged_equal))
        with self.assertRaisesRegex(ValueError, "registry member"):
            registry.issue_token(
                forged_equal,
                current_generation=None,
            )

        foreign_registry = _FakeRegistry("coordinator.foreign")
        foreign_stage = foreign_registry.seal(
            _mutable(stage_id="stage.foreign.1"),
            evidence,
            _generation(evidence),
        )
        self.assertFalse(registry.contains(foreign_stage))
        with self.assertRaisesRegex(ValueError, "registry member"):
            registry.issue_token(
                foreign_stage,
                current_generation=None,
            )

    def test_bare_path_and_mismatched_identity_cannot_be_sealed(self) -> None:
        registry = _FakeRegistry()
        evidence = _evidence()
        with self.assertRaises(TypeError):
            registry.seal(
                cast(
                    MutableStageRef,
                    cast(object, Path("/catalog/stage")),
                ),
                evidence,
                _generation(evidence),
            )

        other_identity = _identity(
            "tm.secondary",
            Path("/catalog/other.jsonl"),
        )
        with self.assertRaisesRegex(ValueError, "resource identity"):
            registry.seal(
                _mutable(other_identity),
                evidence,
                _generation(evidence),
            )

    def test_registry_owns_single_token_state_transitions(self) -> None:
        registry = _FakeRegistry()
        evidence = _evidence()
        stage = registry.seal(
            _mutable(),
            evidence,
            _generation(evidence),
        )
        self.assertIs(
            registry.state(stage),
            ActivationCapabilityState.SEALED,
        )
        token = registry.issue_token(stage, current_generation=None)
        self.assertIs(
            registry.state(stage),
            ActivationCapabilityState.TOKEN_ISSUED,
        )
        with self.assertRaisesRegex(ValueError, "already issued"):
            registry.issue_token(stage, current_generation=None)
        registry.consume(token)
        self.assertIs(
            registry.state(stage),
            ActivationCapabilityState.CONSUMED,
        )
        with self.assertRaisesRegex(ValueError, "not cancellable"):
            registry.cancel(token)

        second = registry.seal(
            _mutable(stage_id="stage.primary.2"),
            evidence,
            _generation(evidence),
        )
        second_token = registry.issue_token(
            second,
            current_generation=None,
        )
        registry.cancel(second_token)
        self.assertIs(
            registry.state(second),
            ActivationCapabilityState.CANCELLED,
        )

    def test_expected_generation_and_full_token_chain_are_closed(self) -> None:
        registry = _FakeRegistry()
        evidence = _evidence()
        stage = registry.seal(
            _mutable(),
            evidence,
            _generation(evidence, expected_prior_generation=3),
        )
        with self.assertRaisesRegex(ValueError, "expected prior"):
            registry.issue_token(stage, current_generation=2)
        token = registry.issue_token(stage, current_generation=3)
        contract_module._validate_activation_token_for_stage(token, stage)

        tampered = object.__new__(contract_module._ActivationToken)
        for item in fields(token):
            object.__setattr__(
                tampered,
                item.name,
                getattr(token, item.name),
            )
        object.__setattr__(
            tampered,
            "artifact_seal_digest",
            _DIGEST_E,
        )
        with self.assertRaisesRegex(ValueError, "does not close"):
            contract_module._validate_activation_token_for_stage(
                tampered,
                stage,
            )
        with self.assertRaisesRegex(ValueError, "registry member"):
            registry.consume(tampered)

    def test_generation_zero_is_valid_but_bool_and_negative_are_rejected(
        self,
    ) -> None:
        registry = _FakeRegistry()
        evidence = _evidence()
        generation_zero = _generation(
            evidence,
            expected_prior_generation=0,
        )
        self.assertEqual(generation_zero.expected_prior_generation, 0)

        stage = registry.seal(_mutable(), evidence, generation_zero)
        with self.assertRaisesRegex(ValueError, "expected prior"):
            registry.issue_token(stage, current_generation=None)
        with self.assertRaisesRegex(TypeError, "current generation"):
            registry.issue_token(stage, current_generation=False)
        token = registry.issue_token(stage, current_generation=0)
        self.assertEqual(token.expected_prior_generation, 0)

        with self.assertRaisesRegex(ValueError, "at least 0"):
            _generation(evidence, expected_prior_generation=-1)
        with self.assertRaisesRegex(TypeError, "must be an integer"):
            _generation(evidence, expected_prior_generation=False)


if __name__ == "__main__":
    unittest.main()
