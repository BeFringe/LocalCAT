"""普通 POSIX 文件读取保留原 root 前缀，独立关闭且不重复祖先 FD。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cached_property
import errno
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import TYPE_CHECKING
import unittest
from unittest import mock

from platform_fs_contracts import (
    BoundRegularFile, PlatformFileError, PlatformFileErrorCode, RootedDirectoryAuthority,
)

if TYPE_CHECKING:
    from platform_fs_posix import PosixPlatformAdapter


def _fd_count() -> int:
    directory = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
    count = 0
    for entry in os.listdir(directory):
        try:
            _ = os.fstat(int(entry))
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            count += 1
    return count


@dataclass(frozen=True)
class _Fixture:
    base: Path
    configured: Path
    adapter: PosixPlatformAdapter
    root: RootedDirectoryAuthority
    before_root: int
    with_root: int


@unittest.skipUnless(os.name == "posix", "requires POSIX retained-file operations")
class PosixReadLifetimeTests(unittest.TestCase):
    @cached_property
    def fixture(self) -> _Fixture:
        from platform_fs_posix import PosixPlatformAdapter

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        configured = base / "one" / "two" / "three" / "four" / "five" / "root"
        (configured / "data").mkdir(parents=True)
        for number in range(25):
            _ = (configured / "data" / f"file-{number}.bin").write_bytes(b"retained source")
        adapter = PosixPlatformAdapter()
        before_root = _fd_count()
        root = adapter.bind_root(configured)
        self.addCleanup(root.close)
        return _Fixture(base, configured, adapter, root, before_root, _fd_count())

    def open_file(self, number: int = 0) -> BoundRegularFile:
        bound = self.fixture.adapter.open_regular(self.fixture.root, PurePosixPath(f"data/file-{number}.bin"))
        self.addCleanup(bound.close)
        return bound

    def test_twenty_five_files_keep_one_root_prefix_and_release_it_last(self) -> None:
        files = [self.open_file(number) for number in range(25)]
        # Each independent file owns only its regular FD and one descendant FD.
        self.assertEqual(_fd_count() - self.fixture.with_root, 50)
        self.fixture.root.close()
        self.fixture.root.close()
        self.assertEqual(_fd_count(), self.fixture.with_root + 50)
        for bound in files[:-1]:
            self.assertEqual(bound.read_all(), b"retained source")
            bound.close()
            bound.close()
        self.assertEqual(_fd_count(), self.fixture.with_root + 2)
        self.assertEqual(files[-1].read_all(), b"retained source")
        files[-1].close()
        self.assertEqual(_fd_count(), self.fixture.before_root)

    def test_file_first_and_closed_root_do_not_revoke_a_sibling(self) -> None:
        first, second = self.open_file(), self.open_file(1)
        first.close()
        first.close()
        third = self.open_file(2)
        self.fixture.root.close()
        with self.assertRaises(PlatformFileError) as caught:
            self.fixture.adapter.open_regular(self.fixture.root, PurePosixPath("data/file-3.bin"))
        self.assertEqual(caught.exception.code, PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value)
        self.assertEqual(second.read_all(), b"retained source")
        self.assertEqual(third.read_all(), b"retained source")
        second.close()
        third.close()
        self.assertEqual(_fd_count(), self.fixture.before_root)

    def test_distinct_file_threads_keep_independent_lifetimes(self) -> None:
        fixture = self.fixture

        def open_one(number: int) -> BoundRegularFile:
            return fixture.adapter.open_regular(fixture.root, PurePosixPath(f"data/file-{number}.bin"))

        with ThreadPoolExecutor(max_workers=5) as pool:
            files = list(pool.map(open_one, range(25)))
        for bound in files:
            self.addCleanup(bound.close)
        self.fixture.root.close()

        def read_close(bound: BoundRegularFile) -> bytes:
            try:
                return bound.read_all()
            finally:
                bound.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual(list(pool.map(read_close, files)), [b"retained source"] * 25)
        self.assertEqual(_fd_count(), self.fixture.before_root)

    def test_file_directly_under_root_has_no_owned_tail(self) -> None:
        _ = (self.fixture.configured / "direct.bin").write_bytes(b"direct")
        bound = self.fixture.adapter.open_regular(self.fixture.root, PurePosixPath("direct.bin"))
        self.addCleanup(bound.close)
        self.assertEqual(_fd_count(), self.fixture.with_root + 1)
        self.fixture.root.close()
        self.assertEqual(bound.read_all(), b"direct")
        bound.close()
        self.assertEqual(_fd_count(), self.fixture.before_root)

    def test_root_close_during_open_keeps_acquired_file_prefix(self) -> None:
        fixture = self.fixture
        original = os.open

        def close_root(path: str, flags: int, *, dir_fd: int | None = None) -> int:
            descriptor = original(path, flags, dir_fd=dir_fd)
            if path == "file-0.bin":
                fixture.root.close()
            return descriptor

        with mock.patch("platform_fs_posix.os.open", side_effect=close_root):
            bound = self.open_file()
        self.assertTrue(fixture.root.closed)
        self.assertEqual(bound.read_all(), b"retained source")
        bound.close()
        self.assertEqual(_fd_count(), fixture.before_root)

    def test_constructor_failure_preserves_primary_after_close_fault(self) -> None:
        from platform_fs_posix import _close_fd

        fixture = self.fixture
        original_fstat = os.fstat
        failed_close = False

        def fail_regular(descriptor: int) -> os.stat_result:
            result = original_fstat(descriptor)
            if stat.S_ISREG(result.st_mode):
                fixture.root.close()
                raise RuntimeError("primary construction failure")
            return result

        def close_then_fail(descriptor: int) -> None:
            nonlocal failed_close
            _close_fd(descriptor)
            if not failed_close:
                failed_close = True
                raise RuntimeError("secondary close failure")

        with mock.patch("platform_fs_posix.os.fstat", side_effect=fail_regular), mock.patch(
            "platform_fs_posix._close_fd", side_effect=close_then_fail,
        ):
            with self.assertRaisesRegex(RuntimeError, "primary construction"):
                fixture.adapter.open_regular(fixture.root, PurePosixPath("data/file-0.bin"))
        self.assertTrue(failed_close)
        self.assertEqual(_fd_count(), fixture.before_root)

    def test_failed_descendant_or_file_open_releases_only_attempt(self) -> None:
        survivor = self.open_file()
        before = _fd_count()
        for relative in ("data/missing/file.bin", "data/missing.bin"):
            with self.subTest(relative=relative), self.assertRaises(PlatformFileError):
                self.fixture.adapter.open_regular(self.fixture.root, PurePosixPath(relative))
            self.assertEqual(_fd_count(), before)
            self.assertEqual(survivor.read_all(), b"retained source")

    def test_file_constructor_failure_closes_new_file_and_tail(self) -> None:
        survivor = self.open_file()
        before = _fd_count()
        original = os.fstat

        def fail_regular(descriptor: int) -> os.stat_result:
            result = original(descriptor)
            if stat.S_ISREG(result.st_mode):
                raise RuntimeError("injected file construction failure")
            return result

        with mock.patch("platform_fs_posix.os.fstat", side_effect=fail_regular):
            with self.assertRaisesRegex(RuntimeError, "file construction"):
                self.fixture.adapter.open_regular(self.fixture.root, PurePosixPath("data/file-1.bin"))
        self.assertEqual(_fd_count(), before)
        self.assertEqual(survivor.read_all(), b"retained source")
        self.assertEqual(self.open_file(2).read_all(), b"retained source")

    def test_programmer_error_during_file_open_releases_attempt(self) -> None:
        survivor = self.open_file()
        before = _fd_count()
        original = os.open

        def fail_file(path: str, flags: int, *, dir_fd: int | None = None) -> int:
            if path == "file-1.bin":
                raise RuntimeError("injected file open failure")
            return original(path, flags, dir_fd=dir_fd)

        with mock.patch("platform_fs_posix.os.open", side_effect=fail_file):
            with self.assertRaisesRegex(RuntimeError, "file open"):
                self.fixture.adapter.open_regular(self.fixture.root, PurePosixPath("data/file-1.bin"))
        self.assertEqual(_fd_count(), before)
        self.assertEqual(survivor.read_all(), b"retained source")

    def test_partial_duplicate_failure_is_stable_and_closes_copies(self) -> None:
        survivor = self.open_file()
        before = _fd_count()
        original = os.dup
        for _ in range(3):
            attempts = 0

            def fail_second(descriptor: int) -> int:
                nonlocal attempts
                attempts += 1
                if attempts == 2:
                    raise OSError(errno.EMFILE, "injected duplicate failure")
                return original(descriptor)

            with mock.patch("platform_fs_posix.os.dup", side_effect=fail_second):
                with self.assertRaises(PlatformFileError) as caught:
                    self.fixture.adapter.bind_parent(self.fixture.root, PurePosixPath("data/file-1.bin"))
            self.assertEqual(caught.exception.code, PlatformFileErrorCode.OUTSIDE_ROOT.value)
            self.assertEqual(_fd_count(), before)
            self.assertEqual(survivor.read_all(), b"retained source")

    def test_same_path_clone_does_not_refresh_original_file(self) -> None:
        bound = self.open_file()
        old = self.fixture.configured.with_name("old-root")
        self.fixture.configured.rename(old)
        shutil.copytree(old, self.fixture.configured)
        self.fixture.root.close()
        with self.assertRaises(PlatformFileError) as caught:
            bound.read_all()
        self.assertEqual(caught.exception.code, PlatformFileErrorCode.IDENTITY_STALE.value)
        fresh = self.fixture.adapter.bind_root(self.fixture.configured)
        with fresh:
            new_file = self.fixture.adapter.open_regular(fresh, PurePosixPath("data/file-0.bin"))
            with new_file:
                self.assertEqual(new_file.read_all(), b"retained source")
        with self.assertRaises(PlatformFileError):
            bound.snapshot()

    def test_ancestor_rename_still_rejects_after_root_close(self) -> None:
        bound = self.open_file()
        (self.fixture.base / "one").rename(self.fixture.base / "old-one")
        self.fixture.root.close()
        with self.assertRaises(PlatformFileError) as caught:
            bound.read_all()
        self.assertEqual(caught.exception.code, PlatformFileErrorCode.IDENTITY_STALE.value)

    def test_replaced_file_or_link_still_rejects_after_root_close(self) -> None:
        first, second = self.open_file(), self.open_file(1)
        path = self.fixture.configured / "data" / "file-0.bin"
        path.rename(path.with_suffix(".old"))
        path.write_bytes(b"retained source")
        linked = self.fixture.configured / "data" / "file-1.bin"
        linked.rename(linked.with_suffix(".old"))
        linked.symlink_to(path)
        self.fixture.root.close()
        with self.assertRaises(PlatformFileError) as stale:
            first.read_all()
        self.assertEqual(stale.exception.code, PlatformFileErrorCode.IDENTITY_STALE.value)
        with self.assertRaises(PlatformFileError) as reparse:
            second.read_all()
        self.assertEqual(reparse.exception.code, PlatformFileErrorCode.REPARSE_REJECTED.value)

    def test_separate_roots_and_foreign_issuer_do_not_share_authority(self) -> None:
        from platform_fs_posix import PosixPlatformAdapter

        first = self.open_file()
        foreign = PosixPlatformAdapter()
        foreign_root = foreign.bind_root(self.fixture.configured)
        with foreign_root:
            second = foreign.open_regular(foreign_root, PurePosixPath("data/file-0.bin"))
        self.addCleanup(second.close)
        first.close()
        self.fixture.root.close()
        self.assertEqual(second.read_all(), b"retained source")
        with self.assertRaises(PlatformFileError) as caught:
            self.fixture.adapter.observe_bound_content(second)
        self.assertEqual(caught.exception.code, PlatformFileErrorCode.CAPABILITY_UNAVAILABLE.value)
        self.assertEqual(foreign.observe_bound_content(second).byte_count, len(b"retained source"))
        second.close()
        self.assertEqual(_fd_count(), self.fixture.before_root)


if __name__ == "__main__":
    unittest.main()
