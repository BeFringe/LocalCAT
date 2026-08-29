"""Task 2.4 fail-closed composition and backend self-probe guards."""

from __future__ import annotations

import ast
import importlib
import os
from pathlib import Path
import sys
import tempfile
import types
from typing import get_protocol_members
import unittest
from unittest import mock

from platform_fs_contracts import (
    BoundDirectoryAuthority,
    ExistingFileDurability,
    MutableFileReservationService,
    PersistentPrivateProof,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateStorageProof,
    ProcessFileLock,
    RootedFileSystem,
)
import platform_fs


ROOT = Path(__file__).resolve().parents[1]
COMPOSITION_PATH = ROOT / "platform_fs.py"
POSIX_RUNTIME = sys.platform in {"darwin", "linux"}


def _assert_stable_error(
    case: unittest.TestCase,
    caught: unittest.case._AssertRaisesContext[PlatformFileError],
    code: PlatformFileErrorCode = PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
) -> None:
    error = caught.exception
    case.assertEqual(error.code, code.value)
    case.assertEqual(error.args, (code.value,))
    case.assertIsNone(error.__cause__)


class _ProbeAuthority(BoundDirectoryAuthority):
    def __init__(self, *, fail_reproof: bool = False) -> None:
        super().__init__()
        self.fail_reproof = fail_reproof
        self.close_calls = 0

    def _close_authority(self) -> None:
        self.close_calls += 1

    def _reprove(self) -> None:
        if self.fail_reproof:
            raise RuntimeError("probe fault")

    def _inspect_entry(self, name: str) -> object:
        del name
        return None

    def _observe_ledger_entries(self, lease: object, limits: object) -> tuple[object, ...]:
        del lease, limits
        return ()

    def _create_candidate(self, name: str, *, private: bool) -> object:
        del name, private
        raise AssertionError("composition must not create a candidate")

    def _begin_publish(
        self,
        candidate: object,
        destination: str,
        *,
        mode: object,
        lease: object,
    ) -> object:
        del candidate, destination, mode, lease
        raise AssertionError("composition must not publish")

    def _rename_candidate(self, candidate: object, destination: str, **kwargs: object) -> object:
        del candidate, destination, kwargs
        raise AssertionError("composition must not publish")

    def _open_published_destination(self, destination: str, facts: object) -> object:
        del destination, facts
        raise AssertionError("composition must not publish")

    def _create_pending_publication(self, facts: object, retained: object) -> object:
        del facts, retained
        raise AssertionError("composition must not publish")

    def _unlink_owned(self, name: str, expected: object) -> None:
        del name, expected
        raise AssertionError("composition must not unlink")


