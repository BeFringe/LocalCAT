"""Task 2.1 contract guards for the backend-neutral platform file boundary."""

from __future__ import annotations

import ast
import copy
from dataclasses import FrozenInstanceError, fields, replace
import hashlib
import inspect
import json
import math
from pathlib import Path, PurePath, PurePosixPath
import pickle
import threading
import unittest
from unittest import mock

from platform_fs_contracts import (
    BoundContentFacts,
    BoundDirectoryAuthority,
    BoundExistingFileMutationGuard,
    BoundRegularFile,
    BoundSynchronizedRegularFile,
    CandidateFile,
    CandidateContentFacts,
    DEVICE_KEY_ID_HASH_ALGORITHM,
    DEVICE_KEY_ID_SIZE_BYTES,
    DEVICE_SECRET_SIZE_BYTES,
    DeviceSecretAuthority,
    EntrySnapshot,
    ExistingFileDurability,
    ExistingFileMutationGuard,
    ExistingFileRetirement,
    ExistingRetirementSource,
    RetirementSourceDirectoryAuthority,
    FileObjectIdentity,
    LedgerEntryObservation,
    LedgerEnumerationLimits,
    LockLease,
    LockPolicy,
    LockWait,
    MutableFileReservation,
    MutableFileReservationService,
    OwnedNamespaceRetirement,
    OpaqueAuthority,
    PendingPublication,
    PersistentPrivateProof,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateStorageProof,
    PrivateAccessEvidence,
    PrivateProofContext,
    PrivateProofObjectRole,
    PRIVATE_PROOF_DIGEST_SIZE_BYTES,
    ProcessFileLock,
    PublishFacts,
    PublishMode,
    RootedDirectoryAuthority,
    RootedFileSystem,
    RetainedRetirement,
    RetirementDirectoryAuthority,
    VerifiedPrivateProof,
    WINDOWS_PRIVATE_PROOF_DOMAIN_TAG,
    WINDOWS_PRIVATE_PROOF_SCHEMA,
    WINDOWS_PRIVATE_SECURITY_PROFILE_ID,
    WindowsPrivateProof,
    derive_device_key_id,
    decode_windows_private_proof,
    encode_windows_private_proof,
    encode_windows_private_proof_unsigned,
    windows_private_proof_mac_message,
    validate_relative_name,
    validate_relative_path,
)


_ROOT = Path(__file__).resolve().parents[1]
_DIGEST = b"d" * 32


def _identity(*, kind: str = "regular") -> FileObjectIdentity:
    return FileObjectIdentity(
        platform="windows",
        volume_id=b"volume",
        file_id=b"f" * 16,
        kind=kind,
        link_count=1,
    )


def _facts(mode: PublishMode = PublishMode.CREATE_IF_ABSENT) -> PublishFacts:
    return PublishFacts(
        mode=mode,
        destination_identity=_identity(),
        content_sha256=_DIGEST,
        byte_count=7,
        reparse_free=True,
    )


class _ProbeAuthority(OpaqueAuthority):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def _close_authority(self) -> None:
        self.close_calls += 1

    def touch(self) -> None:
        self._require_open()


class _Regular(BoundRegularFile):
    def __init__(self, payload: bytes = b"payload") -> None:
        super().__init__()
        self.payload = payload
        self.read_at_calls: list[tuple[int, int, EntrySnapshot]] = []

    def _close_authority(self) -> None:
        pass

    def _read_at(
        self,
        offset: int,
        maximum_bytes: int,
        expected: EntrySnapshot,
    ) -> bytes:
        if expected != self._snapshot():
            raise AssertionError("unexpected snapshot")
        self.read_at_calls.append((offset, maximum_bytes, expected))
        return self.payload[offset : offset + maximum_bytes]

    def _identity(self) -> FileObjectIdentity:
        return _identity()

    def _snapshot(self) -> EntrySnapshot:
        return EntrySnapshot(_identity(), len(self.payload), b"token", True)


class _SynchronizedRegular(_Regular, BoundSynchronizedRegularFile):
    def __init__(self, payload: bytes = b"payload") -> None:
        super().__init__(payload)
        self.synchronize_calls = 0

    def _synchronize_content(
        self,
        expected: BoundContentFacts,
    ) -> BoundContentFacts:
        self.synchronize_calls += 1
        return expected


class _MutationGuard(BoundExistingFileMutationGuard):
    def __init__(self) -> None:
        super().__init__(_identity())

    def _reprove_guard(self) -> FileObjectIdentity:
        return _identity()

    def _close_authority(self) -> None:
        pass


class _Candidate(CandidateFile):
    def __init__(self) -> None:
        super().__init__()
        self.payload = b""
        self.flushed = False

    def _close_authority(self) -> None:
        pass

    def _write_chunks(self, chunks: object) -> CandidateContentFacts:
        self.payload = b"".join(chunks)  # type: ignore[arg-type]
        return CandidateContentFacts(len(self.payload), hashlib.sha256(self.payload).digest())

    def _flush_content(self, expected: CandidateContentFacts) -> CandidateContentFacts:
        self.flushed = True
        return expected

    def _identity(self) -> FileObjectIdentity:
        return _identity()


class _FaultingCandidate(_Candidate):
    def __init__(self) -> None:
        super().__init__()
        self.fail_write = False
        self.fail_flush = False

    def _write_chunks(self, chunks: object) -> CandidateContentFacts:
        if self.fail_write:
            raise RuntimeError("write fault")
        return super()._write_chunks(chunks)

    def _flush_content(self, expected: CandidateContentFacts) -> CandidateContentFacts:
        if self.fail_flush:
            raise RuntimeError("flush fault")
        return super()._flush_content(expected)


class _Lease(LockLease):
    def __init__(self) -> None:
        super().__init__()
        self.reprove_calls = 0
        self.binding_calls: list[tuple[BoundDirectoryAuthority, str, bytes]] = []

    def _reprove_lock(self) -> None:
        self.reprove_calls += 1

    def _reprove_binding(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
    ) -> None:
        self.binding_calls.append((parent, name, payload))

    def _close_authority(self) -> None:
        pass


class _MutableReservation(MutableFileReservation):
    def __init__(self, created_identity: FileObjectIdentity | None = None) -> None:
        selected = created_identity or _identity()
        super().__init__(selected)
        self.observed: object = selected
        self.reprove_calls = 0
        self.close_calls = 0

    def _reprove_identity(self) -> FileObjectIdentity:
        self.reprove_calls += 1
        return self.observed  # type: ignore[return-value]

    def _close_authority(self) -> None:
        self.close_calls += 1


class _TransferFaultReservation(_MutableReservation):
    def _mark_authority_transferred(self) -> None:
        super()._mark_authority_transferred()
        raise KeyboardInterrupt("after transfer state")


class _RetirementDirectory(RetirementDirectoryAuthority):
    def _reprove(self) -> None:
        pass

    def _inspect_entry(self, name: str) -> EntrySnapshot | None:
        del name
        return None

    def _close_authority(self) -> None:
        pass


