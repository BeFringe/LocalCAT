"""Bound destination publication for direct and packaged resource artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from typing import Callable, TypeVar
from uuid import uuid4

from resource_package_contracts import ResourcePortabilityError


_ValidationT = TypeVar("_ValidationT")
_COPY_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResourceArtifactPublication:
    destination_before_digest: str | None
    destination_after_digest: str


@dataclass(frozen=True, slots=True)
class _DestinationFact:
    digest: str
    identity: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class _ParentFact:
    device: int
    inode: int


class ResourceArtifactSaveService:
    """Publish one validated same-directory candidate with proven rollback."""

    def publish(
        self,
        candidate: Path,
        destination: Path,
        validator: Callable[[Path], _ValidationT],
    ) -> tuple[ResourceArtifactPublication, _ValidationT]:
        if not isinstance(candidate, Path) or not candidate.is_absolute():
            raise TypeError("artifact candidate must be an absolute Path")
        if not isinstance(destination, Path) or not destination.is_absolute():
            raise TypeError("artifact destination must be an absolute Path")
        if candidate.parent != destination.parent or candidate == destination:
            raise ValueError("artifact candidate must share the destination directory")
        if not callable(validator):
            raise TypeError("artifact validator must be callable")
        parent_fact = _parent_fact(destination.parent)
        parent_descriptor = _open_bound_parent(destination.parent, parent_fact)
        try:
            candidate_fact = _regular_file_fact_at(parent_descriptor, candidate.name)
            try:
                validator(candidate)
            except Exception as error:
                raise ResourcePortabilityError(
                    "RESOURCE.EXPORT.VALIDATION_FAILED"
                ) from error
            before = _optional_regular_file_fact_at(
                parent_descriptor,
                destination.name,
            )
            recovery_name: str | None = None
            published = False
            try:
                if before is not None:
                    recovery_name = f".{destination.name}.{uuid4().hex}.lkg"
                    _copy_new_file_at(
                        parent_descriptor,
                        destination.name,
                        recovery_name,
                    )
                    if (
                        _regular_file_fact_at(parent_descriptor, recovery_name).digest
                        != before.digest
                    ):
                        raise ResourcePortabilityError("RESOURCE.EXPORT.STAGE_FAILED")
                _require_parent(destination.parent, parent_fact)
                if (
                    _optional_regular_file_fact_at(
                        parent_descriptor,
                        destination.name,
                    )
                    != before
                ):
                    raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")
                if (
                    _regular_file_fact_at(parent_descriptor, candidate.name)
                    != candidate_fact
                ):
                    raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_CHANGED")
                os.replace(
                    candidate.name,
                    destination.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                published = True
                os.fsync(parent_descriptor)
                _require_parent(destination.parent, parent_fact)
                try:
                    validation = validator(destination)
                except Exception as error:
                    raise ResourcePortabilityError(
                        "RESOURCE.EXPORT.VALIDATION_FAILED"
                    ) from error
                _require_parent(destination.parent, parent_fact)
                after = _regular_file_fact_at(parent_descriptor, destination.name)
                if after.digest != candidate_fact.digest:
                    raise ResourcePortabilityError("RESOURCE.EXPORT.VALIDATION_FAILED")
                if recovery_name is not None:
                    os.unlink(recovery_name, dir_fd=parent_descriptor)
                    recovery_name = None
                    os.fsync(parent_descriptor)
                return (
                    ResourceArtifactPublication(
                        destination_before_digest=(
                            None if before is None else before.digest
                        ),
                        destination_after_digest=after.digest,
                    ),
                    validation,
                )
            except BaseException as primary:
                if published:
                    try:
                        observed = _optional_regular_file_fact_at(
                            parent_descriptor,
                            destination.name,
                        )
                        if observed is None or observed.digest != candidate_fact.digest:
                            raise ResourcePortabilityError(
                                "RESOURCE.EXPORT.RECOVERY_REQUIRED"
                            )
                        if recovery_name is None:
                            os.unlink(destination.name, dir_fd=parent_descriptor)
                        else:
                            os.replace(
                                recovery_name,
                                destination.name,
                                src_dir_fd=parent_descriptor,
                                dst_dir_fd=parent_descriptor,
                            )
                            recovery_name = None
                        os.fsync(parent_descriptor)
                        restored = _optional_regular_file_fact_at(
                            parent_descriptor,
                            destination.name,
                        )
                        if (
                            (before is None and restored is not None)
                            or (
                                before is not None
                                and (
                                    restored is None
                                    or restored.digest != before.digest
                                )
                            )
                        ):
                            raise ResourcePortabilityError(
                                "RESOURCE.EXPORT.RECOVERY_REQUIRED"
                            )
                    except BaseException as rollback_error:
                        raise ResourcePortabilityError(
                            "RESOURCE.EXPORT.RECOVERY_REQUIRED"
                        ) from rollback_error
                if recovery_name is not None:
                    if (
                        _optional_regular_file_fact_at(
                            parent_descriptor,
                            recovery_name,
                        )
                        is not None
                    ):
                        os.unlink(recovery_name, dir_fd=parent_descriptor)
                        os.fsync(parent_descriptor)
                if isinstance(primary, ResourcePortabilityError):
                    raise primary
                raise ResourcePortabilityError(
                    "RESOURCE.EXPORT.PUBLICATION_FAILED"
                ) from primary
        finally:
            os.close(parent_descriptor)


def _optional_regular_file_fact(path: Path) -> _DestinationFact | None:
    try:
        return _regular_file_fact(path)
    except FileNotFoundError:
        return None


def _optional_regular_file_fact_at(
    parent_descriptor: int,
    name: str,
) -> _DestinationFact | None:
    try:
        return _regular_file_fact_at(parent_descriptor, name)
    except FileNotFoundError:
        return None


def _regular_file_fact(path: Path) -> _DestinationFact:
    initial = os.lstat(path)
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
        raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")
    digest = hashlib.sha256()
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    first_identity = (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
    )
    final_identity = (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    )
    if first_identity != final_identity:
        raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")
    return _DestinationFact(digest.hexdigest(), final_identity)


def _regular_file_fact_at(parent_descriptor: int, name: str) -> _DestinationFact:
    initial = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
        raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    first_identity = (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
    )
    final_identity = (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    )
    if first_identity != final_identity:
        raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")
    return _DestinationFact(digest.hexdigest(), final_identity)


def _copy_new_file(source: Path, destination: Path) -> None:
    source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            while True:
                chunk = os.read(source_descriptor, _COPY_CHUNK)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    view = view[written:]
            os.fsync(destination_descriptor)
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)
    _fsync_directory(destination.parent)


def _copy_new_file_at(
    parent_descriptor: int,
    source_name: str,
    destination_name: str,
) -> None:
    source_descriptor = os.open(
        source_name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        destination_descriptor = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        try:
            while True:
                chunk = os.read(source_descriptor, _COPY_CHUNK)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    view = view[written:]
            os.fsync(destination_descriptor)
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)
    os.fsync(parent_descriptor)


def _parent_fact(path: Path) -> _ParentFact:
    status = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(status.st_mode):
        raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")
    return _ParentFact(status.st_dev, status.st_ino)


def _open_bound_parent(path: Path, expected: _ParentFact) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    status = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(status.st_mode)
        or (status.st_dev, status.st_ino) != (expected.device, expected.inode)
    ):
        os.close(descriptor)
        raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")
    return descriptor


def _require_parent(path: Path, expected: _ParentFact) -> None:
    observed = _parent_fact(path)
    if observed != expected:
        raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ResourceArtifactPublication",
    "ResourceArtifactSaveService",
]