class _FaultingBackend:
    """Protocol-shaped failure double; never accepted as a successful authority."""

    def __init__(
        self,
        authority: _ProbeAuthority | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.authority = authority
        self.error = error

    def bind_root(self, root: Path) -> _ProbeAuthority:
        del root
        if self.error is not None:
            raise self.error
        assert self.authority is not None
        return self.authority

    _bind_root = bind_root

    def open_regular(self, root: object, relative: object) -> object:
        del root, relative
        raise AssertionError

    _open_regular = open_regular

    def bind_parent(self, root: object, relative: object) -> object:
        del root, relative
        raise AssertionError

    _bind_parent = bind_parent

    def open_existing_for_synchronization(
        self,
        root: object,
        relative: object,
    ) -> object:
        del root, relative
        raise AssertionError

    _open_existing_for_synchronization = open_existing_for_synchronization

    def reserve_mutable_file(self, parent: object, name: str) -> object:
        del parent, name
        raise AssertionError

    _reserve_mutable_file = reserve_mutable_file

    def acquire(self, parent: object, name: str, payload: bytes, policy: object) -> object:
        del parent, name, payload, policy
        raise AssertionError

    _acquire = acquire

    def create_private_directory(self, parent: object, name: str) -> object:
        del parent, name
        raise AssertionError

    _create_private_directory = create_private_directory

    def prove_private(self, authority: object) -> object:
        del authority
        raise AssertionError

    _prove_private = prove_private


class _PersistentFaultingBackend(_FaultingBackend):
    """Aggregate + persistent shape used only to exercise composition gates."""

    def bind_device_secret(self, secret_file: object) -> object:
        del secret_file
        raise AssertionError

    _bind_device_secret = bind_device_secret

    def mint(self, target: object, secret: object, context: object) -> object:
        del target, secret, context
        raise AssertionError

    _mint = mint

    def verify(
        self,
        target: object,
        secret: object,
        proof: object,
        expected_context: object,
    ) -> object:
        del target, secret, proof, expected_context
        raise AssertionError

    _verify = verify

    def consume_verified(self, verified: object, expected_context: object) -> None:
        del verified, expected_context
        raise AssertionError

    _consume_verified = consume_verified


class _ConstructorFault:
    def __init__(self) -> None:
        raise RuntimeError("constructor fault")


def _module_with_backend(factory: type[object]) -> types.ModuleType:
    module = types.ModuleType("platform_fs_posix")
    module.PosixPlatformAdapter = factory
    return module


def _windows_module_with_backend(factory: type[object]) -> types.ModuleType:
    module = types.ModuleType("platform_fs_windows")
    module.WindowsPlatformAdapter = factory
    return module


def _fake_fcntl() -> types.ModuleType:
    module = types.ModuleType("fcntl")
    module.flock = lambda *args: None
    module.LOCK_EX = 1
    module.LOCK_NB = 2
    module.LOCK_UN = 4
    return module


def _imports_for(module: types.ModuleType):
    def load(name: str) -> types.ModuleType:
        if name == "fcntl":
            return _fake_fcntl()
        if name == "platform_fs_posix":
            return module
        raise AssertionError(name)

    return load


def _complete_posix_os() -> types.SimpleNamespace:
    fake = types.SimpleNamespace()
    names = set(platform_fs._POSIX_CALLABLES)
    names.update(platform_fs._POSIX_DIR_FD_CALLABLES)
    names.update(platform_fs._POSIX_FOLLOW_SYMLINK_CALLABLES)
    for name in names:
        setattr(fake, name, lambda *args, **kwargs: None)
    for index, name in enumerate(platform_fs._POSIX_CONSTANTS, start=1):
        setattr(fake, name, index)
    fake.supports_dir_fd = {
        getattr(fake, name) for name in platform_fs._POSIX_DIR_FD_CALLABLES
    }
    fake.supports_follow_symlinks = {
        getattr(fake, name) for name in platform_fs._POSIX_FOLLOW_SYMLINK_CALLABLES
    }
    return fake


class CompositionStaticBoundaryTests(unittest.TestCase):
    def test_module_import_has_no_platform_specific_dependencies(self) -> None:
        source = COMPOSITION_PATH.read_text(encoding="utf-8")
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
        forbidden = {
            "fcntl",
            "platform_fs_posix",
            "platform_fs_windows",
            "windows_file_api",
        }
        self.assertFalse(forbidden & (imports | imported_from))

    def test_factory_has_only_explicit_probe_root_and_no_boolean_or_token_gate(self) -> None:
        source = COMPOSITION_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "compose_platform_file_backend"
        )
        self.assertEqual([argument.arg for argument in function.args.args], ["probe_root"])
        self.assertEqual(function.args.kwonlyargs, [])
        self.assertEqual(ast.unparse(function.returns), "PlatformFileBackend")
        self.assertNotIn("token", source.casefold())
        self.assertNotIn("validated = true", source.casefold())

    def test_windows_persistent_narrowing_has_explicit_backend_and_probe_root(self) -> None:
        source = COMPOSITION_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "narrow_windows_persistent_private_proof"
        )
        self.assertEqual(
            [argument.arg for argument in function.args.args],
            ["backend", "probe_root"],
        )
        self.assertEqual(function.args.kwonlyargs, [])
        self.assertEqual(ast.unparse(function.returns), "PersistentPrivateProof")

    def test_current_windows_import_does_not_load_posix_modules(self) -> None:
        if sys.platform != "win32":
            self.skipTest("requires the real Windows import environment")
        sys.modules.pop("platform_fs_posix", None)
        sys.modules.pop("fcntl", None)
        imported = importlib.reload(platform_fs)
        self.assertNotIn("platform_fs_posix", sys.modules)
        self.assertNotIn("fcntl", sys.modules)
        self.assertTrue(callable(imported.compose_platform_file_backend))


