"""Fail-closed composition for the shared platform file backend."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
from typing import cast

from platform_fs_contracts import (
    ExactEmptyChildDirectoryRetirement,
    ExistingFileRetirement,
    LockedDescendantNamespaceInspection,
    PersistentPrivateProof,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    validate_root_path,
)


__all__ = [
    "compose_platform_file_backend",
    "narrow_windows_exact_empty_child_directory_retirement",
    "narrow_windows_existing_file_retirement",
    "narrow_windows_locked_descendant_namespace_inspection",
    "narrow_windows_persistent_private_proof",
]


_POSIX_HOSTS = frozenset({"darwin", "linux"})
_POSIX_CALLABLES = (
    "close",
    "dup",
    "fchmod",
    "fsync",
    "fstat",
    "ftruncate",
    "geteuid",
    "link",
    "mkdir",
    "open",
    "pread",
    "pwrite",
    "rename",
    "replace",
    "rmdir",
    "set_inheritable",
    "stat",
    "unlink",
)
_POSIX_CONSTANTS = (
    "O_CREAT",
    "O_DIRECTORY",
    "O_EXCL",
    "O_NOFOLLOW",
    "O_RDONLY",
    "O_RDWR",
)
_POSIX_DIR_FD_CALLABLES = (
    "link",
    "mkdir",
    "open",
    "rename",
    "rmdir",
    "stat",
    "unlink",
)
_POSIX_FOLLOW_SYMLINK_CALLABLES = ("link", "stat")
_POSIX_LOCK_CALLABLES = ("flock",)
_POSIX_LOCK_CONSTANTS = ("LOCK_EX", "LOCK_NB", "LOCK_UN")


def _capability_unavailable() -> PlatformFileError:
    return PlatformFileError(
        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
        retryable=False,
    )


def _members_are_callable(owner: object, names: tuple[str, ...]) -> bool:
    return all(callable(getattr(owner, name, None)) for name in names)


def _members_exist(owner: object, names: tuple[str, ...]) -> bool:
    return all(getattr(owner, name, None) is not None for name in names)


def _support_set_contains(owner: object, support_name: str, names: tuple[str, ...]) -> bool:
    support = getattr(owner, support_name, None)
    if not isinstance(support, set):
        return False
    return all(getattr(owner, name, None) in support for name in names)


def _posix_primitives_available() -> bool:
    return (
        _members_are_callable(os, _POSIX_CALLABLES)
        and _members_exist(os, _POSIX_CONSTANTS)
        and _support_set_contains(os, "supports_dir_fd", _POSIX_DIR_FD_CALLABLES)
        and _support_set_contains(
            os,
            "supports_follow_symlinks",
            _POSIX_FOLLOW_SYMLINK_CALLABLES,
        )
    )


def _backend_has_contract(backend: object) -> bool:
    return isinstance(backend, PlatformFileBackend)


def _load_posix_backend() -> PlatformFileBackend:
    if not _posix_primitives_available():
        raise _capability_unavailable()
    lock_module = importlib.import_module("fcntl")
    if not (
        _members_are_callable(lock_module, _POSIX_LOCK_CALLABLES)
        and _members_exist(lock_module, _POSIX_LOCK_CONSTANTS)
    ):
        raise _capability_unavailable()
    module = importlib.import_module("platform_fs_posix")
    backend_type = getattr(module, "PosixPlatformAdapter", None)
    if not isinstance(backend_type, type):
        raise _capability_unavailable()
    backend = backend_type()
    if type(backend) is not backend_type or not _backend_has_contract(backend):
        raise _capability_unavailable()
    return backend


def _load_windows_backend_type() -> type[object]:
    module = importlib.import_module("platform_fs_windows")
    backend_type = getattr(module, "WindowsPlatformAdapter", None)
    if not isinstance(backend_type, type):
        raise _capability_unavailable()
    return backend_type


def _load_windows_backend() -> PlatformFileBackend:
    backend_type = _load_windows_backend_type()
    backend = backend_type()
    if type(backend) is not backend_type or not _backend_has_contract(backend):
        raise _capability_unavailable()
    return backend


def _self_probe(backend: PlatformFileBackend, probe_root: Path) -> None:
    authority = None
    try:
        authority = backend.bind_root(probe_root)
        authority.reprove()
    finally:
        if authority is not None:
            authority.close()


def compose_platform_file_backend(probe_root: Path) -> PlatformFileBackend:
    """Return a real backend only after a live, read-only root self-probe."""

    try:
        checked_root = validate_root_path(probe_root)
        if sys.platform == "win32":
            backend = _load_windows_backend()
        elif sys.platform in _POSIX_HOSTS:
            backend = _load_posix_backend()
        else:
            raise _capability_unavailable()
        _self_probe(backend, checked_root)
        return backend
    except Exception:
        raise _capability_unavailable() from None


def narrow_windows_persistent_private_proof(
    backend: PlatformFileBackend,
    probe_root: Path,
) -> PersistentPrivateProof:
    """Narrow one exact composed Windows backend after a fresh live root probe."""

    try:
        checked_root = validate_root_path(probe_root)
        if sys.platform != "win32":
            raise _capability_unavailable()
        backend_type = _load_windows_backend_type()
        if (
            type(backend) is not backend_type
            or not _backend_has_contract(backend)
            or not isinstance(backend, PersistentPrivateProof)
        ):
            raise _capability_unavailable()
        _self_probe(backend, checked_root)
        return cast(PersistentPrivateProof, backend)
    except Exception:
        raise _capability_unavailable() from None


def narrow_windows_locked_descendant_namespace_inspection(
    backend: PlatformFileBackend,
    probe_root: Path,
) -> LockedDescendantNamespaceInspection:
    """Narrow the exact composed Windows backend to descendant inspection."""

    try:
        checked_root = validate_root_path(probe_root)
        if sys.platform != "win32":
            raise _capability_unavailable()
        backend_type = _load_windows_backend_type()
        if (
            type(backend) is not backend_type
            or not _backend_has_contract(backend)
            or not isinstance(backend, LockedDescendantNamespaceInspection)
        ):
            raise _capability_unavailable()
        _self_probe(backend, checked_root)
        return cast(LockedDescendantNamespaceInspection, backend)
    except Exception:
        raise _capability_unavailable() from None


def narrow_windows_existing_file_retirement(
    backend: PlatformFileBackend,
    probe_root: Path,
) -> ExistingFileRetirement:
    """Narrow the exact composed Windows backend to existing-file retirement."""

    try:
        checked_root = validate_root_path(probe_root)
        if sys.platform != "win32":
            raise _capability_unavailable()
        backend_type = _load_windows_backend_type()
        if (
            type(backend) is not backend_type
            or not _backend_has_contract(backend)
            or not isinstance(backend, ExistingFileRetirement)
        ):
            raise _capability_unavailable()
        _self_probe(backend, checked_root)
        return cast(ExistingFileRetirement, backend)
    except Exception:
        raise _capability_unavailable() from None


def narrow_windows_exact_empty_child_directory_retirement(
    backend: PlatformFileBackend,
    probe_root: Path,
) -> ExactEmptyChildDirectoryRetirement:
    """Narrow one composed Windows backend to exact empty-child removal."""

    try:
        checked_root = validate_root_path(probe_root)
        if sys.platform != "win32":
            raise _capability_unavailable()
        backend_type = _load_windows_backend_type()
        if (
            type(backend) is not backend_type
            or not _backend_has_contract(backend)
            or not isinstance(backend, ExactEmptyChildDirectoryRetirement)
        ):
            raise _capability_unavailable()
        _self_probe(backend, checked_root)
        return cast(ExactEmptyChildDirectoryRetirement, backend)
    except Exception:
        raise _capability_unavailable() from None