class _RetainedRetirement(RetainedRetirement):
    def __init__(self, identity: FileObjectIdentity) -> None:
        super().__init__(identity)
        self.observed = EntrySnapshot(identity, 7, b"token", True)
        self.close_calls = 0
        self.fail_close = False

    def _reprove_retirement(self) -> EntrySnapshot:
        return self.observed

    def _close_retirement_authority(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("retirement close fault")


class _RetirementService(OwnedNamespaceRetirement):
    def _bind_or_create_child_directory(
        self,
        parent: BoundDirectoryAuthority | RetirementDirectoryAuthority,
        name: str,
    ) -> RetirementDirectoryAuthority:
        del parent, name
        return _RetirementDirectory()

    def _retire_owned_exclusive(
        self,
        source_parent: BoundDirectoryAuthority,
        source_name: str,
        reservation: MutableFileReservation,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
    ) -> RetainedRetirement:
        del source_parent, source_name, target_parent, target_name
        identity = reservation.identity()
        result = _RetainedRetirement(identity)
        result._accept_reservation_transfer(reservation)
        return result


class _FailingExistingRetirementSource(ExistingRetirementSource):
    def __init__(self, error: BaseException) -> None:
        super().__init__(_identity(), CandidateContentFacts(7, _DIGEST))
        self.error = error
        self.close_calls = 0

    def _reprove_source(self) -> BoundContentFacts:
        raise self.error

    def _close_authority(self) -> None:
        self.close_calls += 1


class _ExistingSourceParent(RetirementSourceDirectoryAuthority):
    def __init__(self, error: BaseException | None = None) -> None:
        super().__init__()
        self.error = error
        self.close_calls = 0

    def _reprove_parent(self) -> None:
        if self.error is not None:
            raise self.error

    def _close_authority(self) -> None:
        self.close_calls += 1


class _ExistingRetirementService(ExistingFileRetirement):
    def __init__(
        self,
        source: ExistingRetirementSource,
        rebound: RetainedRetirement,
    ) -> None:
        self.source = source
        self.rebound = rebound
        self.parent = _ExistingSourceParent()

    def _bind_retirement_source_directory(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> RetirementSourceDirectoryAuthority:
        del root, relative
        return self.parent

    def _open_existing_retirement_source(
        self,
        source_parent: RetirementSourceDirectoryAuthority,
        expected_content: CandidateContentFacts,
    ) -> ExistingRetirementSource:
        del source_parent, expected_content
        return self.source

    def _retire_existing_exclusive(
        self,
        source_parent: RetirementSourceDirectoryAuthority,
        source: ExistingRetirementSource,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
    ) -> RetainedRetirement:
        del source_parent, source, target_parent, target_name
        raise AssertionError("not used")

    def _rebind_existing_retirement(
        self,
        source_parent: BoundDirectoryAuthority,
        source_name: str,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
        expected_content: CandidateContentFacts,
    ) -> RetainedRetirement:
        del source_parent, source_name, target_parent, target_name, expected_content
        return self.rebound


class _PrivateEvidence(PrivateAccessEvidence):
    def _close_authority(self) -> None:
        pass


class _IssuedPrivateEvidence(PrivateAccessEvidence):
    __slots__ = ("issuer",)

    def __init__(self, issuer: object) -> None:
        super().__init__()
        self.issuer = issuer

    def _close_authority(self) -> None:
        pass


class _DeviceSecret(DeviceSecretAuthority):
    __slots__ = ("issuer", "retained", "reprove_calls")

    def __init__(self, issuer: object, payload: bytes = b"s" * 32) -> None:
        super().__init__()
        self.issuer = issuer
        self.retained = _Regular(payload)
        self.reprove_calls = 0

    def _reprove(self) -> None:
        self.retained.snapshot()
        self.reprove_calls += 1

    def clone(self) -> _DeviceSecret:
        self.reprove()
        return _DeviceSecret(self.issuer, self.retained.read_all())

    def _close_authority(self) -> None:
        self.retained.close()


class _VerifiedPrivate(VerifiedPrivateProof):
    __slots__ = (
        "context",
        "issuer",
        "retained_secret",
        "retained_target",
        "terminal_reproof_calls",
    )

    def __init__(
        self,
        issuer: object,
        context: PrivateProofContext,
        secret: _DeviceSecret,
    ) -> None:
        super().__init__()
        self.issuer = issuer
        self.context = context
        self.retained_target = _IssuedPrivateEvidence(issuer)
        self.retained_secret = secret.clone()
        self.terminal_reproof_calls = 0

    def terminal_reproof(self) -> None:
        self.retained_target._require_open()
        self.retained_secret.reprove()
        self.terminal_reproof_calls += 1

    def _close_authority(self) -> None:
        try:
            self.retained_secret.close()
        finally:
            self.retained_target.close()


class _Pending(PendingPublication):
    def __init__(
        self,
        mode: PublishMode,
        *,
        preliminary_facts: PublishFacts | None = None,
        destination: BoundRegularFile | None = None,
    ) -> None:
        self.mode = mode
        self.destination = destination or _Regular()
        super().__init__(preliminary_facts or _facts(mode), self.destination)

    def _terminal_reproof(
        self,
        retained_destination: BoundRegularFile,
        preliminary_facts: PublishFacts,
    ) -> PublishFacts:
        self.asserted_destination = retained_destination
        self.asserted_preliminary = preliminary_facts
        return _facts(self.mode)


class _Directory(RootedDirectoryAuthority):
    def __init__(self) -> None:
        super().__init__()
        self.begin_calls = 0
        self.publish_events: list[tuple[str, bool]] = []
        self.publishing_candidate: CandidateFile | None = None
        self.observations: tuple[LedgerEntryObservation, ...] = ()

    def _close_authority(self) -> None:
        pass

    def _reprove(self) -> None:
        pass

    def _inspect_entry(self, name: str) -> EntrySnapshot | None:
        del name
        return None

    def _observe_ledger_entries(
        self,
        lease: LockLease,
        limits: LedgerEnumerationLimits,
    ) -> tuple[LedgerEntryObservation, ...]:
        del lease, limits
        return self.observations

    def _create_candidate(self, name: str, *, private: bool) -> CandidateFile:
        del name, private
        return _Candidate()

    def _rename_candidate(
        self,
        candidate: CandidateFile,
        destination: str,
        *,
        mode: PublishMode,
        lease: LockLease | None,
    ) -> PublishFacts:
        del destination, lease
        self.begin_calls += 1
        self.publishing_candidate = candidate
        self.publish_events.append(("renamed", candidate.closed))
        return _facts(mode)

    def _open_published_destination(
        self,
        destination: str,
        preliminary_facts: PublishFacts,
    ) -> BoundRegularFile:
        del destination, preliminary_facts
        assert self.publishing_candidate is not None
        self.publish_events.append(("reopened", self.publishing_candidate.closed))
        return _Regular()

    def _create_pending_publication(
        self,
        preliminary_facts: PublishFacts,
        retained_destination: BoundRegularFile,
    ) -> PendingPublication:
        assert self.publishing_candidate is not None
        self.publish_events.append(("pending_created", self.publishing_candidate.closed))
        return _Pending(
            preliminary_facts.mode,
            preliminary_facts=preliminary_facts,
            destination=retained_destination,
        )

    def _unlink_owned(self, name: str, expected: FileObjectIdentity) -> None:
        del name, expected


class _FileSystem(
    RootedFileSystem,
    MutableFileReservationService,
    ExistingFileDurability,
    ExistingFileMutationGuard,
):
    def _bind_root(self, root: Path) -> RootedDirectoryAuthority:
        del root
        return _Directory()

    def _open_regular(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundRegularFile:
        del root, relative
        return _Regular()

    def _bind_parent(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundDirectoryAuthority:
        del root, relative
        return _Directory()

    def _open_existing_for_synchronization(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundSynchronizedRegularFile:
        del root, relative
        return _SynchronizedRegular()

    def _guard_existing_for_mutation(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundExistingFileMutationGuard:
        del root, relative
        return _MutationGuard()

    def _reserve_mutable_file(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> MutableFileReservation:
        del parent, name
        return _MutableReservation()


class _LockService(ProcessFileLock):
    def _acquire(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
        policy: LockPolicy,
    ) -> LockLease:
        del parent, name, payload, policy
        return _Lease()


class _PrivateService(PrivateStorageProof):
    def _create_private_directory(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> BoundDirectoryAuthority:
        del parent, name
        return _Directory()

    def _prove_private(
        self,
        authority: BoundDirectoryAuthority | BoundRegularFile,
    ) -> PrivateAccessEvidence:
        del authority
        return _PrivateEvidence()


def _private_context(
    role: PrivateProofObjectRole = PrivateProofObjectRole.ATTESTATION,
) -> PrivateProofContext:
    return PrivateProofContext(role, b"c" * 32)


def _windows_private_proof(
    context: PrivateProofContext | None = None,
) -> WindowsPrivateProof:
    selected = context or _private_context()
    return WindowsPrivateProof(
        schema=WINDOWS_PRIVATE_PROOF_SCHEMA,
        object_role=selected.object_role,
        security_profile_id=WINDOWS_PRIVATE_SECURITY_PROFILE_ID,
        owner_sid_sha256=b"o" * 32,
        authority_descriptor_sha256=b"a" * 32,
        owner_context_sha256=selected.owner_context_sha256,
        device_key_id=b"k" * 32,
        device_secret_mac=b"m" * 32,
    )


class _PersistentPrivateService(PersistentPrivateProof):
    def __init__(self) -> None:
        self._issuer = object()
        self.bind_calls = 0
        self.mint_calls = 0
        self.verify_calls = 0
        self.consume_calls = 0

    def issue_target(self) -> PrivateAccessEvidence:
        return _IssuedPrivateEvidence(self._issuer)

    def _require_target(self, target: PrivateAccessEvidence) -> None:
        if type(target) is not _IssuedPrivateEvidence or target.issuer is not self._issuer:
            raise PlatformFileError(
                PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                retryable=False,
            )

    def _require_secret(self, secret: DeviceSecretAuthority) -> None:
        if type(secret) is not _DeviceSecret or secret.issuer is not self._issuer:
            raise PlatformFileError(
                PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                retryable=False,
            )

    def _require_verified(self, verified: VerifiedPrivateProof) -> None:
        if type(verified) is not _VerifiedPrivate or verified.issuer is not self._issuer:
            raise PlatformFileError(
                PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                retryable=False,
            )

    def _bind_device_secret(self, secret_file: BoundRegularFile) -> DeviceSecretAuthority:
        self.bind_calls += 1
        return _DeviceSecret(self._issuer, secret_file.read_all())

    def _mint(
        self,
        target: PrivateAccessEvidence,
        secret: DeviceSecretAuthority,
        context: PrivateProofContext,
    ) -> WindowsPrivateProof:
        self._require_target(target)
        self._require_secret(secret)
        self.mint_calls += 1
        return _windows_private_proof(context)

    def _verify(
        self,
        target: PrivateAccessEvidence,
        secret: DeviceSecretAuthority,
        proof: WindowsPrivateProof,
        expected_context: PrivateProofContext,
    ) -> VerifiedPrivateProof:
        del proof
        self._require_target(target)
        self._require_secret(secret)
        self.verify_calls += 1
        return _VerifiedPrivate(
            self._issuer,
            expected_context,
            secret,
        )

    def _consume_verified(
        self,
        verified: VerifiedPrivateProof,
        expected_context: PrivateProofContext,
    ) -> None:
        self._require_verified(verified)
        if verified.context != expected_context:
            raise PlatformFileError(
                PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                retryable=False,
            )

        def terminal_operation() -> None:
            verified.terminal_reproof()
            self.consume_calls += 1

        VerifiedPrivateProof._consume_once(verified, terminal_operation)


class PlatformFileContractArchitectureTests(unittest.TestCase):
    def test_contract_leaf_uses_only_backend_neutral_standard_library(self) -> None:
        tree = ast.parse((_ROOT / "platform_fs_contracts.py").read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
        self.assertEqual(
            imports,
            {
                "__future__",
                "abc",
                "dataclasses",
                "enum",
                "hashlib",
                "json",
                "math",
                "pathlib",
                "threading",
                "typing",
            },
        )
        source = (_ROOT / "platform_fs_contracts.py").read_text(encoding="utf-8")
        for forbidden in ("fcntl", "ctypes", "msvcrt", "win32", "HANDLE", "dirfd"):
            self.assertNotIn(forbidden, source)

    def test_service_types_are_runtime_opaque_authorities(self) -> None:
        for authority_type in (
            RootedDirectoryAuthority,
            BoundDirectoryAuthority,
            BoundRegularFile,
            BoundSynchronizedRegularFile,
            BoundExistingFileMutationGuard,
            CandidateFile,
            MutableFileReservation,
            RetirementDirectoryAuthority,
            RetainedRetirement,
            LockLease,
            PendingPublication,
            PrivateAccessEvidence,
            DeviceSecretAuthority,
            VerifiedPrivateProof,
        ):
            with self.subTest(authority_type=authority_type.__name__):
                self.assertTrue(issubclass(authority_type, OpaqueAuthority))

    def test_service_port_method_shapes_do_not_expose_backend_representation(self) -> None:
        expected_parameters = {
            RootedFileSystem.bind_root: ("self", "root"),
            RootedFileSystem.open_regular: ("self", "root", "relative"),
            RootedFileSystem.bind_parent: ("self", "root", "relative"),
            ExistingFileDurability.open_existing_for_synchronization: (
                "self",
                "root",
                "relative",
            ),
            ExistingFileMutationGuard.guard_existing_for_mutation: (
                "self",
                "root",
                "relative",
            ),
            MutableFileReservationService.reserve_mutable_file: (
                "self",
                "parent",
                "name",
            ),
            OwnedNamespaceRetirement.bind_or_create_child_directory: (
                "self",
                "parent",
                "name",
            ),
            OwnedNamespaceRetirement.retire_owned_exclusive: (
                "self",
                "source_parent",
                "source_name",
                "reservation",
                "target_parent",
                "target_name",
            ),
            ProcessFileLock.acquire: ("self", "parent", "name", "payload", "policy"),
            LockLease.reprove_binding: ("self", "parent", "name", "payload"),
            PrivateStorageProof.create_private_directory: ("self", "parent", "name"),
            PrivateStorageProof.prove_private: ("self", "authority"),
            PersistentPrivateProof.bind_device_secret: ("self", "secret_file"),
            PersistentPrivateProof.mint: (
                "self",
                "target",
                "secret",
                "context",
            ),
            PersistentPrivateProof.verify: (
                "self",
                "target",
                "secret",
                "proof",
                "expected_context",
            ),
            PersistentPrivateProof.consume_verified: (
                "self",
                "verified",
                "expected_context",
            ),
        }
        for method, expected in expected_parameters.items():
            with self.subTest(method=method.__qualname__):
                self.assertEqual(tuple(inspect.signature(method).parameters), expected)

        public_pending_methods = {
            name
            for name, value in inspect.getmembers(PendingPublication, inspect.isfunction)
            if not name.startswith("_")
        }
        self.assertEqual(
            public_pending_methods,
            {"close", "preliminary_facts", "retained_destination", "terminal_reproof"},
        )
        for forbidden in ("handle", "dirfd", "commit", "success", "receipt", "expected_target"):
            self.assertFalse(
                any(forbidden in name.casefold() for name in public_pending_methods),
                forbidden,
            )

    def test_persistent_proof_port_is_not_part_of_aggregate_backend(self) -> None:
        self.assertNotIn(PersistentPrivateProof, PlatformFileBackend.__bases__)
        self.assertFalse(isinstance(_PrivateService(), PersistentPrivateProof))
        self.assertTrue(isinstance(_PersistentPrivateService(), PersistentPrivateProof))


class PlatformFileValueContractTests(unittest.TestCase):
    def test_private_proof_constants_context_and_record_shape_are_exact(self) -> None:
        self.assertEqual(WINDOWS_PRIVATE_PROOF_SCHEMA, 1)
        self.assertEqual(WINDOWS_PRIVATE_SECURITY_PROFILE_ID, "WindowsPrivateSecurityV2")
        self.assertEqual(DEVICE_SECRET_SIZE_BYTES, 32)
        self.assertEqual(DEVICE_KEY_ID_HASH_ALGORITHM, "sha256")
        self.assertEqual(DEVICE_KEY_ID_SIZE_BYTES, 32)
        self.assertEqual(PRIVATE_PROOF_DIGEST_SIZE_BYTES, 32)
        self.assertEqual(
            WINDOWS_PRIVATE_PROOF_DOMAIN_TAG,
            b"localcat.windows.private-proof.v1\0",
        )
        self.assertEqual(
            tuple(role.value for role in PrivateProofObjectRole),
            (
                "PRIVATE_DIRECTORY",
                "DEVICE_KEY",
                "ATTESTATION",
                "DEVICE_KEY_CANDIDATE",
                "ATTESTATION_CANDIDATE",
            ),
        )
        context = _private_context()
        proof = _windows_private_proof(context)
        self.assertEqual(
            tuple(field.name for field in fields(context)),
            ("object_role", "owner_context_sha256"),
        )
        self.assertEqual(
            tuple(field.name for field in fields(proof)),
            (
                "schema",
                "object_role",
                "security_profile_id",
                "owner_sid_sha256",
                "authority_descriptor_sha256",
                "owner_context_sha256",
                "device_key_id",
                "device_secret_mac",
            ),
        )
        self.assertNotIsInstance(proof, OpaqueAuthority)
        self.assertEqual(pickle.loads(pickle.dumps(proof)), proof)
        with self.assertRaises(FrozenInstanceError):
            proof.schema = 2  # type: ignore[misc]

    def test_private_proof_values_reject_open_roles_and_non_exact_digests(self) -> None:
        with self.assertRaises(TypeError):
            PrivateProofContext("ATTESTATION", b"c" * 32)  # type: ignore[arg-type]
        for digest in (b"short", bytearray(b"c" * 32)):
            with self.subTest(digest=digest):
                with self.assertRaises((TypeError, ValueError)):
                    PrivateProofContext(
                        PrivateProofObjectRole.ATTESTATION,
                        digest,  # type: ignore[arg-type]
                    )

        valid = dict(
            schema=1,
            object_role=PrivateProofObjectRole.ATTESTATION,
            security_profile_id="WindowsPrivateSecurityV2",
            owner_sid_sha256=b"o" * 32,
            authority_descriptor_sha256=b"a" * 32,
            owner_context_sha256=b"c" * 32,
            device_key_id=b"k" * 32,
            device_secret_mac=b"m" * 32,
        )
        invalid = (
            {"schema": True},
            {"schema": 2},
            {"object_role": "ATTESTATION"},
            {"security_profile_id": "windows-private-v1"},
            {"owner_sid_sha256": b"short"},
            {"authority_descriptor_sha256": bytearray(b"a" * 32)},
            {"owner_context_sha256": b"short"},
            {"device_key_id": b"short"},
            {"device_secret_mac": b"short"},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises((TypeError, ValueError)):
                    WindowsPrivateProof(**(valid | change))  # type: ignore[arg-type]

    def test_private_proof_nested_codec_is_exact_canonical_and_round_trips(self) -> None:
        proof = _windows_private_proof()
        encoded = encode_windows_private_proof(proof)
        expected = (
            b'{"authority_descriptor_sha256":"'
            + b"61" * 32
            + b'","device_key_id":"'
            + b"6b" * 32
            + b'","device_secret_mac":"'
            + b"6d" * 32
            + b'","object_role":"ATTESTATION","owner_context_sha256":"'
            + b"63" * 32
            + b'","owner_sid_sha256":"'
            + b"6f" * 32
            + b'","schema":1,"security_profile_id":"WindowsPrivateSecurityV2"}'
        )
        self.assertEqual(encoded, expected)
        self.assertFalse(encoded.endswith(b"\n"))
        self.assertEqual(decode_windows_private_proof(encoded), proof)
        for role in PrivateProofObjectRole:
            with self.subTest(role=role):
                selected = _windows_private_proof(
                    PrivateProofContext(role, b"c" * 32)
                )
                self.assertEqual(
                    decode_windows_private_proof(
                        encode_windows_private_proof(selected)
                    ),
                    selected,
                )
        with self.assertRaises(TypeError):
            encode_windows_private_proof(object())  # type: ignore[arg-type]

    def test_private_proof_decoder_rejects_oversize_before_json_parse(self) -> None:
        from platform_fs_contracts import WINDOWS_PRIVATE_PROOF_MAX_BYTES

        with mock.patch("platform_fs_contracts.json.loads") as loads:
            with self.assertRaises(ValueError):
                decode_windows_private_proof(
                    b"{" + b" " * WINDOWS_PRIVATE_PROOF_MAX_BYTES + b"}"
                )
        loads.assert_not_called()

    def test_private_proof_unsigned_mac_projection_and_key_id_have_golden_bytes(self) -> None:
        proof = _windows_private_proof()
        unsigned = (
            b'{"authority_descriptor_sha256":"'
            + b"61" * 32
            + b'","device_key_id":"'
            + b"6b" * 32
            + b'","object_role":"ATTESTATION","owner_context_sha256":"'
            + b"63" * 32
            + b'","owner_sid_sha256":"'
            + b"6f" * 32
            + b'","schema":1,"security_profile_id":"WindowsPrivateSecurityV2"}'
        )
        self.assertEqual(encode_windows_private_proof_unsigned(proof), unsigned)
        self.assertEqual(
            windows_private_proof_mac_message(proof),
            b"localcat.windows.private-proof.v1\0" + unsigned,
        )
        self.assertNotIn(proof.device_secret_mac.hex().encode("ascii"), unsigned)
        changed_mac = replace(proof, device_secret_mac=b"z" * 32)
        self.assertEqual(
            encode_windows_private_proof_unsigned(changed_mac),
            unsigned,
        )
        self.assertEqual(
            windows_private_proof_mac_message(changed_mac),
            b"localcat.windows.private-proof.v1\0" + unsigned,
        )

        raw_secret = bytes(range(DEVICE_SECRET_SIZE_BYTES))
        expected_key_id = bytes.fromhex(
            "630dcd2966c4336691125448bbb25b4f"
            "f412a49c732db2c8abc1b8581bd710dd"
        )
        self.assertEqual(derive_device_key_id(raw_secret), expected_key_id)
        self.assertEqual(
            derive_device_key_id(raw_secret),
            hashlib.sha256(raw_secret).digest(),
        )
        for invalid in (b"short", b"x" * 33, bytearray(b"x" * 32)):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    derive_device_key_id(invalid)  # type: ignore[arg-type]

    def test_private_proof_nested_codec_rejects_noncanonical_and_malformed_bytes(self) -> None:
        canonical = encode_windows_private_proof(_windows_private_proof())
        mapping = json.loads(canonical)
        malformed = (
            canonical + b"\n",
            json.dumps(mapping, indent=2).encode("utf-8"),
            canonical.replace(b'"schema":1', b'"schema":1.0'),
            canonical.replace(b'"schema":1', b'"schema":NaN'),
            canonical.replace(b"ATTESTATION", b"UNKNOWN_ROLE"),
            canonical.replace(b'"schema":1', b'"schema":1,"schema":1'),
            canonical.replace(b'"schema":1', b'"extra":{"x":1,"x":2},"schema":1'),
            canonical.replace(b"61" * 32, b"61" * 31, 1),
            canonical.replace(b"61" * 32, b"6A" * 32, 1),
            canonical.replace(b'"schema":1,', b"", 1),
            canonical.replace(b'"schema":1', b'"schema":1,"unknown":0'),
            canonical.decode("utf-8"),
            bytearray(canonical),
            b"\xff",
        )
        for value in malformed:
            with self.subTest(value=repr(value)[:120]):
                with self.assertRaises((TypeError, ValueError)):
                    decode_windows_private_proof(value)  # type: ignore[arg-type]
    def test_file_identity_shape_is_frozen_exact_and_live_only(self) -> None:
        identity = _identity()
        self.assertEqual(
            tuple(field.name for field in fields(identity)),
            ("platform", "volume_id", "file_id", "kind", "link_count"),
        )
        with self.assertRaises(FrozenInstanceError):
            identity.link_count = 2  # type: ignore[misc]
        for operation in (
            lambda: pickle.dumps(identity),
            lambda: copy.copy(identity),
            lambda: copy.deepcopy(identity),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(TypeError):
                    operation()

    def test_file_identity_rejects_invalid_physical_facts(self) -> None:
        valid = dict(
            platform="windows",
            volume_id=b"volume",
            file_id=b"f" * 16,
            kind="regular",
            link_count=1,
        )
        invalid = (
            {"platform": "other"},
            {"platform": 1},
            {"volume_id": b""},
            {"volume_id": bytearray(b"volume")},
            {"file_id": b"short"},
            {"file_id": bytearray(b"f" * 16)},
            {"kind": "symlink"},
            {"link_count": 0},
            {"link_count": True},
        )
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises((TypeError, ValueError)):
                    FileObjectIdentity(**(valid | change))

    def test_posix_identity_accepts_opaque_canonical_bytes(self) -> None:
        identity = FileObjectIdentity("posix", b"dev", b"inode", "directory", 2)
        self.assertEqual(identity.kind, "directory")

    def test_entry_snapshot_and_publish_facts_are_exact_platform_facts(self) -> None:
        snapshot = EntrySnapshot(_identity(), 7, b"mtime", True)
        content = BoundContentFacts(snapshot, _DIGEST)
        facts = _facts()
        self.assertEqual(snapshot.byte_count, 7)
        self.assertEqual(content.content_sha256, _DIGEST)
        self.assertEqual(facts.content_sha256, _DIGEST)
        self.assertFalse(
            {"success", "phase", "generation", "compatibility", "receipt", "expected_target"}
            & {field.name for field in fields(facts)}
        )
        invalid_snapshot_args = (
            (object(), 7, b"mtime", True),
            (_identity(), True, b"mtime", True),
            (_identity(), 7, bytearray(b"mtime"), True),
            (_identity(), 7, b"mtime", 1),
        )
        for args in invalid_snapshot_args:
            with self.subTest(args=args):
                with self.assertRaises((TypeError, ValueError)):
                    EntrySnapshot(*args)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            PublishFacts(PublishMode.CREATE_IF_ABSENT, _identity(), b"short", 7, True)
        for args in (
            (object(), _DIGEST),
            (snapshot, bytearray(_DIGEST)),
            (snapshot, b"short"),
        ):
            with self.subTest(args=args):
                with self.assertRaises((TypeError, ValueError)):
                    BoundContentFacts(*args)  # type: ignore[arg-type]
        for operation in (
            lambda: pickle.dumps(content),
            lambda: copy.copy(content),
            lambda: copy.deepcopy(content),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(TypeError):
                    operation()

    def test_publish_mode_is_closed(self) -> None:
        self.assertEqual(
            tuple(mode.value for mode in PublishMode),
            ("create_if_absent", "replace_under_lock"),
        )


class PlatformFileErrorContractTests(unittest.TestCase):
    def test_error_family_codes_and_retryability_are_stable_and_body_free(self) -> None:
        fixed = {
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE: False,
            PlatformFileErrorCode.ENTRY_UNAVAILABLE: False,
            PlatformFileErrorCode.OUTSIDE_ROOT: False,
            PlatformFileErrorCode.REPARSE_REJECTED: False,
            PlatformFileErrorCode.IDENTITY_STALE: True,
            PlatformFileErrorCode.LOCK_CONTENDED: True,
            PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN: False,
            PlatformFileErrorCode.DURABILITY_UNAVAILABLE: False,
            PlatformFileErrorCode.PUBLISH_FAILED: True,
            PlatformFileErrorCode.RECOVERY_REQUIRED: True,
        }
        for code, retryable in fixed.items():
            with self.subTest(code=code):
                error = PlatformFileError(code, retryable=retryable)
                self.assertEqual(error.code, code.value)
                self.assertIs(error.retryable, retryable)
                self.assertEqual(str(error), code.value)
                self.assertFalse(hasattr(error, "path"))
                self.assertFalse(hasattr(error, "body"))
                for name, value in (
                    ("code", "PLATFORM.FS.UNKNOWN"),
                    ("retryable", not retryable),
                    ("path", "private/path"),
                    ("body", "private body"),
                    ("args", ("private body",)),
                ):
                    with self.subTest(code=code, mutation=name):
                        with self.assertRaises(AttributeError):
                            setattr(error, name, value)
                with self.assertRaises(TypeError):
                    error.add_note("private body")
                with self.assertRaises(TypeError):
                    vars(error)
                with self.assertRaises(ValueError):
                    PlatformFileError(code, retryable=not retryable)

        for retryable in (False, True):
            error = PlatformFileError(
                PlatformFileErrorCode.LOCK_UNAVAILABLE,
                retryable=retryable,
            )
            self.assertIs(error.retryable, retryable)

    def test_error_rejects_unknown_codes_subtypes_and_non_boolean_retryability(self) -> None:
        class CodeSubtype(str):
            pass

        for code, retryable in (
            ("PLATFORM.FS.UNKNOWN", False),
            (CodeSubtype(PlatformFileErrorCode.OUTSIDE_ROOT.value), False),
            (PlatformFileErrorCode.OUTSIDE_ROOT, 0),
        ):
            with self.subTest(code=code, retryable=retryable):
                with self.assertRaises((TypeError, ValueError)):
                    PlatformFileError(code, retryable=retryable)  # type: ignore[arg-type]

    def test_error_preserves_python_exception_runtime_protocol(self) -> None:
        error = PlatformFileError(
            PlatformFileErrorCode.OUTSIDE_ROOT,
            retryable=False,
        )
        cause = ValueError("cause")
        context = RuntimeError("context")

        error.__traceback__ = None
        error.__cause__ = cause
        error.__context__ = context

        self.assertIsNone(error.__traceback__)
        self.assertIs(error.__cause__, cause)
        self.assertIs(error.__context__, context)
        self.assertEqual(error.code, PlatformFileErrorCode.OUTSIDE_ROOT.value)
        self.assertIs(error.retryable, False)


class PlatformFileAuthorityContractTests(unittest.TestCase):
    def test_owned_retirement_consumes_reservation_and_retains_exact_identity(self) -> None:
        service = _RetirementService()
        source_parent = _Directory()
        target_parent = service.bind_or_create_child_directory(
            source_parent,
            "quarantine",
        )
        reservation = _MutableReservation()
        retained = service.retire_owned_exclusive(
            source_parent,
            "stage.sqlite3",
            reservation,
            target_parent,
            "stage.sqlite3",
        )
        self.assertTrue(reservation.closed)
        self.assertEqual(reservation.close_calls, 0)
        reservation.close()
        self.assertEqual(reservation.close_calls, 0)
        self.assertEqual(retained.reprove().identity, _identity())
        retained.observed = EntrySnapshot(
            FileObjectIdentity("windows", b"volume", b"x" * 16, "regular", 1),
            7,
            b"token",
            True,
        )
        with self.assertRaises(PlatformFileError) as caught:
            retained.reprove()
        self.assertEqual(caught.exception.code, PlatformFileErrorCode.IDENTITY_STALE.value)
        retained.close()
        retained.close()
        self.assertEqual(retained.close_calls, 1)
        self.assertEqual(reservation.close_calls, 1)
        target_parent.close()
        source_parent.close()

    def test_retirement_close_fault_still_releases_transferred_reservation_once(self) -> None:
        identity = _identity()
        reservation = _MutableReservation(identity)
        retained = _RetainedRetirement(identity)
        retained._accept_reservation_transfer(reservation)
        retained.fail_close = True
        with self.assertRaisesRegex(RuntimeError, "retirement close fault"):
            retained.close()
        self.assertTrue(retained.closed)
        self.assertTrue(reservation.closed)
        self.assertEqual(retained.close_calls, 1)
        self.assertEqual(reservation.close_calls, 1)

    def test_existing_retirement_wrappers_close_failed_terminal_authorities_once(self) -> None:
        source_error = TypeError("source terminal programmer fault")
        source = _FailingExistingRetirementSource(source_error)
        rebound = _RetainedRetirement(_identity())
        rebound.observed = EntrySnapshot(
            FileObjectIdentity("windows", b"volume", b"x" * 16, "regular", 1),
            7,
            b"token",
            True,
        )
        service = _ExistingRetirementService(source, rebound)
        root = _Directory()
        target = _RetirementDirectory()
        expected = CandidateContentFacts(7, _DIGEST)
        try:
            with self.assertRaisesRegex(TypeError, "source terminal programmer fault"):
                source_parent = service.bind_retirement_source_directory(
                    root,
                    PurePosixPath("stage.sqlite3"),
                )
                service.open_existing_retirement_source(
                    source_parent,
                    expected,
                )
            self.assertTrue(source.closed)
            self.assertEqual(source.close_calls, 1)

            with self.assertRaises(PlatformFileError) as caught:
                service.rebind_existing_retirement(
                    root,
                    "stage.sqlite3",
                    target,
                    "stage.sqlite3",
                    expected,
                )
            self.assertEqual(
                caught.exception.code,
                PlatformFileErrorCode.IDENTITY_STALE.value,
            )
            self.assertTrue(rebound.closed)
            self.assertEqual(rebound.close_calls, 1)
        finally:
            if not service.parent.closed:
                service.parent.close()
            target.close()
            root.close()

    def test_source_directory_bind_closes_wrong_and_reprove_fault_authorities_once(self) -> None:
        class WrongSourceDirectory(_Directory):
            def __init__(self) -> None:
                super().__init__()
                self.close_calls = 0

            def _close_authority(self) -> None:
                self.close_calls += 1

        source = _FailingExistingRetirementSource(TypeError("not used"))
        rebound = _RetainedRetirement(_identity())
        service = _ExistingRetirementService(source, rebound)
        root = _Directory()
        try:
            wrong = WrongSourceDirectory()
            service.parent = wrong  # type: ignore[assignment]
            with self.assertRaisesRegex(
                TypeError,
                "RetirementSourceDirectoryAuthority",
            ):
                service.bind_retirement_source_directory(
                    root,
                    PurePosixPath("stage.sqlite3"),
                )
            self.assertTrue(wrong.closed)
            self.assertEqual(wrong.close_calls, 1)

            fault = _ExistingSourceParent(
                TypeError("source directory terminal programmer fault")
            )
            service.parent = fault
            with self.assertRaisesRegex(
                TypeError,
                "source directory terminal programmer fault",
            ):
                service.bind_retirement_source_directory(
                    root,
                    PurePosixPath("stage.sqlite3"),
                )
            self.assertTrue(fault.closed)
            self.assertEqual(fault.close_calls, 1)
        finally:
            if not service.parent.closed:
                service.parent.close()
            root.close()

    def test_transfer_state_fault_keeps_closed_reservation_owned_until_retained_close(self) -> None:
        identity = _identity()
        reservation = _TransferFaultReservation(identity)
        retained = _RetainedRetirement(identity)
        with self.assertRaisesRegex(KeyboardInterrupt, "after transfer state"):
            retained._accept_reservation_transfer(reservation)
        self.assertTrue(reservation.closed)
        reservation.close()
        self.assertEqual(reservation.close_calls, 0)
        retained.close()
        retained.close()
        self.assertEqual(retained.close_calls, 1)
        self.assertEqual(reservation.close_calls, 1)
        retained.close()
        reservation.close()
        self.assertEqual(retained.close_calls, 1)
        self.assertEqual(reservation.close_calls, 1)

    def test_mutable_reservation_reproves_exact_created_single_link_identity(self) -> None:
        created = _identity()
        reservation = _MutableReservation(created)
        self.assertEqual(reservation.identity(), created)
        self.assertEqual(reservation.reprove_calls, 1)

        reservation.observed = FileObjectIdentity(
            platform=created.platform,
            volume_id=created.volume_id,
            file_id=b"x" * 16,
            kind="regular",
            link_count=1,
        )
        with self.assertRaises(PlatformFileError) as caught:
            reservation.identity()
        self.assertEqual(
            caught.exception.code,
            PlatformFileErrorCode.IDENTITY_STALE.value,
        )

        reservation.observed = object()
        with self.assertRaises(TypeError):
            reservation.identity()
        reservation.close()
        reservation.close()
        self.assertEqual(reservation.close_calls, 1)
        with self.assertRaises(PlatformFileError) as caught:
            reservation.identity()
        self.assertEqual(
            caught.exception.code,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
        )

        for invalid in (
            FileObjectIdentity("windows", b"v", b"f" * 16, "directory", 1),
            FileObjectIdentity("windows", b"v", b"f" * 16, "regular", 2),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _MutableReservation(invalid)

    def test_regular_bounded_read_validates_shape_and_preserves_eof(self) -> None:
        regular = _Regular(b"0123456789")
        expected = regular.snapshot()
        self.assertEqual(regular.read_at(2, 4, expected), b"2345")
        self.assertEqual(regular.read_at(8, 4, expected), b"89")
        self.assertEqual(regular.read_at(10, 1, expected), b"")
        with self.assertRaises(ValueError):
            regular.read_at(20, 1, expected)

        for offset, maximum_bytes in (
            (-1, 1),
            (0, 0),
            (0, -1),
            (0, 64 * 1024 + 1),
        ):
            with self.subTest(offset=offset, maximum_bytes=maximum_bytes):
                with self.assertRaises(ValueError):
                    regular.read_at(offset, maximum_bytes, expected)
        for offset, maximum_bytes in (
            (True, 1),
            ("0", 1),
            (0, True),
            (0, "1"),
        ):
            with self.subTest(offset=offset, maximum_bytes=maximum_bytes):
                with self.assertRaises(TypeError):
                    regular.read_at(
                        offset,
                        maximum_bytes,
                        expected,
                    )  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            regular.read_at(0, 1, object())  # type: ignore[arg-type]

        class WrongType(_Regular):
            def _read_at(
                self,
                offset: int,
                maximum_bytes: int,
                expected: EntrySnapshot,
            ) -> bytes:
                del offset, maximum_bytes, expected
                return bytearray(b"x")  # type: ignore[return-value]

        class TooLong(_Regular):
            def _read_at(
                self,
                offset: int,
                maximum_bytes: int,
                expected: EntrySnapshot,
            ) -> bytes:
                del offset, expected
                return b"x" * (maximum_bytes + 1)

        class TooShort(_Regular):
            def _read_at(
                self,
                offset: int,
                maximum_bytes: int,
                expected: EntrySnapshot,
            ) -> bytes:
                del offset, maximum_bytes, expected
                return b""

        with self.assertRaises(TypeError):
            wrong = WrongType()
            wrong.read_at(0, 1, wrong.snapshot())
        with self.assertRaises(ValueError):
            too_long = TooLong()
            too_long.read_at(0, 1, too_long.snapshot())
        with self.assertRaises(ValueError):
            too_short = TooShort()
            too_short.read_at(0, 1, too_short.snapshot())

    def test_regular_read_all_uses_one_bounded_baseline_and_exact_eof_probe(self) -> None:
        payload = b"x" * (64 * 1024 + 17)
        regular = _Regular(payload)
        expected = regular.snapshot()

        self.assertEqual(regular.read_all(), payload)
        self.assertEqual(
            [(offset, maximum) for offset, maximum, _snapshot in regular.read_at_calls],
            [(0, 64 * 1024), (64 * 1024, 17), (len(payload), 1)],
        )
        self.assertTrue(
            all(snapshot == expected for _offset, _maximum, snapshot in regular.read_at_calls)
        )

    def test_regular_content_facts_stream_exact_bytes_and_are_live_only(self) -> None:
        payload = b"x" * (64 * 1024 + 17)
        regular = _Regular(payload)

        facts = regular.content_facts()

        self.assertEqual(facts.snapshot, regular.snapshot())
        self.assertEqual(facts.content_sha256, hashlib.sha256(payload).digest())
        self.assertEqual(
            [(offset, maximum) for offset, maximum, _snapshot in regular.read_at_calls],
            [(0, 64 * 1024), (64 * 1024, 17), (len(payload), 1)],
        )

    def test_synchronized_regular_requires_exact_pre_and_post_content(self) -> None:
        regular = _SynchronizedRegular(b"durable")
        expected = regular.content_facts()

        self.assertEqual(regular.synchronize_content(expected), expected)
        self.assertEqual(regular.synchronize_calls, 1)
        with self.assertRaises(TypeError):
            regular.synchronize_content(object())  # type: ignore[arg-type]

        stale = _SynchronizedRegular(b"before")
        stale_expected = stale.content_facts()
        stale.payload = b"after"
        with self.assertRaises(PlatformFileError) as caught:
            stale.synchronize_content(stale_expected)
        self.assertEqual(
            caught.exception.code,
            PlatformFileErrorCode.IDENTITY_STALE.value,
        )
        self.assertEqual(stale.synchronize_calls, 0)

    def test_persistent_private_proof_port_validates_and_mints_only_values(self) -> None:
        service = _PersistentPrivateService()
        secret_file = _Regular(b"s" * DEVICE_SECRET_SIZE_BYTES)
        target = service.issue_target()
        context = _private_context()

        secret = service.bind_device_secret(secret_file)
        secret_file.close()
        secret.reprove()
        proof = service.mint(target, secret, context)
        verified = service.verify(target, secret, proof, context)

        self.assertIs(type(proof), WindowsPrivateProof)
        self.assertNotIsInstance(proof, OpaqueAuthority)
        self.assertIsInstance(secret, DeviceSecretAuthority)
        self.assertIsInstance(verified, VerifiedPrivateProof)
        self.assertFalse(secret.retained.closed)
        self.assertEqual(
            (
                service.bind_calls,
                service.mint_calls,
                service.verify_calls,
                service.consume_calls,
            ),
            (1, 1, 1, 0),
        )
        retained = secret.retained
        secret.close()
        self.assertTrue(retained.closed)
        with self.assertRaises(PlatformFileError):
            secret.reprove()

    def test_persistent_private_proof_port_rejects_wrong_types_and_closed_inputs(self) -> None:
        service = _PersistentPrivateService()
        secret_file = _Regular(b"s" * DEVICE_SECRET_SIZE_BYTES)
        target = service.issue_target()
        context = _private_context()
        secret = service.bind_device_secret(secret_file)
        proof = service.mint(target, secret, context)

        invalid_calls = (
            lambda: service.bind_device_secret(object()),
            lambda: service.mint(object(), secret, context),
            lambda: service.mint(target, object(), context),
            lambda: service.mint(target, secret, object()),
            lambda: service.verify(object(), secret, proof, context),
            lambda: service.verify(target, object(), proof, context),
            lambda: service.verify(target, secret, object(), context),
            lambda: service.verify(target, secret, proof, object()),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(TypeError):
                    call()  # type: ignore[misc]
        self.assertEqual(
            (service.bind_calls, service.mint_calls, service.verify_calls),
            (1, 1, 0),
        )

        closed_file = _Regular(b"s" * DEVICE_SECRET_SIZE_BYTES)
        closed_file.close()
        with self.assertRaises(PlatformFileError):
            service.bind_device_secret(closed_file)
        target.close()
        with self.assertRaises(PlatformFileError):
            service.mint(target, secret, context)
        replacement_target = service.issue_target()
        secret.close()
        with self.assertRaises(PlatformFileError):
            service.verify(replacement_target, secret, proof, context)

    def test_persistent_private_proof_context_mismatch_fails_before_backend_verify(self) -> None:
        service = _PersistentPrivateService()
        target = service.issue_target()
        secret = service.bind_device_secret(_Regular(b"s" * DEVICE_SECRET_SIZE_BYTES))
        proof = service.mint(target, secret, _private_context())
        mismatches = (
            PrivateProofContext(PrivateProofObjectRole.DEVICE_KEY, b"c" * 32),
            PrivateProofContext(PrivateProofObjectRole.ATTESTATION, b"x" * 32),
        )
        for expected in mismatches:
            with self.subTest(expected=expected):
                with self.assertRaises(PlatformFileError) as caught:
                    service.verify(target, secret, proof, expected)
                self.assertEqual(
                    caught.exception.code,
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN.value,
                )
        self.assertEqual(service.verify_calls, 0)

    def test_persistent_private_proof_port_rejects_backend_shape_violations(self) -> None:
        service = _PersistentPrivateService()
        target = service.issue_target()
        secret_file = _Regular(b"s" * DEVICE_SECRET_SIZE_BYTES)
        context = _private_context()

        class BadBind(_PersistentPrivateService):
            def _bind_device_secret(self, secret_file: BoundRegularFile) -> DeviceSecretAuthority:
                del secret_file
                return object()  # type: ignore[return-value]

        with self.assertRaises(TypeError):
            BadBind().bind_device_secret(secret_file)

        class ClosedBind(_PersistentPrivateService):
            def _bind_device_secret(self, secret_file: BoundRegularFile) -> DeviceSecretAuthority:
                del secret_file
                result = _DeviceSecret(self._issuer)
                result.close()
                return result

        with self.assertRaises(PlatformFileError):
            ClosedBind().bind_device_secret(secret_file)

        secret = service.bind_device_secret(secret_file)

        class BadMint(_PersistentPrivateService):
            def _mint(
                self,
                target: PrivateAccessEvidence,
                secret: DeviceSecretAuthority,
                context: PrivateProofContext,
            ) -> WindowsPrivateProof:
                del target, secret, context
                return object()  # type: ignore[return-value]

        with self.assertRaises(TypeError):
            BadMint().mint(target, secret, context)

        class WrongContextMint(_PersistentPrivateService):
            def _mint(
                self,
                target: PrivateAccessEvidence,
                secret: DeviceSecretAuthority,
                context: PrivateProofContext,
            ) -> WindowsPrivateProof:
                del target, secret, context
                return _windows_private_proof(
                    PrivateProofContext(PrivateProofObjectRole.DEVICE_KEY, b"x" * 32)
                )

        with self.assertRaises(ValueError):
            wrong_service = WrongContextMint()
            wrong_service.mint(
                wrong_service.issue_target(),
                wrong_service.bind_device_secret(secret_file),
                context,
            )

        proof = service.mint(target, secret, context)

        class BadVerify(_PersistentPrivateService):
            def _verify(
                self,
                target: PrivateAccessEvidence,
                secret: DeviceSecretAuthority,
                proof: WindowsPrivateProof,
                expected_context: PrivateProofContext,
            ) -> VerifiedPrivateProof:
                del target, secret, proof
                return object()  # type: ignore[return-value]

        with self.assertRaises(TypeError):
            BadVerify().verify(target, secret, proof, context)

        class ClosedVerify(_PersistentPrivateService):
            def _verify(
                self,
                target: PrivateAccessEvidence,
                secret: DeviceSecretAuthority,
                proof: WindowsPrivateProof,
                expected_context: PrivateProofContext,
            ) -> VerifiedPrivateProof:
                del target, secret, proof
                result = _VerifiedPrivate(
                    self._issuer,
                    expected_context,
                    _DeviceSecret(self._issuer),
                )
                result.close()
                return result

        with self.assertRaises(PlatformFileError):
            ClosedVerify().verify(target, secret, proof, context)

        class BadConsume(_PersistentPrivateService):
            def _consume_verified(
                self,
                verified: VerifiedPrivateProof,
                expected_context: PrivateProofContext,
            ) -> None:
                del verified, expected_context
                return object()  # type: ignore[return-value]

        bad_consume = BadConsume()
        bad_target = bad_consume.issue_target()
        bad_secret = bad_consume.bind_device_secret(secret_file)
        bad_proof = bad_consume.mint(bad_target, bad_secret, context)
        bad_verified = bad_consume.verify(
            bad_target,
            bad_secret,
            bad_proof,
            context,
        )
        with self.assertRaises(TypeError):
            bad_consume.consume_verified(bad_verified, context)
        self.assertFalse(bad_verified.closed)
        bad_verified.close()

    def test_persistent_private_proof_rejects_foreign_and_forged_authorities(self) -> None:
        first = _PersistentPrivateService()
        second = _PersistentPrivateService()
        context = _private_context()
        first_target = first.issue_target()
        first_secret = first.bind_device_secret(_Regular(b"s" * DEVICE_SECRET_SIZE_BYTES))
        first_proof = first.mint(first_target, first_secret, context)
        first_verified = first.verify(
            first_target,
            first_secret,
            first_proof,
            context,
        )

        second_target = second.issue_target()
        second_secret = second.bind_device_secret(_Regular(b"s" * DEVICE_SECRET_SIZE_BYTES))
        second_proof = second.mint(second_target, second_secret, context)
        second_verified = second.verify(
            second_target,
            second_secret,
            second_proof,
            context,
        )

        for call in (
            lambda: second.mint(first_target, second_secret, context),
            lambda: second.mint(second_target, first_secret, context),
            lambda: second.verify(first_target, second_secret, second_proof, context),
            lambda: second.verify(second_target, first_secret, second_proof, context),
            lambda: second.consume_verified(first_verified, context),
            lambda: first.mint(second_target, first_secret, context),
            lambda: first.mint(first_target, second_secret, context),
            lambda: first.verify(second_target, first_secret, first_proof, context),
            lambda: first.verify(first_target, second_secret, first_proof, context),
            lambda: first.consume_verified(second_verified, context),
        ):
            with self.subTest(call=call):
                with self.assertRaises(PlatformFileError) as caught:
                    call()
                self.assertEqual(
                    caught.exception.code,
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN.value,
                )

        class ForgedTarget(_IssuedPrivateEvidence):
            pass

        class ForgedSecret(_DeviceSecret):
            pass

        class ForgedVerified(_VerifiedPrivate):
            pass

        class DispatchBypassVerified(_VerifiedPrivate):
            def __init__(
                self,
                issuer: object,
                context: PrivateProofContext,
                secret: _DeviceSecret,
            ) -> None:
                super().__init__(issuer, context, secret)
                self.dynamic_calls: list[str] = []

            def _consume_once(self, operation: object) -> None:
                del operation
                self.dynamic_calls.append("consume")

            def _require_open(self) -> None:
                self.dynamic_calls.append("require_open")

            def close(self) -> None:
                self.dynamic_calls.append("close")

        forged_target = ForgedTarget(first._issuer)
        forged_secret = ForgedSecret(first._issuer)
        forged_verified = ForgedVerified(first._issuer, context, first_secret)
        bypass_verified = DispatchBypassVerified(
            first._issuer,
            context,
            first_secret,
        )
        for call in (
            lambda: first.mint(_PrivateEvidence(), first_secret, context),
            lambda: first.mint(forged_target, first_secret, context),
            lambda: first.mint(first_target, forged_secret, context),
            lambda: first.consume_verified(forged_verified, context),
            lambda: first.consume_verified(bypass_verified, context),
        ):
            with self.subTest(call=call):
                with self.assertRaises(PlatformFileError) as caught:
                    call()
                self.assertEqual(
                    caught.exception.code,
                    PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN.value,
                )

        self.assertFalse(first_verified.closed)
        self.assertFalse(second_verified.closed)
        self.assertFalse(forged_verified.closed)
        self.assertEqual(bypass_verified.dynamic_calls, [])
        self.assertFalse(bypass_verified.closed)
        self.assertEqual(first.consume_calls, 0)
        self.assertEqual(second.consume_calls, 0)

        first.consume_verified(first_verified, context)
        second.consume_verified(second_verified, context)
        self.assertTrue(first_verified.closed)
        self.assertTrue(second_verified.closed)
        self.assertEqual(first.consume_calls, 1)
        self.assertEqual(second.consume_calls, 1)

        forged_verified.close()
        self.assertTrue(forged_verified.closed)
        self.assertTrue(forged_verified.retained_secret.closed)
        self.assertTrue(forged_verified.retained_target.closed)
        OpaqueAuthority.close(bypass_verified)
        self.assertTrue(bypass_verified.closed)

    def test_verified_private_proof_is_issuer_bound_one_shot_and_close_sensitive(self) -> None:
        service = _PersistentPrivateService()
        context = _private_context()
        target = service.issue_target()
        secret = service.bind_device_secret(_Regular(b"s" * DEVICE_SECRET_SIZE_BYTES))
        proof = service.mint(target, secret, context)
        verified = service.verify(target, secret, proof, context)

        retained_secret = verified.retained_secret
        retained_target = verified.retained_target
        service.consume_verified(verified, context)
        self.assertTrue(verified.closed)
        self.assertEqual(verified.terminal_reproof_calls, 1)
        self.assertTrue(retained_secret.closed)
        self.assertTrue(retained_secret.retained.closed)
        self.assertTrue(retained_target.closed)
        self.assertEqual(service.consume_calls, 1)
        with self.assertRaises(PlatformFileError):
            service.consume_verified(verified, context)
        self.assertEqual(service.consume_calls, 1)

        closed = service.verify(target, secret, proof, context)
        closed.close()
        with self.assertRaises(PlatformFileError):
            service.consume_verified(closed, context)
        self.assertEqual(service.consume_calls, 1)

        mismatch = service.verify(target, secret, proof, context)
        with self.assertRaises(PlatformFileError) as caught:
            service.consume_verified(
                mismatch,
                PrivateProofContext(PrivateProofObjectRole.ATTESTATION, b"x" * 32),
            )
        self.assertEqual(
            caught.exception.code,
            PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN.value,
        )
        self.assertFalse(mismatch.closed)
        self.assertFalse(mismatch.retained_secret.closed)
        self.assertFalse(mismatch.retained_target.closed)
        self.assertEqual(service.consume_calls, 1)
        service.consume_verified(mismatch, context)
        self.assertTrue(mismatch.closed)
        self.assertTrue(mismatch.retained_secret.closed)
        self.assertTrue(mismatch.retained_target.closed)
        self.assertEqual(service.consume_calls, 2)

        stale = service.verify(target, secret, proof, context)
        stale.retained_secret.retained.close()
        with self.assertRaises(PlatformFileError):
            service.consume_verified(stale, context)
        self.assertTrue(stale.closed)
        self.assertTrue(stale.retained_secret.closed)
        self.assertTrue(stale.retained_target.closed)
        self.assertEqual(stale.terminal_reproof_calls, 0)
        self.assertEqual(service.consume_calls, 2)

        public_verified_methods = {
            name
            for name, value in inspect.getmembers(
                VerifiedPrivateProof,
                inspect.isfunction,
            )
            if not name.startswith("_")
        }
        self.assertEqual(public_verified_methods, {"close"})

    def test_verified_private_proof_serializes_concurrent_consumers(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingService(_PersistentPrivateService):
            def _consume_verified(
                self,
                verified: VerifiedPrivateProof,
                expected_context: PrivateProofContext,
            ) -> None:
                self._require_verified(verified)
                if verified.context != expected_context:
                    raise PlatformFileError(
                        PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
                        retryable=False,
                    )

                def terminal_operation() -> None:
                    entered.set()
                    if not release.wait(timeout=2):
                        raise AssertionError("concurrent consumption test timed out")
                    verified.terminal_reproof()
                    self.consume_calls += 1

                VerifiedPrivateProof._consume_once(verified, terminal_operation)

        service = BlockingService()
        context = _private_context()
        target = service.issue_target()
        secret = service.bind_device_secret(_Regular(b"s" * DEVICE_SECRET_SIZE_BYTES))
        proof = service.mint(target, secret, context)
        verified = service.verify(target, secret, proof, context)
        outcomes: list[str] = []

        def consume() -> None:
            try:
                service.consume_verified(verified, context)
            except PlatformFileError as error:
                outcomes.append(error.code)
            else:
                outcomes.append("consumed")

        first = threading.Thread(target=consume)
        second = threading.Thread(target=consume)
        first.start()
        self.assertTrue(entered.wait(timeout=2))
        second.start()
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertCountEqual(
            outcomes,
            ["consumed", PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value],
        )
        self.assertTrue(verified.closed)
        self.assertEqual(verified.terminal_reproof_calls, 1)
        self.assertEqual(service.consume_calls, 1)

    def test_authority_is_context_managed_noncopyable_and_closed_once(self) -> None:
        authority = _ProbeAuthority()
        with authority as entered:
            self.assertIs(entered, authority)
            authority.touch()
        self.assertTrue(authority.closed)
        self.assertEqual(authority.close_calls, 1)
        authority.close()
        self.assertEqual(authority.close_calls, 1)
        for operation in (
            authority.touch,
            lambda: authority.__enter__(),
            lambda: pickle.dumps(authority),
            lambda: copy.copy(authority),
            lambda: copy.deepcopy(authority),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises((PlatformFileError, TypeError)):
                    operation()

    def test_lock_lease_reprove_is_public_and_requires_live_lease(self) -> None:
        lease = _Lease()
        self.assertIsNone(lease.reprove())
        self.assertEqual(lease.reprove_calls, 1)
        lease.close()
        with self.assertRaises(PlatformFileError) as caught:
            lease.reprove()
        self.assertEqual(
            caught.exception.code,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
        )

    def test_lock_lease_binding_reproof_validates_exact_live_inputs_and_return(self) -> None:
        lease = _Lease()
        parent = _Directory()

        self.assertIsNone(
            lease.reprove_binding(parent, "resource.lock", b"payload")
        )
        self.assertEqual(
            lease.binding_calls,
            [(parent, "resource.lock", b"payload")],
        )

        for call in (
            lambda: lease.reprove_binding(object(), "resource.lock", b"payload"),
            lambda: lease.reprove_binding(
                parent,
                PurePath("resource.lock"),  # type: ignore[arg-type]
                b"payload",
            ),
            lambda: lease.reprove_binding(
                parent,
                "resource.lock",
                bytearray(b"payload"),  # type: ignore[arg-type]
            ),
        ):
            with self.subTest(call=call), self.assertRaises(TypeError):
                call()
        for invalid in ("", ".", "..", "a/b", "a\\b", "nul\0name"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                lease.reprove_binding(parent, invalid, b"payload")
        self.assertEqual(len(lease.binding_calls), 1)

        closed_parent = _Directory()
        closed_parent.close()
        with self.assertRaises(PlatformFileError) as caught_parent:
            lease.reprove_binding(closed_parent, "resource.lock", b"payload")
        self.assertEqual(
            caught_parent.exception.code,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
        )

        class WrongReturn(_Lease):
            def _reprove_binding(
                self,
                parent: BoundDirectoryAuthority,
                name: str,
                payload: bytes,
            ) -> None:
                del parent, name, payload
                return object()  # type: ignore[return-value]

        with self.assertRaises(TypeError):
            WrongReturn().reprove_binding(parent, "resource.lock", b"payload")

        lease.close()
        with self.assertRaises(PlatformFileError) as caught_lease:
            lease.reprove_binding(parent, "resource.lock", b"payload")
        self.assertEqual(
            caught_lease.exception.code,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
        )

    def test_regular_candidate_and_pending_methods_fail_after_close(self) -> None:
        regular = _Regular()
        regular_snapshot = regular.snapshot()
        candidate = _Candidate()
        pending = _Pending(PublishMode.CREATE_IF_ABSENT)
        for authority, operations in (
            (
                regular,
                (
                    lambda: regular.read_at(0, 1, regular_snapshot),
                    regular.read_all,
                    regular.content_facts,
                    regular.identity,
                    regular.snapshot,
                ),
            ),
            (candidate, (lambda: candidate.write_all(b"x"), candidate.flush_content, candidate.identity)),
            (
                pending,
                (
                    pending.preliminary_facts,
                    pending.retained_destination,
                    pending.terminal_reproof,
                ),
            ),
        ):
            authority.close()
            for operation in operations:
                with self.subTest(authority=type(authority).__name__, operation=operation):
                    with self.assertRaises(PlatformFileError) as caught:
                        operation()
                    self.assertEqual(
                        caught.exception.code,
                        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
                    )

    def test_directory_and_lock_operations_fail_after_close(self) -> None:
        directory = _Directory()
        lease = _Lease()
        directory.close()
        for operation in (
            directory.reprove,
            lambda: directory.inspect_entry("entry.json"),
            lambda: directory.create_candidate("candidate.json", private=False),
            lambda: directory.unlink_owned("entry.json", _identity()),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(PlatformFileError) as caught:
                    operation()
                self.assertEqual(
                    caught.exception.code,
                    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
                )

        live_directory = _Directory()
        candidate = _Candidate()
        candidate.write_all(b"payload")
        candidate.flush_content()
        lease.close()
        with self.assertRaises(PlatformFileError) as caught:
            live_directory.begin_publish(
                candidate,
                "entry.json",
                mode=PublishMode.REPLACE_UNDER_LOCK,
                lease=lease,
            )
        self.assertEqual(
            caught.exception.code,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
        )

    def test_pending_retains_destination_until_pending_close(self) -> None:
        pending = _Pending(PublishMode.CREATE_IF_ABSENT)
        destination = pending.retained_destination()
        self.assertIs(pending.retained_destination(), destination)
        self.assertEqual(destination.read_all(), b"payload")
        self.assertEqual(pending.preliminary_facts(), _facts())
        self.assertEqual(pending.terminal_reproof(), _facts())
        self.assertIs(pending.asserted_destination, destination)
        self.assertEqual(pending.asserted_preliminary, _facts())
        pending.close()
        self.assertTrue(destination.closed)

    def test_pending_reproof_fails_if_retained_destination_is_closed_early(self) -> None:
        pending = _Pending(PublishMode.CREATE_IF_ABSENT)
        destination = pending.retained_destination()
        destination.close()
        for operation in (pending.preliminary_facts, pending.terminal_reproof):
            with self.subTest(operation=operation):
                with self.assertRaises(PlatformFileError) as caught:
                    operation()
                self.assertEqual(
                    caught.exception.code,
                    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
                )

    def test_begin_publish_requires_exact_mode_candidate_and_lock_combination(self) -> None:
        directory = _Directory()
        create_candidate = _Candidate()
        create_candidate.write_all(b"create")
        create_candidate.flush_content()
        replace_candidate = _Candidate()
        replace_candidate.write_all(b"replace")
        replace_candidate.flush_content()
        lease = _Lease()

        create = directory.begin_publish(
            create_candidate,
            "new.json",
            mode=PublishMode.CREATE_IF_ABSENT,
            lease=None,
        )
        self.assertIsInstance(create, PendingPublication)
        self.assertTrue(create_candidate.closed)
        self.assertEqual(
            directory.publish_events,
            [("renamed", False), ("reopened", True), ("pending_created", True)],
        )
        directory.publish_events.clear()
        replace = directory.begin_publish(
            replace_candidate,
            "existing.json",
            mode=PublishMode.REPLACE_UNDER_LOCK,
            lease=lease,
        )
        self.assertIsInstance(replace, PendingPublication)
        self.assertTrue(replace_candidate.closed)
        self.assertEqual(
            directory.publish_events,
            [("renamed", False), ("reopened", True), ("pending_created", True)],
        )
        self.assertEqual(directory.begin_calls, 2)

        for candidate in (create_candidate, replace_candidate):
            with self.subTest(candidate=candidate):
                with self.assertRaises(PlatformFileError):
                    candidate.write_all(b"after")

        candidate = _Candidate()
        candidate.write_all(b"invalid-combinations")
        candidate.flush_content()
        invalid_calls = (
            lambda: directory.begin_publish(
                candidate,
                "new.json",
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=lease,
            ),
            lambda: directory.begin_publish(
                candidate,
                "old.json",
                mode=PublishMode.REPLACE_UNDER_LOCK,
                lease=None,
            ),
            lambda: directory.begin_publish(
                object(),  # type: ignore[arg-type]
                "old.json",
                mode=PublishMode.REPLACE_UNDER_LOCK,
                lease=lease,
            ),
            lambda: directory.begin_publish(
                candidate,
                "old.json",
                mode="replace_under_lock",  # type: ignore[arg-type]
                lease=lease,
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises((TypeError, ValueError)):
                    call()
        self.assertEqual(directory.begin_calls, 2)

        candidate.close()
        with self.assertRaises(PlatformFileError):
            directory.begin_publish(
                candidate,
                "closed.json",
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )

    def test_begin_publish_rejects_unwritten_unflushed_and_reused_candidates(self) -> None:
        directory = _Directory()
        unwritten = _Candidate()
        written = _Candidate()
        written.write_all(b"payload")
        for candidate in (unwritten, written):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    directory.begin_publish(
                        candidate,
                        "entry.json",
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
        with self.assertRaises(ValueError):
            unwritten.flush_content()

        written.flush_content()
        directory.begin_publish(
            written,
            "entry.json",
            mode=PublishMode.CREATE_IF_ABSENT,
            lease=None,
        )
        with self.assertRaises(PlatformFileError):
            directory.begin_publish(
                written,
                "again.json",
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )

    def test_candidate_backend_fault_invalidates_prior_publishable_state(self) -> None:
        directory = _Directory()

        write_fault = _FaultingCandidate()
        write_fault.fail_write = True
        with self.assertRaises(RuntimeError):
            write_fault.write_all(b"first")
        with self.assertRaises(ValueError):
            directory.begin_publish(
                write_fault,
                "write-fault.json",
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )

        flush_fault = _FaultingCandidate()
        flush_fault.write_all(b"payload")
        flush_fault.flush_content()
        flush_fault.fail_flush = True
        with self.assertRaises(RuntimeError):
            flush_fault.flush_content()
        with self.assertRaises(ValueError):
            directory.begin_publish(
                flush_fault,
                "flush-fault.json",
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )

    def test_candidate_stream_is_one_shot_ordered_bounded_and_write_all_delegates(self) -> None:
        candidate = _Candidate()
        iterated: list[bytes] = []

        def chunks() -> object:
            for chunk in (b"first", b"second"):
                iterated.append(chunk)
                yield chunk

        facts = candidate.write_chunks(chunks(), maximum_bytes=11)  # type: ignore[arg-type]
        self.assertEqual(iterated, [b"first", b"second"])
        self.assertEqual(candidate.payload, b"firstsecond")
        self.assertEqual(facts.byte_count, 11)
        self.assertEqual(facts.content_sha256, hashlib.sha256(b"firstsecond").digest())
        candidate.flush_content()
        with self.assertRaises(ValueError):
            candidate.write_chunks((b"again",), maximum_bytes=5)

        compatibility = _Candidate()
        self.assertIsNone(compatibility.write_all(b"payload"))
        self.assertEqual(compatibility.payload, b"payload")

        empty_stream = _Candidate()
        empty_facts = empty_stream.write_chunks((), maximum_bytes=0)
        self.assertEqual(empty_stream.payload, b"")
        self.assertEqual(empty_facts.byte_count, 0)
        self.assertEqual(empty_facts.content_sha256, hashlib.sha256(b"").digest())
        empty_stream.flush_content()

        empty_compatibility = _Candidate()
        self.assertIsNone(empty_compatibility.write_all(b""))
        self.assertEqual(empty_compatibility.payload, b"")
        empty_compatibility.flush_content()

        invalid_streams = (
            ((b"",), 0),
            ((b"x" * (64 * 1024 + 1),), 64 * 1024 + 1),
            ((b"over",), 3),
            ((bytearray(b"not exact"),), 9),
        )
        for stream, maximum in invalid_streams:
            with self.subTest(stream=stream):
                with self.assertRaises((TypeError, ValueError)):
                    _Candidate().write_chunks(stream, maximum_bytes=maximum)  # type: ignore[arg-type]

    def test_ledger_observations_are_limited_sorted_and_require_live_lease(self) -> None:
        directory = _Directory()
        lease = _Lease()
        first = LedgerEntryObservation(
            "a.json",
            EntrySnapshot(_identity(), 3, b"a", True),
        )
        second = LedgerEntryObservation(
            "b.json",
            EntrySnapshot(
                FileObjectIdentity("windows", b"volume", b"g" * 16, "regular", 1),
                4,
                b"b",
                True,
            ),
        )
        directory.observations = (first, second)
        limits = LedgerEnumerationLimits(2, 16, 7)
        self.assertEqual(directory.observe_ledger_entries(lease, limits), (first, second))

        directory.observations = (second, first)
        with self.assertRaises(ValueError):
            directory.observe_ledger_entries(lease, limits)
        lease.close()
        with self.assertRaises(PlatformFileError):
            directory.observe_ledger_entries(lease, limits)

    def test_authority_operations_validate_names_before_backend_calls(self) -> None:
        directory = _Directory()
        for invalid in ("", ".", "..", "a/b", "a\\b", "nul\0name"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    directory.inspect_entry(invalid)
                with self.assertRaises(ValueError):
                    directory.create_candidate(invalid, private=False)
                with self.assertRaises(ValueError):
                    directory.unlink_owned(invalid, _identity())
        self.assertEqual(validate_relative_name("valid.json"), "valid.json")
        with self.assertRaises(TypeError):
            validate_relative_name(PurePath("valid.json"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            directory.create_candidate("candidate.json", private=1)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            directory.unlink_owned("valid.json", object())  # type: ignore[arg-type]

    def test_lock_policy_has_exact_wait_timeout_combinations(self) -> None:
        self.assertEqual(LockPolicy(LockWait.FAIL_FAST).timeout_seconds, None)
        self.assertEqual(LockPolicy(LockWait.BLOCK).timeout_seconds, None)
        self.assertEqual(LockPolicy(LockWait.TIMEOUT, 1.5).timeout_seconds, 1.5)
        for args in (
            (LockWait.FAIL_FAST, 1.0),
            (LockWait.BLOCK, 1.0),
            (LockWait.TIMEOUT, None),
            (LockWait.TIMEOUT, 0.0),
            (LockWait.TIMEOUT, math.inf),
            ("timeout", 1.0),
        ):
            with self.subTest(args=args):
                with self.assertRaises((TypeError, ValueError)):
                    LockPolicy(*args)  # type: ignore[arg-type]

    def test_service_facades_validate_authorities_paths_types_and_returns(self) -> None:
        filesystem = _FileSystem()
        root = filesystem.bind_root(_ROOT)
        regular = filesystem.open_regular(root, PurePath("nested", "source.json"))
        synchronized = filesystem.open_existing_for_synchronization(
            root,
            PurePath("nested", "source.json"),
        )
        mutation_guard = filesystem.guard_existing_for_mutation(
            root,
            PurePath("nested", "source.json"),
        )
        parent = filesystem.bind_parent(root, PurePath("nested", "source.json"))
        reservation = filesystem.reserve_mutable_file(parent, "stage.sqlite3")
        self.assertEqual(regular.read_all(), b"payload")
        synchronized_facts = synchronized.content_facts()
        self.assertEqual(
            synchronized.synchronize_content(synchronized_facts),
            synchronized_facts,
        )
        self.assertEqual(mutation_guard.reprove(), _identity())
        mutation_guard.close()
        parent.reprove()
        self.assertEqual(reservation.identity(), _identity())
        self.assertFalse(reservation.closed)
        reservation.close()

        for invalid in ("", ".", "..", "a/b", "a\\b", "nul\0name"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                filesystem.reserve_mutable_file(parent, invalid)
        with self.assertRaises(TypeError):
            filesystem.reserve_mutable_file(object(), "stage.sqlite3")  # type: ignore[arg-type]

        for relative in (
            PurePath(),
            PurePath("..", "escape.json"),
            PurePath("nested", "..", "escape.json"),
            PurePosixPath("a\\b"),
        ):
            with self.subTest(relative=relative):
                with self.assertRaises((TypeError, ValueError)):
                    validate_relative_path(relative)
                with self.assertRaises((TypeError, ValueError)):
                    filesystem.open_regular(root, relative)
                with self.assertRaises((TypeError, ValueError)):
                    filesystem.open_existing_for_synchronization(root, relative)
                with self.assertRaises((TypeError, ValueError)):
                    filesystem.guard_existing_for_mutation(root, relative)
        with self.assertRaises(TypeError):
            filesystem.open_regular(root, "source.json")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            filesystem.open_existing_for_synchronization(
                root,
                "source.json",  # type: ignore[arg-type]
            )
        with self.assertRaises(TypeError):
            filesystem.guard_existing_for_mutation(
                root,
                "source.json",  # type: ignore[arg-type]
            )

        closed_root = filesystem.bind_root(_ROOT)
        closed_root.close()
        with self.assertRaises(PlatformFileError):
            filesystem.bind_parent(closed_root, PurePath("source.json"))
        with self.assertRaises(PlatformFileError):
            filesystem.open_existing_for_synchronization(
                closed_root,
                PurePath("source.json"),
            )
        with self.assertRaises(PlatformFileError):
            filesystem.guard_existing_for_mutation(
                closed_root,
                PurePath("source.json"),
            )

        lock_service = _LockService()
        lease = lock_service.acquire(
            parent,
            "resource.lock",
            b"payload",
            LockPolicy(LockWait.FAIL_FAST),
        )
        self.assertFalse(lease.closed)
        for call in (
            lambda: lock_service.acquire(
                parent,
                "../lock",
                b"payload",
                LockPolicy(LockWait.FAIL_FAST),
            ),
            lambda: lock_service.acquire(
                parent,
                "resource.lock",
                bytearray(b"payload"),  # type: ignore[arg-type]
                LockPolicy(LockWait.FAIL_FAST),
            ),
            lambda: lock_service.acquire(
                parent,
                "resource.lock",
                b"payload",
                "fail_fast",  # type: ignore[arg-type]
            ),
        ):
            with self.subTest(call=call):
                with self.assertRaises((TypeError, ValueError)):
                    call()

        private_service = _PrivateService()
        private_directory = private_service.create_private_directory(parent, "private")
        evidence = private_service.prove_private(private_directory)
        self.assertFalse(evidence.closed)
        private_directory.close()
        with self.assertRaises(PlatformFileError):
            private_service.prove_private(private_directory)


if __name__ == "__main__":
    unittest.main()
