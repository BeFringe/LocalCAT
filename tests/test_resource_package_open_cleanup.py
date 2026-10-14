"""ResourcePackage source failures release retained platform authorities."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import errno
from pathlib import Path, PurePath
import tempfile
import unittest
from unittest.mock import patch

from platform_fs import compose_platform_file_backend
from platform_fs_contracts import (
    BoundDirectoryAuthority,
    BoundRegularFile,
    OpaqueAuthority,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    RootedDirectoryAuthority,
    RootedFileSystem,
)
from resource_package import open_resource_package, write_resource_package_bytes
from resource_package_contracts import ResourcePortabilityError
from tests import test_resource_package_carrier as carrier_fixture


class ResourcePackageOpenCleanupTests(unittest.TestCase):
    @contextmanager
    def _package(self) -> Generator[tuple[Path, PlatformFileBackend]]:
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw).resolve() / "input.localcat-resource"
            payload = b'{"source":"a","target":"b"}\n'
            manifest = carrier_fixture.ResourcePackageCarrierTests()._manifest(payload)
            _ = write_resource_package_bytes(package, manifest, payload)
            yield package, compose_platform_file_backend(package.parent)

    @contextmanager
    def _observe(
        self,
        package: Path,
        phase: str = "",
        primary: BaseException | None = None,
        *,
        close_fault: bool = False,
    ) -> Generator[tuple[list[OpaqueAuthority], list[OpaqueAuthority]]]:
        original_bytes = package.read_bytes()
        acquired: list[OpaqueAuthority] = []
        closed: list[OpaqueAuthority] = []
        real_root = RootedFileSystem.bind_root
        real_parent = RootedFileSystem.bind_parent
        real_open = RootedFileSystem.open_regular
        real_close = OpaqueAuthority.close

        def fail(current: str) -> None:
            if phase == current and primary is not None:
                raise primary

        def root(backend: PlatformFileBackend, path: Path) -> RootedDirectoryAuthority:
            fail("bind_root")
            authority = real_root(backend, path)
            acquired.append(authority)
            return authority

        def parent(
            backend: PlatformFileBackend,
            root: RootedDirectoryAuthority,
            relative: PurePath,
        ) -> BoundDirectoryAuthority:
            fail("bind_parent")
            authority = real_parent(backend, root, relative)
            acquired.append(authority)
            return authority

        def opened(
            backend: PlatformFileBackend,
            root: RootedDirectoryAuthority,
            relative: PurePath,
        ) -> BoundRegularFile:
            fail("open_regular")
            authority = real_open(backend, root, relative)
            acquired.append(authority)
            return authority

        def close(authority: OpaqueAuthority) -> None:
            real_close(authority)
            if any(authority is item for item in acquired):
                closed.append(authority)
                if close_fault:
                    raise OSError("fixture cleanup failure")

        try:
            with (
                patch.object(RootedFileSystem, "bind_root", new=root),
                patch.object(RootedFileSystem, "bind_parent", new=parent),
                patch.object(RootedFileSystem, "open_regular", new=opened),
                patch.object(OpaqueAuthority, "close", new=close),
            ):
                yield acquired, closed
        finally:
            # Keep failing RED runs from leaking the real authorities they expose.
            for authority in reversed(acquired):
                real_close(authority)
            self.assertEqual(package.read_bytes(), original_bytes)

    def _assert_released(
        self,
        acquired: list[OpaqueAuthority],
        closed: list[OpaqueAuthority],
    ) -> None:
        self.assertEqual(closed, list(reversed(acquired)))
        self.assertTrue(all(authority.closed for authority in acquired))

    def test_known_initialization_failure_is_safe_and_releases_acquired_authorities(self) -> None:
        with self._package() as (package, backend):
            for phase, count in (("bind_root", 0), ("bind_parent", 1), ("open_regular", 2)):
                for primary in (
                    OSError(errno.EMFILE, "fixture open failure"),
                    PlatformFileError(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False),
                ):
                    with self.subTest(phase=phase, error=type(primary).__name__):
                        with self._observe(package, phase, primary) as (acquired, closed):
                            with self.assertRaises(ResourcePortabilityError) as caught:
                                _ = open_resource_package(package, backend=backend)
                            self.assertEqual(caught.exception.code, "RESOURCE.PACKAGE.SOURCE_UNSAFE")
                            self.assertEqual(str(caught.exception), "RESOURCE.PACKAGE.SOURCE_UNSAFE")
                            self.assertEqual(len(acquired), count)
                            self._assert_released(acquired, closed)

    def test_programmer_initialization_failure_is_preserved_and_releases_authorities(self) -> None:
        with self._package() as (package, backend):
            primary = RuntimeError("fixture programmer failure")
            with self._observe(package, "open_regular", primary) as (acquired, closed):
                with self.assertRaises(RuntimeError) as caught:
                    _ = open_resource_package(package, backend=backend)
                self.assertIs(caught.exception, primary)
                self.assertEqual(len(acquired), 2)
                self._assert_released(acquired, closed)

    def test_initialization_close_failures_do_not_mask_primary_or_skip_remaining_authority(self) -> None:
        with self._package() as (package, backend):
            for primary in (
                PlatformFileError(PlatformFileErrorCode.CAPABILITY_UNAVAILABLE, retryable=False),
                OSError(errno.EMFILE, "fixture open failure"),
                RuntimeError("fixture programmer failure"),
            ):
                with self.subTest(error=type(primary).__name__):
                    with self._observe(package, "open_regular", primary, close_fault=True) as (acquired, closed):
                        if isinstance(primary, (PlatformFileError, OSError)):
                            with self.assertRaises(ResourcePortabilityError) as caught:
                                _ = open_resource_package(package, backend=backend)
                            self.assertEqual(caught.exception.code, "RESOURCE.PACKAGE.SOURCE_UNSAFE")
                        else:
                            with self.assertRaises(RuntimeError) as caught_programmer:
                                _ = open_resource_package(package, backend=backend)
                            self.assertIs(caught_programmer.exception, primary)
                        self.assertEqual(len(acquired), 2)
                        self._assert_released(acquired, closed)

    def test_validation_close_failures_preserve_primary_and_close_all_three_authorities(self) -> None:
        with self._package() as (package, backend):
            for primary in (
                ResourcePortabilityError("RESOURCE.PACKAGE.DIGEST_MISMATCH"),
                RuntimeError("fixture parser programmer failure"),
            ):
                with self.subTest(error=type(primary).__name__):
                    with self._observe(package, close_fault=True) as (acquired, closed):
                        with patch("resource_package._parse_carrier", side_effect=primary):
                            with self.assertRaises(type(primary)) as caught:
                                _ = open_resource_package(package, backend=backend)
                        self.assertIs(caught.exception, primary)
                        self.assertEqual(len(acquired), 3)
                        self._assert_released(acquired, closed)

    def test_success_retains_authorities_until_sealed_package_is_closed(self) -> None:
        with self._package() as (package, backend):
            with self._observe(package) as (acquired, closed):
                sealed = open_resource_package(package, backend=backend)
                self.assertEqual(len(acquired), 3)
                self.assertFalse(sealed.closed)
                self.assertEqual(closed, [])
                self.assertTrue(all(not authority.closed for authority in acquired))
                sealed.reprove()
                self.assertEqual(sealed.validation.artifact_byte_count, len(package.read_bytes()))
                sealed.close()
                self.assertTrue(sealed.closed)
                self._assert_released(acquired, closed)
                sealed.close()
                self._assert_released(acquired, closed)