class CompositionFactoryMatrixTests(unittest.TestCase):
    def test_aggregate_backend_protocol_requires_all_five_service_shapes(self) -> None:
        backend = _FaultingBackend()
        self.assertIsInstance(backend, RootedFileSystem)
        self.assertIsInstance(backend, ExistingFileDurability)
        self.assertIsInstance(backend, MutableFileReservationService)
        self.assertIsInstance(backend, ProcessFileLock)
        self.assertIsInstance(backend, PrivateStorageProof)
        self.assertIsInstance(backend, PlatformFileBackend)
        self.assertNotIsInstance(backend, PersistentPrivateProof)
        for missing in (
            "bind_root",
            "open_existing_for_synchronization",
            "reserve_mutable_file",
            "acquire",
            "prove_private",
        ):
            members = {
                name: (lambda *args, **kwargs: None)
                for name in get_protocol_members(PlatformFileBackend)
                if name != missing
            }
            incomplete = type("IncompleteBackend", (), members)()
            self.assertNotIsInstance(incomplete, PlatformFileBackend)

    def test_posix_support_sets_use_renameat_representation_while_requiring_replace(self) -> None:
        fake_os = _complete_posix_os()
        self.assertIn(fake_os.rename, fake_os.supports_dir_fd)
        self.assertNotIn(fake_os.replace, fake_os.supports_dir_fd)
        with mock.patch.object(platform_fs, "os", fake_os):
            self.assertTrue(platform_fs._posix_primitives_available())
            fake_os.supports_dir_fd.remove(fake_os.rename)
            self.assertFalse(platform_fs._posix_primitives_available())
            fake_os.supports_dir_fd.add(fake_os.rename)
            fake_os.replace = None
            self.assertFalse(platform_fs._posix_primitives_available())

    def test_unknown_host_fails_before_importing_any_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            platform_fs.sys, "platform", "plan9"
        ), mock.patch.object(platform_fs.importlib, "import_module") as import_module:
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs.compose_platform_file_backend(Path(directory))
        _assert_stable_error(self, caught)
        import_module.assert_not_called()

    def test_relative_probe_root_fails_without_backend_import(self) -> None:
        with mock.patch.object(platform_fs.importlib, "import_module") as import_module:
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs.compose_platform_file_backend(Path("relative"))
        _assert_stable_error(self, caught)
        import_module.assert_not_called()

    def test_path_subclass_spoof_fails_without_backend_import(self) -> None:
        class SpoofedPath(type(Path())):
            def is_absolute(self) -> bool:
                return True

        spoofed = SpoofedPath("relative")
        backend = _PersistentFaultingBackend(authority=_ProbeAuthority())
        operations = (
            lambda: platform_fs.compose_platform_file_backend(spoofed),
            lambda: platform_fs.narrow_windows_persistent_private_proof(
                backend,
                spoofed,
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation), mock.patch.object(
                platform_fs.importlib,
                "import_module",
            ) as import_module:
                with self.assertRaises(PlatformFileError) as caught:
                    operation()
            _assert_stable_error(self, caught)
            import_module.assert_not_called()

    def test_windows_missing_backend_never_imports_posix(self) -> None:
        requested: list[str] = []

        def reject(name: str) -> object:
            requested.append(name)
            raise ModuleNotFoundError(name)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            platform_fs.sys, "platform", "win32"
        ), mock.patch.object(platform_fs.importlib, "import_module", side_effect=reject):
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs.compose_platform_file_backend(Path(directory))
        _assert_stable_error(self, caught)
        self.assertEqual(requested, ["platform_fs_windows"])

    def test_windows_backend_constructor_fault_is_normalized(self) -> None:
        module = _windows_module_with_backend(_ConstructorFault)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            platform_fs.sys, "platform", "win32"
        ), mock.patch.object(
            platform_fs.importlib, "import_module", return_value=module
        ) as import_module:
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs.compose_platform_file_backend(Path(directory))
        _assert_stable_error(self, caught)
        import_module.assert_called_once_with("platform_fs_windows")

    def test_windows_persistent_narrowing_returns_same_exact_backend_after_live_probe(self) -> None:
        authority = _ProbeAuthority()
        backend = _PersistentFaultingBackend(authority=authority)
        module = _windows_module_with_backend(_PersistentFaultingBackend)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            platform_fs.sys, "platform", "win32"
        ), mock.patch.object(
            platform_fs.importlib, "import_module", return_value=module
        ) as import_module:
            narrowed = platform_fs.narrow_windows_persistent_private_proof(
                backend,
                Path(directory),
            )
        self.assertIs(narrowed, backend)
        self.assertIsInstance(narrowed, PlatformFileBackend)
        self.assertIsInstance(narrowed, PersistentPrivateProof)
        self.assertTrue(authority.closed)
        self.assertEqual(authority.close_calls, 1)
        import_module.assert_called_once_with("platform_fs_windows")

    def test_windows_persistent_narrowing_rejects_foreign_and_structural_backends(self) -> None:
        approved_module = _windows_module_with_backend(_PersistentFaultingBackend)

        class ForeignPersistentBackend(_PersistentFaultingBackend):
            pass

        foreign = ForeignPersistentBackend(authority=_ProbeAuthority())
        missing_persistent = _FaultingBackend(authority=_ProbeAuthority())
        cases = (
            (foreign, approved_module),
            (missing_persistent, _windows_module_with_backend(_FaultingBackend)),
        )
        for backend, module in cases:
            with self.subTest(backend=type(backend).__name__), tempfile.TemporaryDirectory() as directory, mock.patch.object(
                platform_fs.sys, "platform", "win32"
            ), mock.patch.object(
                platform_fs.importlib, "import_module", return_value=module
            ):
                with self.assertRaises(PlatformFileError) as caught:
                    platform_fs.narrow_windows_persistent_private_proof(
                        backend,  # type: ignore[arg-type]
                        Path(directory),
                    )
            _assert_stable_error(self, caught)
            self.assertFalse(backend.authority.closed)  # type: ignore[union-attr]

    def test_windows_persistent_narrowing_normalizes_import_and_probe_faults(self) -> None:
        import_fault_backend = _PersistentFaultingBackend(authority=_ProbeAuthority())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            platform_fs.sys, "platform", "win32"
        ), mock.patch.object(
            platform_fs.importlib,
            "import_module",
            side_effect=ModuleNotFoundError("platform_fs_windows"),
        ):
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs.narrow_windows_persistent_private_proof(
                    import_fault_backend,
                    Path(directory),
                )
        _assert_stable_error(self, caught)

        probe_authority = _ProbeAuthority(fail_reproof=True)
        probe_fault_backend = _PersistentFaultingBackend(authority=probe_authority)
        module = _windows_module_with_backend(_PersistentFaultingBackend)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            platform_fs.sys, "platform", "win32"
        ), mock.patch.object(platform_fs.importlib, "import_module", return_value=module):
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs.narrow_windows_persistent_private_proof(
                    probe_fault_backend,
                    Path(directory),
                )
        _assert_stable_error(self, caught)
        self.assertTrue(probe_authority.closed)
        self.assertEqual(probe_authority.close_calls, 1)

    def test_windows_persistent_narrowing_normalizes_durability_probe_fault(self) -> None:
        durability_error = PlatformFileError(
            PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
            retryable=False,
        )
        backend = _PersistentFaultingBackend(error=durability_error)
        module = _windows_module_with_backend(_PersistentFaultingBackend)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            platform_fs.sys, "platform", "win32"
        ), mock.patch.object(platform_fs.importlib, "import_module", return_value=module):
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs.narrow_windows_persistent_private_proof(
                    backend,
                    Path(directory),
                )
        _assert_stable_error(self, caught)

    def test_windows_persistent_narrowing_rejects_relative_root_before_import(self) -> None:
        backend = _PersistentFaultingBackend(authority=_ProbeAuthority())
        with mock.patch.object(platform_fs.sys, "platform", "win32"), mock.patch.object(
            platform_fs.importlib,
            "import_module",
        ) as import_module:
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs.narrow_windows_persistent_private_proof(
                    backend,
                    Path("relative"),
                )
        _assert_stable_error(self, caught)
        import_module.assert_not_called()

    def test_non_windows_persistent_narrowing_never_loads_windows_or_posix(self) -> None:
        backend = _PersistentFaultingBackend(authority=_ProbeAuthority())
        for host in ("linux", "darwin", "plan9"):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory, mock.patch.object(
                platform_fs.sys, "platform", host
            ), mock.patch.object(platform_fs.importlib, "import_module") as import_module:
                with self.assertRaises(PlatformFileError) as caught:
                    platform_fs.narrow_windows_persistent_private_proof(
                        backend,
                        Path(directory),
                    )
            _assert_stable_error(self, caught)
            import_module.assert_not_called()

    def test_posix_backend_path_never_imports_windows_modules(self) -> None:
        requested: list[str] = []

        def load(name: str) -> object:
            requested.append(name)
            if name == "fcntl":
                return _fake_fcntl()
            return types.ModuleType(name)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            platform_fs.sys, "platform", "linux"
        ), mock.patch.object(
            platform_fs, "_posix_primitives_available", return_value=True
        ), mock.patch.object(platform_fs.importlib, "import_module", side_effect=load):
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs.compose_platform_file_backend(Path(directory))
        _assert_stable_error(self, caught)
        self.assertEqual(requested, ["fcntl", "platform_fs_posix"])

    def test_missing_posix_api_constant_and_support_sets_fail_before_import(self) -> None:
        cases = (
            mock.patch.object(platform_fs.os, "pread", None, create=True),
            mock.patch.object(platform_fs.os, "O_NOFOLLOW", None, create=True),
            mock.patch.object(platform_fs.os, "supports_dir_fd", set()),
            mock.patch.object(platform_fs.os, "supports_follow_symlinks", set()),
        )
        for unavailable in cases:
            with self.subTest(unavailable=unavailable), tempfile.TemporaryDirectory() as directory:
                with unavailable, mock.patch.object(
                    platform_fs.sys, "platform", "linux"
                ), mock.patch.object(platform_fs.importlib, "import_module") as import_module:
                    with self.assertRaises(PlatformFileError) as caught:
                        platform_fs.compose_platform_file_backend(Path(directory))
                    _assert_stable_error(self, caught)
                    import_module.assert_not_called()

    def test_missing_posix_lock_api_or_constant_fails_before_adapter_import(self) -> None:
        for missing in ("flock", "LOCK_EX"):
            requested: list[str] = []
            lock_module = _fake_fcntl()
            delattr(lock_module, missing)

            def load(name: str) -> object:
                requested.append(name)
                if name == "fcntl":
                    return lock_module
                raise AssertionError("adapter import must not follow a failed lock gate")

            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory, mock.patch.object(
                platform_fs.sys, "platform", "linux"
            ), mock.patch.object(
                platform_fs, "_posix_primitives_available", return_value=True
            ), mock.patch.object(platform_fs.importlib, "import_module", side_effect=load):
                with self.assertRaises(PlatformFileError) as caught:
                    platform_fs.compose_platform_file_backend(Path(directory))
            _assert_stable_error(self, caught)
            self.assertEqual(requested, ["fcntl"])

    def test_backend_import_constructor_and_shape_faults_are_normalized(self) -> None:
        cases = (
            ModuleNotFoundError("missing"),
            _module_with_backend(_ConstructorFault),
            _module_with_backend(object),
        )
        for result in cases:
            with self.subTest(result=type(result).__name__), tempfile.TemporaryDirectory() as directory:
                effect = result if isinstance(result, BaseException) else None
                importer = effect if effect is not None else _imports_for(result)
                with mock.patch.object(platform_fs.sys, "platform", "linux"), mock.patch.object(
                    platform_fs, "_posix_primitives_available", return_value=True
                ), mock.patch.object(
                    platform_fs.importlib,
                    "import_module",
                    side_effect=importer,
                ):
                    with self.assertRaises(PlatformFileError) as caught:
                        platform_fs.compose_platform_file_backend(Path(directory))
                _assert_stable_error(self, caught)

    def test_self_probe_fault_closes_authority_and_returns_stable_error(self) -> None:
        authority = _ProbeAuthority(fail_reproof=True)

        class ProbeFaultBackend(_FaultingBackend):
            def __init__(self) -> None:
                super().__init__(authority=authority)

        module = _module_with_backend(ProbeFaultBackend)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            platform_fs.sys, "platform", "linux"
        ), mock.patch.object(
            platform_fs, "_posix_primitives_available", return_value=True
        ), mock.patch.object(
            platform_fs.importlib, "import_module", side_effect=_imports_for(module)
        ):
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs.compose_platform_file_backend(Path(directory))
        _assert_stable_error(self, caught)
        self.assertTrue(authority.closed)
        self.assertEqual(authority.close_calls, 1)

    def test_unsupported_root_or_volume_probe_code_is_normalized(self) -> None:
        for code, retryable in (
            (PlatformFileErrorCode.REPARSE_REJECTED, False),
            (PlatformFileErrorCode.IDENTITY_STALE, True),
        ):
            backend_error = PlatformFileError(code, retryable=retryable)

            class UnsupportedBackend(_FaultingBackend):
                def __init__(self) -> None:
                    super().__init__(error=backend_error)

            module = _module_with_backend(UnsupportedBackend)
            with self.subTest(code=code.value), tempfile.TemporaryDirectory() as directory, mock.patch.object(
                platform_fs.sys, "platform", "linux"
            ), mock.patch.object(
                platform_fs, "_posix_primitives_available", return_value=True
            ), mock.patch.object(
                platform_fs.importlib, "import_module", side_effect=_imports_for(module)
            ):
                with self.assertRaises(PlatformFileError) as caught:
                    platform_fs.compose_platform_file_backend(Path(directory))
            _assert_stable_error(self, caught)

    def test_composition_normalizes_durability_probe_fault(self) -> None:
        error = PlatformFileError(
            PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
            retryable=False,
        )

        class DurabilityFaultBackend(_FaultingBackend):
            def __init__(self) -> None:
                super().__init__(error=error)

        module = _module_with_backend(DurabilityFaultBackend)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            platform_fs.sys, "platform", "linux"
        ), mock.patch.object(
            platform_fs, "_posix_primitives_available", return_value=True
        ), mock.patch.object(
            platform_fs.importlib, "import_module", side_effect=_imports_for(module)
        ):
            with self.assertRaises(PlatformFileError) as caught:
                platform_fs.compose_platform_file_backend(Path(directory))
        _assert_stable_error(self, caught)

    def test_current_windows_factory_and_persistent_narrowing_are_live_and_read_only(self) -> None:
        if sys.platform != "win32":
            self.skipTest("requires the real Windows host")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = tuple(root.iterdir())
            backend = platform_fs.compose_platform_file_backend(root)
            narrowed = platform_fs.narrow_windows_persistent_private_proof(
                backend,
                root,
            )
            after = tuple(root.iterdir())
        from platform_fs_windows import WindowsPlatformAdapter

        self.assertIs(type(backend), WindowsPlatformAdapter)
        self.assertIs(narrowed, backend)
        self.assertEqual(after, before)


@unittest.skipUnless(POSIX_RUNTIME, "POSIX runtime evidence requires darwin/linux")
class PosixCompositionRuntimeTests(unittest.TestCase):
    def test_real_adapter_is_returned_after_handle_only_root_probe(self) -> None:
        from platform_fs_posix import PosixPlatformAdapter

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fd_directory = Path("/dev/fd")
            before = len(tuple(fd_directory.iterdir())) if fd_directory.is_dir() else None
            backend = platform_fs.compose_platform_file_backend(root)
            after = len(tuple(fd_directory.iterdir())) if fd_directory.is_dir() else None
            self.assertIs(type(backend), PosixPlatformAdapter)
            self.assertIsInstance(backend, RootedFileSystem)
            self.assertIsInstance(backend, ProcessFileLock)
            self.assertIsInstance(backend, PrivateStorageProof)
            self.assertEqual(after, before)
            with backend.bind_root(root) as authority:
                authority.reprove()


if __name__ == "__main__":
    unittest.main()
