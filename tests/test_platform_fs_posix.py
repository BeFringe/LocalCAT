from __future__ import annotations

import ast
import builtins
import errno
import hashlib
import importlib.util
import inspect
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from platform_fs_contracts import (
    LedgerEnumerationLimits,
    LockPolicy,
    LockWait,
    OpaqueAuthority,
    PlatformFileError,
    PlatformFileErrorCode,
    PublishMode,
)


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "platform_fs_posix.py"
CONTRACTS_PATH = ROOT / "platform_fs_contracts.py"


class PosixAdapterStaticBoundaryTests(unittest.TestCase):
    @staticmethod
    def _load_with_fake_fcntl(flock: object) -> types.ModuleType:
        fake_fcntl = types.ModuleType("fcntl")
        fake_fcntl.LOCK_EX = 1
        fake_fcntl.LOCK_NB = 2
        fake_fcntl.LOCK_UN = 4
        fake_fcntl.flock = flock
        module_name = "_isolated_platform_fs_posix"
        spec = importlib.util.spec_from_file_location(module_name, ADAPTER_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"fcntl": fake_fcntl}), mock.patch.object(
            os,
            "O_NOFOLLOW",
            0,
            create=True,
        ), mock.patch.object(os, "O_DIRECTORY", 0, create=True):
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(module_name, None)
        return module

    def test_adapter_contains_required_primitives_and_contracts_remain_neutral(self) -> None:
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_from = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        self.assertIn("fcntl", imports | imported_from)
        for token in (
            "fcntl.flock",
            "dir_fd=",
            "O_NOFOLLOW",
            "O_DIRECTORY",
            "os.pread(",
            "os.fsync(",
        ):
            self.assertIn(token, source)

        contracts_source = CONTRACTS_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import fcntl", contracts_source)
        self.assertNotIn("platform_fs_posix", contracts_source)

    def test_contract_import_does_not_attempt_to_load_posix_backend(self) -> None:
        original_import = builtins.__import__

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "fcntl" or name.startswith("platform_fs_posix"):
                raise AssertionError("neutral contract imported the POSIX backend")
            return original_import(name, *args, **kwargs)

        module_name = "_isolated_platform_fs_contracts"
        module = types.ModuleType(module_name)
        module.__dict__["__builtins__"] = dict(
            vars(builtins),
            __import__=guarded_import,
        )
        sys.modules[module_name] = module
        try:
            exec(
                compile(CONTRACTS_PATH.read_bytes(), str(CONTRACTS_PATH), "exec"),
                module.__dict__,
            )
        finally:
            sys.modules.pop(module_name, None)

    def test_adapter_has_no_business_receipt_or_expected_target_cas_surface(self) -> None:
        tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"))
        public_methods = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")
        }
        self.assertFalse(
            public_methods
            & {
                "commit_business_receipt",
                "issue_receipt",
                "expected_target_cas",
                "mark_business_success",
            }
        )

    def test_streaming_and_ledger_observation_remain_fd_bound(self) -> None:
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        self.assertIn("os.scandir(self._directory_fd)", source)
        self.assertIn("os.pwrite(", source)
        self.assertIn("os.pread(", source)
        self.assertNotIn("Path.iterdir", source)
        self.assertNotIn("Path.glob", source)

    def test_existing_file_durability_is_rooted_and_does_not_publish(self) -> None:
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        adapter = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "PosixPlatformAdapter"
        )
        self.assertIn(
            "ExistingFileDurability",
            {
                base.id
                for base in adapter.bases
                if isinstance(base, ast.Name)
            },
        )
        opener = next(
            node
            for node in adapter.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_open_existing_for_synchronization"
        )
        opener_source = ast.get_source_segment(source, opener)
        assert opener_source is not None
        self.assertIn("_open_directory_chain", opener_source)
        self.assertIn("_SYNCHRONIZED_READ_FLAGS", opener_source)
        self.assertIn("dir_fd=", opener_source)

        authority = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "_PosixBoundSynchronizedRegularFile"
        )
        synchronize = next(
            node
            for node in authority.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_synchronize_content"
        )
        synchronize_source = ast.get_source_segment(source, synchronize)
        assert synchronize_source is not None
        self.assertEqual(synchronize_source.count("os.fsync("), 2)
        self.assertNotIn("os.replace(", synchronize_source)
        self.assertNotIn("os.rename(", synchronize_source)
        self.assertNotIn("os.unlink(", synchronize_source)

        module = self._load_with_fake_fcntl(lambda descriptor, operation: None)
        self.assertFalse(
            inspect.isabstract(module._PosixBoundSynchronizedRegularFile)
        )

    def test_flock_errno_mapping_executes_without_posix_host_import(self) -> None:
        def raising(error_number: int) -> object:
            def flock(descriptor: int, operation: int) -> None:
                del descriptor, operation
                raise OSError(error_number, "injected flock fault")

            return flock

        for error_number in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            with self.subTest(error_number=error_number):
                module = self._load_with_fake_fcntl(raising(error_number))
                with self.assertRaises(PlatformFileError) as caught:
                    module.PosixPlatformAdapter._flock(
                        7,
                        LockPolicy(LockWait.FAIL_FAST),
                    )
                self.assertEqual(caught.exception.code, "PLATFORM.FS.LOCK_CONTENDED")

        module = self._load_with_fake_fcntl(raising(errno.EIO))
        with self.assertRaises(PlatformFileError) as caught:
            module.PosixPlatformAdapter._flock(7, LockPolicy(LockWait.FAIL_FAST))
        self.assertEqual(caught.exception.code, "PLATFORM.FS.LOCK_UNAVAILABLE")

        module = self._load_with_fake_fcntl(raising(errno.EWOULDBLOCK))
        with mock.patch.object(module.time, "monotonic", side_effect=[0.0, 1.0]):
            with self.assertRaises(PlatformFileError) as caught:
                module.PosixPlatformAdapter._flock(
                    7,
                    LockPolicy(LockWait.TIMEOUT, timeout_seconds=0.5),
                )
        self.assertEqual(caught.exception.code, "PLATFORM.FS.LOCK_CONTENDED")

        identity_error = PlatformFileError(
            PlatformFileErrorCode.IDENTITY_STALE,
            retryable=True,
        )
        normalized = module._normalize_lock_platform_error(identity_error)
        self.assertEqual(normalized.code, "PLATFORM.FS.LOCK_UNAVAILABLE")

    def test_unlock_fault_closes_all_descriptors_and_maps_stable_error(self) -> None:
        def fail_unlock(descriptor: int, operation: int) -> None:
            del descriptor
            if operation == 4:
                raise OSError(errno.EIO, "injected unlock fault")

        module = self._load_with_fake_fcntl(fail_unlock)
        lease = module._PosixLockLease(
            101,
            (201, 202),
            (None, "child"),
            (object(), object()),
            "lock",
            object(),
            b"payload",
        )
        with mock.patch.object(module, "_close_fd") as close_fd:
            with self.assertRaises(PlatformFileError) as caught:
                lease.close()
        self.assertTrue(lease.closed)
        self.assertEqual(
            caught.exception.code,
            PlatformFileErrorCode.LOCK_UNAVAILABLE.value,
        )
        self.assertEqual(
            caught.exception.args,
            (PlatformFileErrorCode.LOCK_UNAVAILABLE.value,),
        )
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(
            [call.args[0] for call in close_fd.call_args_list],
            [101, 202, 201],
        )

    def test_lock_binding_reproof_uses_retained_chain_name_and_payload(self) -> None:
        module = self._load_with_fake_fcntl(lambda descriptor, operation: None)

        def parent(
            fds: tuple[int, ...],
            names: tuple[str | None, ...],
            identities: tuple[object, ...],
        ):
            result = object.__new__(module._PosixBoundDirectory)
            OpaqueAuthority.__init__(result)
            result._directory_fds = fds
            result._directory_names = names
            result._directory_identities = identities
            result._fault_injector = None
            return result

        expected_parent = parent((201, 202), (None, "child"), (object(), object()))
        other_parent = parent((301,), (None,), (object(),))
        lease = module._PosixLockLease(
            101,
            (401, 402),
            (None, "child"),
            (object(), object()),
            "resource.lock",
            object(),
            b"payload",
        )

        with mock.patch.object(
            module._PosixLockLease,
            "_matches_parent",
            return_value=True,
        ) as matches:
            self.assertIsNone(
                lease.reprove_binding(expected_parent, "resource.lock", b"payload")
            )
        matches.assert_called_once_with(
            expected_parent._directory_fds,
            expected_parent._directory_names,
            expected_parent._directory_identities,
        )

        for name, payload in (
            ("other.lock", b"payload"),
            ("resource.lock", b"other"),
        ):
            with self.subTest(name=name, payload=payload), self.assertRaises(
                PlatformFileError
            ) as caught:
                lease.reprove_binding(expected_parent, name, payload)
            self.assertEqual(
                caught.exception.code,
                PlatformFileErrorCode.LOCK_UNAVAILABLE.value,
            )

        with mock.patch.object(
            module._PosixLockLease,
            "_matches_parent",
            return_value=False,
        ), self.assertRaises(PlatformFileError) as caught_parent:
            lease.reprove_binding(other_parent, "resource.lock", b"payload")
        self.assertEqual(
            caught_parent.exception.code,
            PlatformFileErrorCode.LOCK_UNAVAILABLE.value,
        )

        for programming_error in (TypeError("type"), AssertionError("assert")):
            with self.subTest(programming_error=type(programming_error).__name__), mock.patch.object(
                module._PosixLockLease,
                "_matches_parent",
                side_effect=programming_error,
            ), self.assertRaises(type(programming_error)):
                lease.reprove_binding(
                    expected_parent,
                    "resource.lock",
                    b"payload",
                )


