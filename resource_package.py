"""Strict deterministic two-member ResourcePackage carrier."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import binascii
import hashlib
import os
from pathlib import Path
import stat
import struct
from typing import BinaryIO, Iterator

from resource_package_contracts import (
    MAX_ARTIFACT_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_PAYLOAD_BYTES,
    ResourcePackageManifest,
    ResourcePackageTransferMetadata,
    ResourcePackageValidationReport,
    ResourcePortabilityError,
    manifest_from_bytes,
    manifest_to_bytes,
)


_LOCAL_SIGNATURE = 0x04034B50
_CENTRAL_SIGNATURE = 0x02014B50
_EOCD_SIGNATURE = 0x06054B50
_LOCAL = struct.Struct("<IHHHHHIIIHH")
_CENTRAL = struct.Struct("<IHHHHHHIIIHHHHHII")
_EOCD = struct.Struct("<IHHHHIIH")
_VERSION_MADE_BY = (3 << 8) | 20
_VERSION_NEEDED = 20
_DOS_TIME = 0
_DOS_DATE = 33
_EXTERNAL_ATTRIBUTES = (stat.S_IFREG | 0o644) << 16
_MANIFEST_NAME = b"manifest.json"
_COPY_CHUNK = 1024 * 1024
_NATIVE_PATH_TYPE = type(Path())


@dataclass(frozen=True, slots=True)
class _CarrierMember:
    name: bytes
    crc32: int
    byte_count: int
    local_offset: int
    data_offset: int


class SealedResourcePackage:
    """One retained, validated package descriptor and its exact payload member."""

    __slots__ = (
        "_artifact_digest",
        "_fd",
        "_identity",
        "_payload",
        "_source_path",
        "manifest",
        "validation",
    )

    def __init__(
        self,
        *,
        fd: int,
        identity: tuple[int, int, int, int],
        artifact_digest: str,
        source_path: Path,
        manifest: ResourcePackageManifest,
        validation: ResourcePackageValidationReport,
        payload: _CarrierMember,
    ) -> None:
        self._fd = fd
        self._identity = identity
        self._artifact_digest = artifact_digest
        self._source_path = source_path
        self.manifest = manifest
        self.validation = validation
        self._payload = payload

    @property
    def closed(self) -> bool:
        return self._fd < 0

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> SealedResourcePackage:
        if self.closed:
            raise ValueError("sealed ResourcePackage is closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def reprove(self) -> None:
        """Reprove the retained file identity and complete bytes before apply."""

        fd = self._require_open()
        try:
            observed = _fd_identity(fd)
            path_info = os.lstat(self._source_path)
            path_identity = (
                path_info.st_dev,
                path_info.st_ino,
                path_info.st_size,
                path_info.st_mtime_ns,
            )
        except (OSError, ResourcePortabilityError) as error:
            raise ResourcePortabilityError("RESOURCE.IMPORT.SOURCE_STALE") from error
        if (
            not stat.S_ISREG(path_info.st_mode)
            or path_info.st_nlink != 1
            or path_identity != self._identity
            or observed != self._identity
            or _digest_fd(fd) != self._artifact_digest
        ):
            raise ResourcePortabilityError("RESOURCE.IMPORT.SOURCE_STALE")

    def copy_payload_to(self, destination: Path) -> None:
        """Copy the exact bounded payload to a new private regular file."""

        if type(destination) is not _NATIVE_PATH_TYPE or not destination.is_absolute():
            raise TypeError("payload destination must be an absolute Path")
        fd = self._require_open()
        output = _open_new_regular(destination)
        digest = hashlib.sha256()
        crc = 0
        remaining = self._payload.byte_count
        try:
            os.lseek(fd, self._payload.data_offset, os.SEEK_SET)
            while remaining:
                chunk = os.read(fd, min(_COPY_CHUNK, remaining))
                if not chunk:
                    raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
                _write_all(output, chunk)
                digest.update(chunk)
                crc = binascii.crc32(chunk, crc)
                remaining -= len(chunk)
            os.fsync(output)
        except BaseException:
            os.close(output)
            destination.unlink(missing_ok=True)
            raise
        os.close(output)
        _fsync_directory(destination.parent)
        if (
            digest.hexdigest() != self.validation.payload_digest
            or crc & 0xFFFFFFFF != self._payload.crc32
        ):
            destination.unlink(missing_ok=True)
            _fsync_directory(destination.parent)
            raise ResourcePortabilityError("RESOURCE.PACKAGE.DIGEST_MISMATCH")
        self.reprove()

    def transfer_metadata(self) -> ResourcePackageTransferMetadata:
        report = self.validation
        return ResourcePackageTransferMetadata(
            artifact_sha256=report.artifact_digest,
            artifact_byte_count=report.artifact_byte_count,
            manifest_schema=self.manifest.schema,
            carrier_profile=report.carrier_profile,
            payload_profile_set=report.payload_profile_set,
            resource_kind=report.resource_kind,
            payload_profile=report.payload_profile,
            payload_sha256=report.payload_digest,
            record_count=report.record_count,
        )

    def transfer_artifact(self) -> ResourcePackageArtifact:
        """Detach a path-free immutable artifact port from this sealed source."""

        return ResourcePackageArtifact(
            fd=os.dup(self._require_open()),
            identity=self._identity,
            artifact_digest=self._artifact_digest,
            metadata=self.transfer_metadata(),
        )

    def _require_open(self) -> int:
        if self._fd < 0:
            raise ValueError("sealed ResourcePackage is closed")
        return self._fd


class ResourcePackageArtifact:
    """Path-free bounded byte source for a future provider/sync consumer."""

    __slots__ = ("_artifact_digest", "_fd", "_identity", "metadata")

    def __init__(
        self,
        *,
        fd: int,
        identity: tuple[int, int, int, int],
        artifact_digest: str,
        metadata: ResourcePackageTransferMetadata,
    ) -> None:
        self._fd = fd
        self._identity = identity
        self._artifact_digest = artifact_digest
        self.metadata = metadata

    @property
    def closed(self) -> bool:
        return self._fd < 0

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> ResourcePackageArtifact:
        self._reprove()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def open_bounded_stream(self) -> Iterator[BinaryIO]:
        """Yield only the retained validated artifact bytes, never a local path."""

        self._reprove()
        descriptor = os.dup(self._fd)
        handle = os.fdopen(descriptor, "rb", closefd=True)
        try:
            handle.seek(0)
            yield handle
        finally:
            handle.close()

    def _reprove(self) -> None:
        if self._fd < 0:
            raise ValueError("ResourcePackage artifact is closed")
        if (
            _fd_identity(self._fd) != self._identity
            or _digest_fd(self._fd) != self._artifact_digest
            or self.metadata.artifact_sha256 != self._artifact_digest
            or self.metadata.artifact_byte_count != self._identity[2]
        ):
            raise ResourcePortabilityError("RESOURCE.PACKAGE.SOURCE_UNSAFE")


def write_resource_package(
    destination: Path,
    manifest: ResourcePackageManifest,
    payload_source: Path,
) -> ResourcePackageValidationReport:
    """Write one canonical ResourcePackage to a new caller-owned path."""

    _require_absolute_path(destination, "package destination")
    _require_absolute_path(payload_source, "package payload source")
    manifest_bytes = manifest_to_bytes(manifest)
    source_fd = _open_existing_regular(payload_source)
    destination_fd = -1
    try:
        source_identity = _fd_identity(source_fd)
        payload_digest, payload_crc, payload_size = _measure_fd(
            source_fd,
            MAX_PAYLOAD_BYTES,
        )
        if (
            payload_digest != manifest.payload.sha256
            or payload_size != manifest.payload.byte_count
        ):
            raise ResourcePortabilityError("RESOURCE.PACKAGE.DIGEST_MISMATCH")
        destination_fd = _open_new_regular(destination)
        entries: list[_CarrierMember] = []
        offset = 0
        manifest_crc = binascii.crc32(manifest_bytes) & 0xFFFFFFFF
        offset = _write_local_member(
            destination_fd,
            offset,
            _MANIFEST_NAME,
            manifest_crc,
            len(manifest_bytes),
            lambda output: _write_all(output, manifest_bytes),
            entries,
        )

        def copy_payload(output: int) -> None:
            os.lseek(source_fd, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            crc = 0
            copied = 0
            while copied < payload_size:
                chunk = os.read(source_fd, min(_COPY_CHUNK, payload_size - copied))
                if not chunk:
                    raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
                _write_all(output, chunk)
                digest.update(chunk)
                crc = binascii.crc32(chunk, crc)
                copied += len(chunk)
            if (
                digest.hexdigest() != payload_digest
                or crc & 0xFFFFFFFF != payload_crc
                or _fd_identity(source_fd) != source_identity
            ):
                raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_CHANGED")

        offset = _write_local_member(
            destination_fd,
            offset,
            manifest.payload.path.encode("utf-8"),
            payload_crc,
            payload_size,
            copy_payload,
            entries,
        )
        central_offset = offset
        for member in entries:
            central = _CENTRAL.pack(
                _CENTRAL_SIGNATURE,
                _VERSION_MADE_BY,
                _VERSION_NEEDED,
                0,
                0,
                _DOS_TIME,
                _DOS_DATE,
                member.crc32,
                member.byte_count,
                member.byte_count,
                len(member.name),
                0,
                0,
                0,
                0,
                _EXTERNAL_ATTRIBUTES,
                member.local_offset,
            )
            _write_all(destination_fd, central)
            _write_all(destination_fd, member.name)
            offset += len(central) + len(member.name)
        central_size = offset - central_offset
        eocd = _EOCD.pack(
            _EOCD_SIGNATURE,
            0,
            0,
            2,
            2,
            central_size,
            central_offset,
            0,
        )
        _write_all(destination_fd, eocd)
        offset += len(eocd)
        if offset > MAX_ARTIFACT_BYTES:
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = -1
        _fsync_directory(destination.parent)
    except BaseException:
        if destination_fd >= 0:
            os.close(destination_fd)
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)

    with open_resource_package(destination) as sealed:
        return sealed.validation


def open_resource_package(source: Path) -> SealedResourcePackage:
    """Open, raw-validate and retain one exact ResourcePackage artifact."""

    _require_absolute_path(source, "package source")
    fd = _open_existing_regular(source)
    try:
        identity = _fd_identity(fd)
        artifact_size = identity[2]
        if artifact_size > MAX_ARTIFACT_BYTES or artifact_size < _EOCD.size:
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
        members = _parse_carrier(fd, artifact_size)
        manifest_member, payload_member = members
        manifest_bytes = _read_member_bytes(fd, manifest_member, MAX_MANIFEST_BYTES)
        manifest = manifest_from_bytes(manifest_bytes)
        expected_payload_name = manifest.payload.path.encode("utf-8")
        if payload_member.name != expected_payload_name:
            raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
        payload_digest, payload_crc = _digest_member(fd, payload_member)
        if (
            payload_digest != manifest.payload.sha256
            or payload_member.byte_count != manifest.payload.byte_count
            or payload_crc != payload_member.crc32
        ):
            raise ResourcePortabilityError("RESOURCE.PACKAGE.DIGEST_MISMATCH")
        artifact_digest = _digest_fd(fd)
        report = ResourcePackageValidationReport(
            artifact_digest=artifact_digest,
            artifact_byte_count=artifact_size,
            manifest_digest=hashlib.sha256(manifest_bytes).hexdigest(),
            carrier_profile=manifest.carrier_profile,
            payload_profile_set=manifest.payload_profile_set,
            resource_kind=manifest.resource_kind,
            payload_profile=manifest.payload_profile,
            payload_digest=payload_digest,
            payload_byte_count=payload_member.byte_count,
            record_count=manifest.payload.record_count,
            legacy_record_count=manifest.profile_counts.legacy_record_count,
            v1_record_count=manifest.profile_counts.v1_record_count,
            safe_issues=(),
        )
        if _fd_identity(fd) != identity:
            raise ResourcePortabilityError("RESOURCE.PACKAGE.SOURCE_UNSAFE")
        return SealedResourcePackage(
            fd=fd,
            identity=identity,
            artifact_digest=artifact_digest,
            source_path=source,
            manifest=manifest,
            validation=report,
            payload=payload_member,
        )
    except BaseException:
        os.close(fd)
        raise


def validate_resource_package(source: Path) -> ResourcePackageValidationReport:
    with open_resource_package(source) as sealed:
        return sealed.validation


def _write_local_member(
    fd: int,
    offset: int,
    name: bytes,
    crc32: int,
    byte_count: int,
    writer: object,
    entries: list[_CarrierMember],
) -> int:
    if not callable(writer):
        raise TypeError("member writer must be callable")
    header = _LOCAL.pack(
        _LOCAL_SIGNATURE,
        _VERSION_NEEDED,
        0,
        0,
        _DOS_TIME,
        _DOS_DATE,
        crc32,
        byte_count,
        byte_count,
        len(name),
        0,
    )
    _write_all(fd, header)
    _write_all(fd, name)
    data_offset = offset + len(header) + len(name)
    writer(fd)
    entries.append(
        _CarrierMember(
            name=name,
            crc32=crc32,
            byte_count=byte_count,
            local_offset=offset,
            data_offset=data_offset,
        )
    )
    return data_offset + byte_count


def _parse_carrier(fd: int, artifact_size: int) -> tuple[_CarrierMember, _CarrierMember]:
    eocd_offset = artifact_size - _EOCD.size
    eocd = _read_exact_at(fd, eocd_offset, _EOCD.size)
    values = _EOCD.unpack(eocd)
    if values[:5] != (_EOCD_SIGNATURE, 0, 0, 2, 2) or values[7] != 0:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.FORMAT_UNSUPPORTED")
    central_size, central_offset = values[5], values[6]
    if central_offset + central_size != eocd_offset:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
    cursor = central_offset
    central_members: list[_CarrierMember] = []
    for _index in range(2):
        fixed = _read_exact_at(fd, cursor, _CENTRAL.size)
        item = _CENTRAL.unpack(fixed)
        if (
            item[0] != _CENTRAL_SIGNATURE
            or item[1] != _VERSION_MADE_BY
            or item[2] != _VERSION_NEEDED
            or item[3:7] != (0, 0, _DOS_TIME, _DOS_DATE)
            or item[8] != item[9]
            or item[11:15] != (0, 0, 0, 0)
            or item[15] != _EXTERNAL_ATTRIBUTES
        ):
            raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
        name_length = item[10]
        name = _read_exact_at(fd, cursor + _CENTRAL.size, name_length)
        central_members.append(
            _CarrierMember(
                name=name,
                crc32=item[7],
                byte_count=item[8],
                local_offset=item[16],
                data_offset=-1,
            )
        )
        cursor += _CENTRAL.size + name_length
    if cursor != central_offset + central_size:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
    expected_names = (_MANIFEST_NAME, central_members[1].name)
    if central_members[0].name != _MANIFEST_NAME:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
    if (
        not central_members[1].name
        or central_members[1].name == _MANIFEST_NAME
        or central_members[1].name.lower() == _MANIFEST_NAME.lower()
    ):
        raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
    local_members: list[_CarrierMember] = []
    expected_offset = 0
    for central, expected_name in zip(central_members, expected_names, strict=True):
        if central.local_offset != expected_offset:
            raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
        fixed = _read_exact_at(fd, central.local_offset, _LOCAL.size)
        item = _LOCAL.unpack(fixed)
        if (
            item[0] != _LOCAL_SIGNATURE
            or item[1] != _VERSION_NEEDED
            or item[2:6] != (0, 0, _DOS_TIME, _DOS_DATE)
            or item[6] != central.crc32
            or item[7] != central.byte_count
            or item[8] != central.byte_count
            or item[10] != 0
        ):
            raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
        name = _read_exact_at(fd, central.local_offset + _LOCAL.size, item[9])
        if name != central.name or name != expected_name:
            raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
        data_offset = central.local_offset + _LOCAL.size + len(name)
        local = _CarrierMember(
            name=name,
            crc32=central.crc32,
            byte_count=central.byte_count,
            local_offset=central.local_offset,
            data_offset=data_offset,
        )
        local_members.append(local)
        expected_offset = data_offset + central.byte_count
    if expected_offset != central_offset:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
    return local_members[0], local_members[1]


def _read_member_bytes(fd: int, member: _CarrierMember, limit: int) -> bytes:
    if member.byte_count > limit:
        raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
    payload = _read_exact_at(fd, member.data_offset, member.byte_count)
    if binascii.crc32(payload) & 0xFFFFFFFF != member.crc32:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.DIGEST_MISMATCH")
    return payload


def _digest_member(fd: int, member: _CarrierMember) -> tuple[str, int]:
    if member.byte_count > MAX_PAYLOAD_BYTES:
        raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
    os.lseek(fd, member.data_offset, os.SEEK_SET)
    remaining = member.byte_count
    digest = hashlib.sha256()
    crc = 0
    while remaining:
        chunk = os.read(fd, min(_COPY_CHUNK, remaining))
        if not chunk:
            raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
        digest.update(chunk)
        crc = binascii.crc32(chunk, crc)
        remaining -= len(chunk)
    return digest.hexdigest(), crc & 0xFFFFFFFF


def _measure_fd(fd: int, limit: int) -> tuple[str, int, int]:
    size = os.fstat(fd).st_size
    if size > limit:
        raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
    digest, crc = _digest_member(
        fd,
        _CarrierMember(b"payload", 0, size, 0, 0),
    )
    return digest, crc, size


def _digest_fd(fd: int) -> str:
    size = os.fstat(fd).st_size
    digest, _crc = _digest_member(
        fd,
        _CarrierMember(b"artifact", 0, size, 0, 0),
    )
    return digest


def _fd_identity(fd: int) -> tuple[int, int, int, int]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.SOURCE_UNSAFE")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _open_existing_regular(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        _fd_identity(fd)
        return fd
    except (OSError, ResourcePortabilityError) as error:
        if fd >= 0:
            os.close(fd)
        raise ResourcePortabilityError("RESOURCE.PACKAGE.SOURCE_UNSAFE") from error


def _open_new_regular(path: Path) -> int:
    _require_absolute_path(path, "new artifact")
    try:
        return os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError as error:
        raise ResourcePortabilityError("RESOURCE.EXPORT.STAGE_FAILED") from error


def _read_exact_at(fd: int, offset: int, size: int) -> bytes:
    if type(offset) is not int or type(size) is not int or offset < 0 or size < 0:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
    os.lseek(fd, offset, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _require_absolute_path(path: object, name: str) -> None:
    if type(path) is not _NATIVE_PATH_TYPE or not path.is_absolute():
        raise TypeError(f"{name} must be an absolute Path")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ResourcePackageArtifact",
    "SealedResourcePackage",
    "open_resource_package",
    "validate_resource_package",
    "write_resource_package",
]
