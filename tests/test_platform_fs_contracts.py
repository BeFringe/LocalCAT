"""Task 2.1 contract guards for the backend-neutral platform file boundary."""

from __future__ import annotations

import ast
import copy
from dataclasses import FrozenInstanceError, fields
import inspect
import math
from pathlib import Path, PurePath, PurePosixPath
import pickle
import unittest

from platform_fs_contracts import (
    BoundDirectoryAuthority,
    BoundRegularFile,
    CandidateFile,
    EntrySnapshot,
    FileObjectIdentity,
    LockLease,
    LockPolicy,
    LockWait,
    OpaqueAuthority,
    PendingPublication,
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateStorageProof,
    PrivateAccessEvidence,
    ProcessFileLock,
    PublishFacts,
    PublishMode,
    RootedDirectoryAuthority,
    RootedFileSystem,
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

    def _close_authority(self) -> None:
        pass

    def _read_all(self) -> bytes:
        return self.payload

    def _identity(self) -> FileObjectIdentity:
        return _identity()

    def _snapshot(self) -> EntrySnapshot:
        return EntrySnapshot(_identity(), len(self.payload), b"token", True)


class _Candidate(CandidateFile):
    def __init__(self) -> None:
        super().__init__()
        self.payload = b""
        self.flushed = False

    def _close_authority(self) -> None:
        pass

    def _write_all(self, payload: bytes) -> None:
        self.payload = payload

    def _flush_content(self) -> None:
        self.flushed = True

    def _identity(self) -> FileObjectIdentity:
        return _identity()


class _Lease(LockLease):
    def _close_authority(self) -> None:
        pass


class _PrivateEvidence(PrivateAccessEvidence):
    def _close_authority(self) -> None:
        pass


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

    def _close_authority(self) -> None:
        pass

    def _reprove(self) -> None:
        pass

    def _inspect_entry(self, name: str) -> EntrySnapshot | None:
        del name
        return None

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


class _FileSystem(RootedFileSystem):
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
            {"__future__", "abc", "dataclasses", "enum", "math", "pathlib", "typing"},
        )
        source = (_ROOT / "platform_fs_contracts.py").read_text(encoding="utf-8")
        for forbidden in ("fcntl", "ctypes", "msvcrt", "win32", "HANDLE", "dirfd"):
            self.assertNotIn(forbidden, source)

    def test_service_types_are_runtime_opaque_authorities(self) -> None:
        for authority_type in (
            RootedDirectoryAuthority,
            BoundDirectoryAuthority,
            BoundRegularFile,
            CandidateFile,
            LockLease,
            PendingPublication,
            PrivateAccessEvidence,
        ):
            with self.subTest(authority_type=authority_type.__name__):
                self.assertTrue(issubclass(authority_type, OpaqueAuthority))

    def test_service_port_method_shapes_do_not_expose_backend_representation(self) -> None:
        expected_parameters = {
            RootedFileSystem.bind_root: ("self", "root"),
            RootedFileSystem.open_regular: ("self", "root", "relative"),
            RootedFileSystem.bind_parent: ("self", "root", "relative"),
            ProcessFileLock.acquire: ("self", "parent", "name", "payload", "policy"),
            PrivateStorageProof.create_private_directory: ("self", "parent", "name"),
            PrivateStorageProof.prove_private: ("self", "authority"),
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


class PlatformFileValueContractTests(unittest.TestCase):
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
        facts = _facts()
        self.assertEqual(snapshot.byte_count, 7)
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

    def test_publish_mode_is_closed(self) -> None:
        self.assertEqual(
            tuple(mode.value for mode in PublishMode),
            ("create_if_absent", "replace_under_lock"),
        )


class PlatformFileErrorContractTests(unittest.TestCase):
    def test_error_family_codes_and_retryability_are_stable_and_body_free(self) -> None:
        fixed = {
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE: False,
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


class PlatformFileAuthorityContractTests(unittest.TestCase):
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

    def test_regular_candidate_and_pending_methods_fail_after_close(self) -> None:
        regular = _Regular()
        candidate = _Candidate()
        pending = _Pending(PublishMode.CREATE_IF_ABSENT)
        for authority, operations in (
            (regular, (regular.read_all, regular.identity, regular.snapshot)),
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
        parent = filesystem.bind_parent(root, PurePath("nested", "source.json"))
        self.assertEqual(regular.read_all(), b"payload")
        parent.reprove()

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
        with self.assertRaises(TypeError):
            filesystem.open_regular(root, "source.json")  # type: ignore[arg-type]

        closed_root = filesystem.bind_root(_ROOT)
        closed_root.close()
        with self.assertRaises(PlatformFileError):
            filesystem.bind_parent(closed_root, PurePath("source.json"))

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
