"""Deterministic source identity for the Windows C3B capability evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Iterable


SNAPSHOT_DOMAIN = b"LocalCAT\x00WindowsC3BSourceSnapshot\x00v1\x00"
C3B_SOURCE_KEYS = (
    "packaging/windows/evidence-scenarios/windows-documented-publish-v1.json",
    "platform_fs.py",
    "platform_fs_contracts.py",
    "platform_fs_windows.py",
    "tests/test_platform_fs_composition.py",
    "tests/test_platform_fs_contracts.py",
    "tests/test_platform_fs_windows.py",
    "tests/test_platform_fs_windows_documented_publish.py",
    "tests/test_platform_fs_windows_lock.py",
    "tests/test_platform_fs_windows_private.py",
    "tests/test_platform_fs_windows_publish.py",
    "tests/test_platform_fs_windows_rooted.py",
    "tests/test_windows_c3b_source_snapshot.py",
    "tests/test_windows_file_api.py",
    "tests/test_windows_release_evidence.py",
    "tests/windows_lock_access_helper.c",
    "tests/windows_lock_worker.py",
    "tests/windows_native_helper_build.py",
    "tests/windows_process_token_helper.py",
    "tests/windows_publish_recovery_worker.py",
    "tools/run_windows_c3b_capability_gate.py",
    "tools/run_windows_documented_publish_evidence.py",
    "tools/validate_windows_release.py",
    "tools/windows_c3b_source_snapshot.py",
    "windows_file_api.py",
)


def _checked_keys(keys: Iterable[str]) -> tuple[str, ...]:
    checked = tuple(sorted(keys))
    if not checked or len(checked) != len(set(checked)):
        raise ValueError("source snapshot keys must be non-empty and unique")
    for key in checked:
        path = PurePosixPath(key)
        if (
            not key
            or "\\" in key
            or path.is_absolute()
            or str(path) != key
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError(f"source snapshot key is not portable: {key!r}")
    return checked


def source_snapshot_sha256(
    repository_root: Path,
    keys: Iterable[str] = C3B_SOURCE_KEYS,
) -> str:
    """Hash sorted portable paths and exact bytes in a domain-separated framing."""

    root = repository_root.resolve(strict=True)
    digest = hashlib.sha256()
    digest.update(SNAPSHOT_DOMAIN)
    for key in _checked_keys(keys):
        path = root.joinpath(*key.split("/"))
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or not resolved.is_relative_to(root):
            raise ValueError(f"source snapshot entry is not a repository file: {key!r}")
        key_bytes = key.encode("utf-8")
        body = resolved.read_bytes()
        digest.update(b"path\x00")
        digest.update(len(key_bytes).to_bytes(8, "big"))
        digest.update(key_bytes)
        digest.update(b"bytes\x00")
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


__all__ = ["C3B_SOURCE_KEYS", "SNAPSHOT_DOMAIN", "source_snapshot_sha256"]