@unittest.skipUnless(os.name == "posix", "POSIX runtime evidence requires macOS or Linux")
class PosixAdapterRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from platform_fs_posix import PosixPlatformAdapter

        cls.adapter_type = PosixPlatformAdapter

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temporary.name)
        (self.root_path / "data").mkdir()
        self.adapter = self.adapter_type()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_rooted_read_uses_retained_handles_and_rejects_symlink(self) -> None:
        source = self.root_path / "data" / "source.txt"
        source.write_bytes(b"sealed source")
        root = self.adapter.bind_root(self.root_path)
        bound = self.adapter.open_regular(root, PurePosixPath("data/source.txt"))
        root.close()

        expected = bound.snapshot()
        self.assertEqual(bound.read_at(0, 6, expected), b"sealed")
        self.assertEqual(bound.read_at(7, 64 * 1024, expected), b"source")
        self.assertEqual(bound.read_at(len(b"sealed source"), 1, expected), b"")
        self.assertEqual(bound.read_all(), b"sealed source")
        self.assertEqual(
            bound.content_facts().content_sha256,
            hashlib.sha256(b"sealed source").digest(),
        )
        self.assertEqual(bound.identity().platform, "posix")
        self.assertEqual(bound.snapshot().byte_count, len(b"sealed source"))
        bound.close()
        with self.assertRaises(PlatformFileError):
            bound.read_at(0, 1, expected)
        with self.assertRaises(PlatformFileError):
            bound.read_all()

        os.symlink(source, self.root_path / "data" / "source-link.txt")
        with self.adapter.bind_root(self.root_path) as another_root:
            with self.assertRaises(PlatformFileError) as caught:
                self.adapter.open_regular(
                    another_root,
                    PurePosixPath("data/source-link.txt"),
                )
        self.assertEqual(caught.exception.code, "PLATFORM.FS.REPARSE_REJECTED")

    def test_existing_regular_synchronization_flushes_file_then_parent(self) -> None:
        import platform_fs_posix

        payload = b"sqlite stage bytes"
        source = self.root_path / "data" / "stage.sqlite"
        source.write_bytes(payload)
        root = self.adapter.bind_root(self.root_path)
        authority = self.adapter.open_existing_for_synchronization(
            root,
            PurePosixPath("data/stage.sqlite"),
        )
        root.close()
        try:
            expected = authority.content_facts()
            self.assertEqual(expected.snapshot.byte_count, len(payload))
            self.assertEqual(
                expected.content_sha256,
                hashlib.sha256(payload).digest(),
            )
            real_fsync = platform_fs_posix.os.fsync
            with mock.patch.object(
                platform_fs_posix.os,
                "fsync",
                wraps=real_fsync,
            ) as fsync:
                synchronized = authority.synchronize_content(expected)
            self.assertEqual(synchronized, expected)
            self.assertEqual(
                [call.args[0] for call in fsync.call_args_list],
                [authority._descriptor, authority._directory_fds[-1]],
            )
            self.assertEqual(source.read_bytes(), payload)
        finally:
            authority.close()

    def test_existing_regular_synchronization_accepts_read_only_owner_artifact(
        self,
    ) -> None:
        payload = b"sealed read-only stage"
        source = self.root_path / "data" / "stage.sqlite"
        source.write_bytes(payload)
        source.chmod(0o444)
        with self.adapter.bind_root(self.root_path) as root:
            authority = self.adapter.open_existing_for_synchronization(
                root,
                PurePosixPath("data/stage.sqlite"),
            )
        try:
            expected = authority.content_facts()
            self.assertEqual(authority.synchronize_content(expected), expected)
            self.assertEqual(source.read_bytes(), payload)
        finally:
            authority.close()

    def test_existing_regular_synchronization_rejects_alias_and_stale_name(self) -> None:
        import platform_fs_posix

        source = self.root_path / "data" / "stage.sqlite"
        source.write_bytes(b"stage")
        alias = self.root_path / "data" / "stage-alias.sqlite"
        os.link(source, alias)
        with self.adapter.bind_root(self.root_path) as root:
            with self.assertRaises(PlatformFileError) as hardlink:
                self.adapter.open_existing_for_synchronization(
                    root,
                    PurePosixPath("data/stage.sqlite"),
                )
        self.assertEqual(
            hardlink.exception.code,
            PlatformFileErrorCode.IDENTITY_STALE.value,
        )
        alias.unlink()

        with self.adapter.bind_root(self.root_path) as root:
            authority = self.adapter.open_existing_for_synchronization(
                root,
                PurePosixPath("data/stage.sqlite"),
            )
        expected = authority.content_facts()
        os.link(source, alias)
        try:
            with self.assertRaises(PlatformFileError) as linked_after_open:
                authority.content_facts()
            self.assertEqual(
                linked_after_open.exception.code,
                PlatformFileErrorCode.IDENTITY_STALE.value,
            )
        finally:
            alias.unlink()
        os.replace(source, self.root_path / "data" / "stage-owned.sqlite")
        source.write_bytes(b"foreign")
        try:
            with mock.patch.object(
                platform_fs_posix.os,
                "fsync",
                wraps=platform_fs_posix.os.fsync,
            ) as fsync, self.assertRaises(PlatformFileError) as stale:
                authority.synchronize_content(expected)
            self.assertEqual(
                stale.exception.code,
                PlatformFileErrorCode.IDENTITY_STALE.value,
            )
            fsync.assert_not_called()
            self.assertEqual(source.read_bytes(), b"foreign")
        finally:
            authority.close()

    def test_existing_regular_synchronization_maps_fsync_failure(self) -> None:
        import platform_fs_posix

        source = self.root_path / "data" / "stage.sqlite"
        source.write_bytes(b"stage")
        with self.adapter.bind_root(self.root_path) as root:
            authority = self.adapter.open_existing_for_synchronization(
                root,
                PurePosixPath("data/stage.sqlite"),
            )
        try:
            expected = authority.content_facts()
            with mock.patch.object(
                platform_fs_posix.os,
                "fsync",
                side_effect=OSError(errno.EIO, "injected durability fault"),
            ), self.assertRaises(PlatformFileError) as caught:
                authority.synchronize_content(expected)
            self.assertEqual(
                caught.exception.code,
                PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value,
            )
            self.assertFalse(caught.exception.retryable)
            self.assertIsNone(caught.exception.__cause__)
        finally:
            authority.close()

    def test_bounded_read_rejects_wrong_baseline_and_short_or_zero_pread(self) -> None:
        import platform_fs_posix

        source = self.root_path / "data" / "source.txt"
        source.write_bytes(b"sealed source")
        with self.adapter.bind_root(self.root_path) as root:
            bound = self.adapter.open_regular(root, PurePosixPath("data/source.txt"))
        expected = bound.snapshot()
        wrong = type(expected)(
            identity=expected.identity,
            byte_count=expected.byte_count + 1,
            modified_token=expected.modified_token,
            reparse_free=expected.reparse_free,
        )
        real_pread = platform_fs_posix.os.pread
        try:
            with mock.patch.object(
                platform_fs_posix.os,
                "pread",
                wraps=real_pread,
            ) as pread, self.assertRaises(PlatformFileError) as stale:
                bound.read_at(0, 6, wrong)
            self.assertEqual(stale.exception.code, PlatformFileErrorCode.IDENTITY_STALE.value)
            pread.assert_not_called()

            for injected in (b"sea", b""):
                with self.subTest(injected=injected), mock.patch.object(
                    platform_fs_posix.os,
                    "pread",
                    return_value=injected,
                ), self.assertRaises(PlatformFileError) as short:
                    bound.read_at(0, 6, expected)
                self.assertEqual(
                    short.exception.code,
                    PlatformFileErrorCode.IDENTITY_STALE.value,
                )
        finally:
            bound.close()

    def test_final_entry_missing_and_access_denied_are_entry_unavailable(self) -> None:
        import platform_fs_posix

        denied = self.root_path / "data" / "denied.txt"
        denied.write_bytes(b"denied")
        with self.adapter.bind_root(self.root_path) as root:
            with self.assertRaises(PlatformFileError) as missing:
                self.adapter.open_regular(root, PurePosixPath("data/missing.txt"))
            self.assertEqual(
                missing.exception.code,
                PlatformFileErrorCode.ENTRY_UNAVAILABLE.value,
            )

            real_open = platform_fs_posix.os.open

            def deny_final(path: object, flags: int, *args: object, **kwargs: object) -> int:
                if path == "denied.txt":
                    raise PermissionError(errno.EACCES, "denied")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                platform_fs_posix.os,
                "open",
                side_effect=deny_final,
            ), self.assertRaises(PlatformFileError) as denied_error:
                self.adapter.open_regular(root, PurePosixPath("data/denied.txt"))
            self.assertEqual(
                denied_error.exception.code,
                PlatformFileErrorCode.ENTRY_UNAVAILABLE.value,
            )

    def test_retained_file_rejects_ancestor_move_and_replacement(self) -> None:
        nested = self.root_path / "data" / "nested"
        nested.mkdir()
        (nested / "source.txt").write_bytes(b"bound")
        root = self.adapter.bind_root(self.root_path)
        bound = self.adapter.open_regular(
            root,
            PurePosixPath("data/nested/source.txt"),
        )
        root.close()
        os.replace(self.root_path / "data", self.root_path / "data-owned")
        replacement = self.root_path / "data" / "nested"
        replacement.mkdir(parents=True)
        (replacement / "source.txt").write_bytes(b"foreign")
        with self.assertRaises(PlatformFileError) as caught:
            bound.read_all()
        self.assertEqual(caught.exception.code, "PLATFORM.FS.IDENTITY_STALE")
        bound.close()

    def test_create_if_absent_publish_retains_exact_readback_authority(self) -> None:
        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                candidate = parent.create_candidate(".item.candidate", private=False)
                candidate.write_all(b"candidate bytes")
                candidate.flush_content()
                pending = parent.begin_publish(
                    candidate,
                    "item.bin",
                    mode=PublishMode.CREATE_IF_ABSENT,
                    lease=None,
                )
                self.assertTrue(candidate.closed)
                preliminary = pending.preliminary_facts()
                retained = pending.retained_destination()
                self.assertEqual(retained.read_all(), b"candidate bytes")
                terminal = pending.terminal_reproof()
                self.assertEqual(terminal, preliminary)
                self.assertFalse(hasattr(pending, "business_receipt"))
                pending.close()
                self.assertTrue(retained.closed)

    def test_candidate_streams_ordered_bounded_chunks(self) -> None:
        chunks = (b"a" * 65536, b"b" * 17, b"c" * 4096)
        generated: list[int] = []

        def stream() -> object:
            for chunk in chunks:
                generated.append(len(chunk))
                yield chunk

        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                candidate = parent.create_candidate(".stream.tmp", private=False)
                facts = candidate.write_chunks(  # type: ignore[arg-type]
                    stream(),
                    maximum_bytes=sum(map(len, chunks)),
                )
                candidate.flush_content()
                candidate.close()
        payload = b"".join(chunks)
        self.assertEqual(generated, [65536, 17, 4096])
        self.assertEqual(facts.byte_count, len(payload))
        self.assertEqual((self.root_path / "data" / ".stream.tmp").read_bytes(), payload)

    def test_locked_ledger_observation_is_bounded_and_rejects_unsafe_children(self) -> None:
        data = self.root_path / "data"
        (data / "alpha.json").write_bytes(b"alpha")
        (data / "beta.json").write_bytes(b"beta")
        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                lease = self.adapter.acquire(
                    parent,
                    ".ledger.lock",
                    b"ledger-v1",
                    LockPolicy(LockWait.FAIL_FAST),
                )
                try:
                    observations = parent.observe_ledger_entries(
                        lease,
                        LedgerEnumerationLimits(16, 1024, 1024 * 1024),
                    )
                    names = tuple(item.name for item in observations)
                    self.assertEqual(names, tuple(sorted(names)))
                    self.assertIn("alpha.json", names)
                    self.assertIn("beta.json", names)

                    with self.assertRaises(PlatformFileError) as limited:
                        parent.observe_ledger_entries(
                            lease,
                            LedgerEnumerationLimits(1, 1024, 1024 * 1024),
                        )
                    self.assertEqual(
                        limited.exception.code,
                        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
                    )

                    unsafe = data / "unsafe-dir"
                    unsafe.mkdir()
                    try:
                        with self.assertRaises(PlatformFileError) as rejected:
                            parent.observe_ledger_entries(
                                lease,
                                LedgerEnumerationLimits(16, 1024, 1024 * 1024),
                            )
                        self.assertEqual(
                            rejected.exception.code,
                            PlatformFileErrorCode.REPARSE_REJECTED.value,
                        )
                    finally:
                        unsafe.rmdir()

                    alias = data / "alpha-alias.json"
                    os.link(data / "alpha.json", alias)
                    try:
                        with self.assertRaises(PlatformFileError) as hardlink:
                            parent.observe_ledger_entries(
                                lease,
                                LedgerEnumerationLimits(16, 1024, 1024 * 1024),
                            )
                        self.assertEqual(
                            hardlink.exception.code,
                            PlatformFileErrorCode.IDENTITY_STALE.value,
                        )
                    finally:
                        alias.unlink()

                    hostile_name = data / "a\\b.json"
                    hostile_name.write_bytes(b"hostile")
                    try:
                        with self.assertRaises(PlatformFileError) as invalid_name:
                            parent.observe_ledger_entries(
                                lease,
                                LedgerEnumerationLimits(16, 1024, 1024 * 1024),
                            )
                        self.assertEqual(
                            invalid_name.exception.code,
                            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
                        )
                    finally:
                        hostile_name.unlink()
                finally:
                    lease.close()

    def test_locked_ledger_observation_reproves_live_lease_at_terminal(self) -> None:
        data = self.root_path / "data"
        (data / "alpha.json").write_bytes(b"alpha")
        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                lease = self.adapter.acquire(
                    parent,
                    ".ledger-terminal.lock",
                    b"ledger-terminal-v1",
                    LockPolicy(LockWait.FAIL_FAST),
                )
                real_scandir = os.scandir
                closed = False

                def close_during_first_scan(path: object):
                    nonlocal closed
                    if not closed:
                        lease.close()
                        closed = True
                    return real_scandir(path)

                with mock.patch.object(
                    os,
                    "scandir",
                    side_effect=close_during_first_scan,
                ), self.assertRaises(PlatformFileError) as caught:
                    parent.observe_ledger_entries(
                        lease,
                        LedgerEnumerationLimits(16, 1024, 1024 * 1024),
                    )
                self.assertEqual(
                    caught.exception.code,
                    PlatformFileErrorCode.LOCK_UNAVAILABLE.value,
                )
                self.assertTrue(closed)

    def test_replace_requires_live_same_parent_posix_lease(self) -> None:
        destination = self.root_path / "data" / "item.bin"
        destination.write_bytes(b"old")
        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                lease = self.adapter.acquire(
                    parent,
                    ".item.lock",
                    b"item-lock-v1",
                    LockPolicy(LockWait.FAIL_FAST),
                )
                candidate = parent.create_candidate(".item.next", private=False)
                candidate.write_all(b"new")
                candidate.flush_content()
                with parent.begin_publish(
                    candidate,
                    "item.bin",
                    mode=PublishMode.REPLACE_UNDER_LOCK,
                    lease=lease,
                ) as pending:
                    self.assertEqual(pending.retained_destination().read_all(), b"new")
                    self.assertEqual(
                        pending.terminal_reproof(),
                        pending.preliminary_facts(),
                    )
                lease.close()
        self.assertEqual(destination.read_bytes(), b"new")

    def test_candidate_and_lease_retain_full_chain_after_parent_closes(self) -> None:
        destination = self.root_path / "data" / "item.bin"
        destination.write_bytes(b"old")
        root = self.adapter.bind_root(self.root_path)
        parent = self.adapter.bind_parent(root, PurePosixPath("data/item.bin"))
        candidate = parent.create_candidate(".item.next", private=False)
        candidate.write_all(b"new")
        candidate.flush_content()
        lease = self.adapter.acquire(
            parent,
            ".item.lock",
            b"item-lock-v1",
            LockPolicy(LockWait.FAIL_FAST),
        )
        parent.close()
        root.close()

        self.assertEqual(candidate.identity().kind, "regular")
        with self.adapter.bind_root(self.root_path) as rebound_root:
            with self.adapter.bind_parent(
                rebound_root,
                PurePosixPath("data/item.bin"),
            ) as rebound_parent:
                with rebound_parent.begin_publish(
                    candidate,
                    "item.bin",
                    mode=PublishMode.REPLACE_UNDER_LOCK,
                    lease=lease,
                ) as pending:
                    self.assertEqual(pending.retained_destination().read_all(), b"new")
                    pending.terminal_reproof()
        lease.close()
        self.assertEqual(destination.read_bytes(), b"new")

    def test_flock_contention_and_release_are_process_lock_facts_only(self) -> None:
        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                policy = LockPolicy(LockWait.FAIL_FAST)
                first = self.adapter.acquire(parent, ".resource.lock", b"v1", policy)
                with self.assertRaises(PlatformFileError) as caught:
                    self.adapter.acquire(parent, ".resource.lock", b"v1", policy)
                self.assertEqual(caught.exception.code, "PLATFORM.FS.LOCK_CONTENDED")
                first.close()
                second = self.adapter.acquire(parent, ".resource.lock", b"v1", policy)
                second.close()

    def test_flock_is_exclusive_across_processes(self) -> None:
        probe = """
from pathlib import Path, PurePosixPath
import sys
from platform_fs_contracts import LockPolicy, LockWait, PlatformFileError
from platform_fs_posix import PosixPlatformAdapter
adapter = PosixPlatformAdapter()
with adapter.bind_root(Path(sys.argv[1])) as root:
    with adapter.bind_parent(root, PurePosixPath('data/item.bin')) as parent:
        try:
            lease = adapter.acquire(
                parent, '.process.lock', b'process-v1',
                LockPolicy(LockWait.FAIL_FAST),
            )
        except PlatformFileError as error:
            print(error.code)
            raise SystemExit(17)
        lease.close()
raise SystemExit(0)
"""
        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                lease = self.adapter.acquire(
                    parent,
                    ".process.lock",
                    b"process-v1",
                    LockPolicy(LockWait.FAIL_FAST),
                )
                contended = subprocess.run(
                    [sys.executable, "-c", probe, str(self.root_path)],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(contended.returncode, 17, contended.stderr)
                self.assertEqual(
                    contended.stdout.strip(),
                    "PLATFORM.FS.LOCK_CONTENDED",
                )
                lease.close()
        released = subprocess.run(
            [sys.executable, "-c", probe, str(self.root_path)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_hardlinked_lock_is_rejected_before_any_mode_mutation(self) -> None:
        victim = self.root_path / "data" / "victim"
        victim.write_bytes(b"v1")
        victim.chmod(0o640)
        os.link(victim, self.root_path / "data" / ".resource.lock")
        before_mode = victim.stat().st_mode
        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                with self.assertRaises(PlatformFileError) as caught:
                    self.adapter.acquire(
                        parent,
                        ".resource.lock",
                        b"v1",
                        LockPolicy(LockWait.FAIL_FAST),
                    )
        self.assertEqual(caught.exception.code, "PLATFORM.FS.LOCK_UNAVAILABLE")
        self.assertEqual(victim.stat().st_mode, before_mode)

    def test_lock_zero_link_and_base_exception_release_owned_authority(self) -> None:
        lock_path = self.root_path / "data" / ".resource.lock"

        def unlink_before_initial_proof(point: str) -> None:
            if point == "lock_before_initial_proof":
                lock_path.unlink()

        adapter = self.adapter_type(_fault_injector=unlink_before_initial_proof)
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                with self.assertRaises(PlatformFileError) as caught:
                    adapter.acquire(
                        parent,
                        ".resource.lock",
                        b"v1",
                        LockPolicy(LockWait.FAIL_FAST),
                    )
        self.assertEqual(caught.exception.code, "PLATFORM.FS.LOCK_UNAVAILABLE")

        def unlink_after_lease(point: str) -> None:
            if point == "lock_after_lease":
                lock_path.unlink()

        adapter = self.adapter_type(_fault_injector=unlink_after_lease)
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                with self.assertRaises(PlatformFileError) as caught:
                    adapter.acquire(
                        parent,
                        ".resource.lock",
                        b"v1",
                        LockPolicy(LockWait.FAIL_FAST),
                    )
        self.assertEqual(caught.exception.code, "PLATFORM.FS.LOCK_UNAVAILABLE")

        def interrupt_after_lease(point: str) -> None:
            if point == "lock_after_lease":
                raise KeyboardInterrupt()

        adapter = self.adapter_type(_fault_injector=interrupt_after_lease)
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                with self.assertRaises(KeyboardInterrupt):
                    adapter.acquire(
                        parent,
                        ".resource.lock",
                        b"v1",
                        LockPolicy(LockWait.FAIL_FAST),
                    )

        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                lease = self.adapter.acquire(
                    parent,
                    ".resource.lock",
                    b"v1",
                    LockPolicy(LockWait.FAIL_FAST),
                )
                lease.close()

    def test_replaced_lock_entry_cannot_authorize_publish(self) -> None:
        destination = self.root_path / "data" / "item.bin"
        destination.write_bytes(b"old")
        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                lease = self.adapter.acquire(
                    parent,
                    ".resource.lock",
                    b"v1",
                    LockPolicy(LockWait.FAIL_FAST),
                )
                os.replace(
                    self.root_path / "data" / ".resource.lock",
                    self.root_path / "data" / ".resource.lock.replaced",
                )
                replacement = self.root_path / "data" / ".resource.lock"
                replacement.write_bytes(b"v1")
                replacement.chmod(0o600)
                second = self.adapter.acquire(
                    parent,
                    ".resource.lock",
                    b"v1",
                    LockPolicy(LockWait.FAIL_FAST),
                )
                first_candidate = parent.create_candidate(".item.first", private=False)
                first_candidate.write_all(b"first")
                first_candidate.flush_content()
                with self.assertRaises(PlatformFileError) as caught:
                    parent.begin_publish(
                        first_candidate,
                        "item.bin",
                        mode=PublishMode.REPLACE_UNDER_LOCK,
                        lease=lease,
                    )
                self.assertEqual(caught.exception.code, "PLATFORM.FS.LOCK_UNAVAILABLE")
                second_candidate = parent.create_candidate(".item.second", private=False)
                second_candidate.write_all(b"second")
                second_candidate.flush_content()
                with parent.begin_publish(
                    second_candidate,
                    "item.bin",
                    mode=PublishMode.REPLACE_UNDER_LOCK,
                    lease=second,
                ) as pending:
                    self.assertEqual(pending.retained_destination().read_all(), b"second")
                    pending.terminal_reproof()
                lease.close()
                second.close()
        self.assertEqual(destination.read_bytes(), b"second")

    def test_candidate_rejects_hardlink_delete_and_name_replacement(self) -> None:
        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                hardlinked = parent.create_candidate(".hardlinked", private=False)
                os.link(
                    self.root_path / "data" / ".hardlinked",
                    self.root_path / "data" / ".hardlinked-alias",
                )
                with self.assertRaises(PlatformFileError) as caught:
                    hardlinked.write_all(b"blocked")
                self.assertEqual(caught.exception.code, "PLATFORM.FS.IDENTITY_STALE")
                hardlinked.close()

                deleted = parent.create_candidate(".deleted", private=False)
                os.unlink(self.root_path / "data" / ".deleted")
                with self.assertRaises(PlatformFileError) as caught:
                    deleted.identity()
                self.assertEqual(caught.exception.code, "PLATFORM.FS.IDENTITY_STALE")
                deleted.close()

                replaced = parent.create_candidate(".replaced", private=False)
                os.replace(
                    self.root_path / "data" / ".replaced",
                    self.root_path / "data" / ".replaced-owned",
                )
                (self.root_path / "data" / ".replaced").write_bytes(b"foreign")
                with self.assertRaises(PlatformFileError) as caught:
                    replaced.flush_content()
                self.assertEqual(caught.exception.code, "PLATFORM.FS.IDENTITY_STALE")
                self.assertEqual(
                    (self.root_path / "data" / ".replaced").read_bytes(),
                    b"foreign",
                )
                replaced.close()

    def test_candidate_setup_failure_removes_only_the_owned_entry(self) -> None:
        adapter = self.adapter_type(
            _fault_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("fault"))
                if point == "candidate_after_create"
                else None
            )
        )
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                with self.assertRaises(PlatformFileError) as caught:
                    parent.create_candidate(".candidate", private=False)
        self.assertEqual(caught.exception.code, "PLATFORM.FS.PUBLISH_FAILED")
        self.assertFalse((self.root_path / "data" / ".candidate").exists())

    def test_candidate_base_exception_cleanup_and_swap_are_conservative(self) -> None:
        foreign = self.root_path / "data" / ".foreign"
        foreign.write_bytes(b"foreign")

        def inject(point: str) -> None:
            if point == "candidate_after_create":
                raise KeyboardInterrupt()

        adapter = self.adapter_type(_fault_injector=inject)
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                with self.assertRaises(KeyboardInterrupt):
                    parent.create_candidate(".base-exception", private=False)
        self.assertFalse((self.root_path / "data" / ".base-exception").exists())

        def swap_then_fail(point: str) -> None:
            if point == "candidate_after_create":
                raise RuntimeError("fault")
            if point == "candidate_cleanup_before_final_reproof":
                os.replace(
                    self.root_path / "data" / ".swap",
                    self.root_path / "data" / ".swap-owned",
                )
                os.replace(foreign, self.root_path / "data" / ".swap")

        adapter = self.adapter_type(_fault_injector=swap_then_fail)
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                with self.assertRaises(PlatformFileError) as caught:
                    parent.create_candidate(".swap", private=False)
        self.assertEqual(caught.exception.code, "PLATFORM.FS.RECOVERY_REQUIRED")
        self.assertEqual((self.root_path / "data" / ".swap").read_bytes(), b"foreign")
        self.assertTrue((self.root_path / "data" / ".swap-owned").exists())

    def test_post_naming_swap_returns_recovery_and_preserves_foreign_entry(self) -> None:
        foreign = self.root_path / "data" / ".foreign-published"
        foreign.write_bytes(b"foreign")

        def inject(point: str) -> None:
            if point == "candidate_after_naming":
                os.replace(
                    self.root_path / "data" / "item.bin",
                    self.root_path / "data" / "item-owned.bin",
                )
                os.replace(foreign, self.root_path / "data" / "item.bin")

        adapter = self.adapter_type(_fault_injector=inject)
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                candidate = parent.create_candidate(".item.next", private=False)
                candidate.write_all(b"owned")
                candidate.flush_content()
                with self.assertRaises(PlatformFileError) as caught:
                    parent.begin_publish(
                        candidate,
                        "item.bin",
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
        self.assertEqual(caught.exception.code, "PLATFORM.FS.RECOVERY_REQUIRED")
        self.assertEqual((self.root_path / "data" / "item.bin").read_bytes(), b"foreign")
        self.assertEqual(
            (self.root_path / "data" / "item-owned.bin").read_bytes(),
            b"owned",
        )

    def test_private_directory_setup_failure_removes_empty_owned_entry(self) -> None:
        adapter = self.adapter_type(
            _fault_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("fault"))
                if point == "private_after_create"
                else None
            )
        )
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                with self.assertRaises(PlatformFileError) as caught:
                    adapter.create_private_directory(parent, "private-fault")
        self.assertEqual(caught.exception.code, "PLATFORM.FS.PRIVATE_STORAGE_UNPROVEN")
        self.assertFalse((self.root_path / "data" / "private-fault").exists())

    def test_private_chain_transfer_closes_every_fd_on_base_exception(self) -> None:
        def interrupt(point: str) -> None:
            if point == "private_before_authority_transfer":
                raise KeyboardInterrupt()

        adapter = self.adapter_type(_fault_injector=interrupt)
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                original_close = os.close
                with mock.patch(
                    "platform_fs_posix.os.close",
                    wraps=original_close,
                ) as close_spy:
                    with self.assertRaises(KeyboardInterrupt):
                        adapter.create_private_directory(parent, "private-transfer")
                self.assertEqual(close_spy.call_count, 3)
        self.assertFalse((self.root_path / "data" / "private-transfer").exists())

    def test_unlink_owned_maps_unexpected_fault_and_preserves_entry(self) -> None:
        def fail(point: str) -> None:
            if point == "candidate_cleanup_before_final_reproof":
                raise RuntimeError("fault")

        adapter = self.adapter_type(_fault_injector=fail)
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                candidate = parent.create_candidate(".owned", private=False)
                identity = candidate.identity()
                with self.assertRaises(PlatformFileError) as caught:
                    parent.unlink_owned(".owned", identity)
                self.assertEqual(caught.exception.code, "PLATFORM.FS.PUBLISH_FAILED")
                self.assertTrue((self.root_path / "data" / ".owned").exists())
                candidate.close()

    def test_private_directory_binds_name_and_cleanup_preserves_swap(self) -> None:
        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                private = self.adapter.create_private_directory(parent, "private")
                os.replace(
                    self.root_path / "data" / "private",
                    self.root_path / "data" / "private-owned",
                )
                (self.root_path / "data" / "private").mkdir()
                with self.assertRaises(PlatformFileError) as caught:
                    private.reprove()
                self.assertEqual(caught.exception.code, "PLATFORM.FS.IDENTITY_STALE")
                private.close()

        foreign = self.root_path / "data" / "foreign-private"
        foreign.mkdir()

        def inject(point: str) -> None:
            if point == "private_after_create":
                raise RuntimeError("fault")
            if point == "private_cleanup_before_final_reproof":
                os.replace(
                    self.root_path / "data" / "private-swap",
                    self.root_path / "data" / "private-swap-owned",
                )
                os.replace(foreign, self.root_path / "data" / "private-swap")

        adapter = self.adapter_type(_fault_injector=inject)
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                with self.assertRaises(PlatformFileError) as caught:
                    adapter.create_private_directory(parent, "private-swap")
        self.assertEqual(caught.exception.code, "PLATFORM.FS.RECOVERY_REQUIRED")
        self.assertTrue((self.root_path / "data" / "private-swap").is_dir())
        self.assertTrue((self.root_path / "data" / "private-swap-owned").is_dir())

    def test_post_open_reproof_failure_closes_transferred_descriptors(self) -> None:
        descriptor_inventory = next(
            path for path in (Path("/proc/self/fd"), Path("/dev/fd")) if path.is_dir()
        )
        adapter = self.adapter_type(
            _fault_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("fault"))
                if point == "published_after_open"
                else None
            )
        )
        with adapter.bind_root(self.root_path) as root:
            with adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                candidate = parent.create_candidate(".item.candidate", private=False)
                candidate.write_all(b"candidate")
                candidate.flush_content()
                before = len(tuple(descriptor_inventory.iterdir()))
                with self.assertRaises(PlatformFileError) as caught:
                    parent.begin_publish(
                        candidate,
                        "item.bin",
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
                self.assertEqual(caught.exception.code, "PLATFORM.FS.RECOVERY_REQUIRED")
                after = len(tuple(descriptor_inventory.iterdir()))
                self.assertLessEqual(after, before)

    def test_bound_parent_constructor_oserror_is_stable_and_leak_free(self) -> None:
        from platform_fs_posix import _capture_directory_identities

        descriptor_inventory = next(
            path for path in (Path("/proc/self/fd"), Path("/dev/fd")) if path.is_dir()
        )
        calls = 0

        def fail_constructor(descriptors: tuple[int, ...]) -> object:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.EIO, "injected constructor fault")
            return _capture_directory_identities(descriptors)

        with self.adapter.bind_root(self.root_path) as root:
            before = len(tuple(descriptor_inventory.iterdir()))
            with mock.patch(
                "platform_fs_posix._capture_directory_identities",
                side_effect=fail_constructor,
            ):
                with self.assertRaises(PlatformFileError) as caught:
                    self.adapter.bind_parent(root, PurePosixPath("data/item.bin"))
            after = len(tuple(descriptor_inventory.iterdir()))
        self.assertEqual(
            caught.exception.code,
            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,
        )
        self.assertEqual(
            caught.exception.args,
            (PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value,),
        )
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(calls, 2)
        self.assertLessEqual(after, before)

    def test_duplicate_fd_closes_partial_transfer_on_base_exception(self) -> None:
        from platform_fs_posix import _duplicate_fd

        descriptor = os.open(self.root_path, os.O_RDONLY)
        try:
            original_close = os.close
            with mock.patch(
                "platform_fs_posix.os.set_inheritable",
                side_effect=KeyboardInterrupt(),
            ), mock.patch(
                "platform_fs_posix.os.close",
                wraps=original_close,
            ) as close_spy:
                with self.assertRaises(KeyboardInterrupt):
                    _duplicate_fd(descriptor)
                close_spy.assert_called_once()
        finally:
            os.close(descriptor)

    def test_private_proof_is_opaque_and_closes_independently(self) -> None:
        with self.adapter.bind_root(self.root_path) as root:
            with self.adapter.bind_parent(root, PurePosixPath("data/item.bin")) as parent:
                private = self.adapter.create_private_directory(parent, "private")
                evidence = self.adapter.prove_private(private)
                private.close()
                self.assertFalse(evidence.closed)
                self.assertFalse(hasattr(evidence, "receipt"))
                evidence.close()
                self.assertTrue(evidence.closed)


if __name__ == "__main__":
    unittest.main()
