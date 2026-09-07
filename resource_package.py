"""Strict deterministic two-member ResourcePackage carrier."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import binascii
import hashlib
import os
import sys
from pathlib import Path
import stat
import struct
from typing import BinaryIO, Iterator

from platform_fs_contracts import (
    BoundDirectoryAuthority,
    BoundRegularFile,
    EntrySnapshot,
    PlatformFileBackend,
    PlatformFileError,
    PublishMode,
    RootedDirectoryAuthority,
)
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
from resource_platform_io import (
    BoundRegularFileStream,
    bind_rooted_regular,
    digest_bound,
    iter_bound_chunks,
    platform_relative_path as _platform_relative_path,
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
_BOUND_READ_CHUNK = 64 * 1024
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
        "_backend",
        "_source",
        "_snapshot",
        "_payload",
        "_parent",
        "_root",
        "manifest",
        "_source_name",
        "validation",
    )

    def __init__(
        self,
        *,
        backend: PlatformFileBackend,
        root: RootedDirectoryAuthority,
        parent: BoundDirectoryAuthority,
        source: BoundRegularFile,
        source_name: str,
        snapshot: EntrySnapshot,
        artifact_digest: str,
        manifest: ResourcePackageManifest,
        validation: ResourcePackageValidationReport,
        payload: _CarrierMember,
    ) -> None:
        self._backend = backend
        self._root: RootedDirectoryAuthority | None = root
        self._parent: BoundDirectoryAuthority | None = parent
        self._source: BoundRegularFile | None = source
        self._source_name = source_name
        self._snapshot = snapshot
        self._artifact_digest = artifact_digest
        self.manifest = manifest
        self.validation = validation
        self._payload = payload

    @property
    def closed(self) -> bool:
        return self._source is None

    def close(self) -> None:
        source = self._source
        parent = self._parent
        root = self._root
        self._source = None
        self._parent = None
        self._root = None
        for authority in (source, parent, root):
            if authority is not None:
                try:
                    authority.close()
                except PlatformFileError:
                    pass

    def __enter__(self) -> SealedResourcePackage:
        if self.closed:
            raise ValueError("sealed ResourcePackage is closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def reprove(self) -> None:
        """Reprove the retained file identity and complete bytes before apply."""

        source = self._require_open()
        parent = self._parent
        if parent is None:
            raise ValueError("sealed ResourcePackage is closed")
        if (
            source.snapshot() != self._snapshot
            or parent.inspect_entry(self._source_name) != self._snapshot
            or digest_bound(source, self._snapshot).hex() != self._artifact_digest
        ):
            raise ResourcePortabilityError("RESOURCE.IMPORT.SOURCE_STALE")

    def copy_payload_to(self, destination: Path) -> None:
        """Copy the exact bounded payload to a new private regular file."""

        if type(destination) is not _NATIVE_PATH_TYPE or not destination.is_absolute():
            raise TypeError("payload destination must be an absolute Path")
        source = self._require_open()
        root = None
        parent = None
        candidate = None
        pending = None
        candidate_name = (
            f".resource-payload-{hashlib.sha256(destination.name.encode('utf-8')).hexdigest()[:16]}-{os.urandom(8).hex()}.tmp"
        )
        candidate_identity = None
        try:
            root = self._backend.bind_root(destination.parent)
            parent = self._backend.bind_parent(
                root,
                _platform_relative_path(destination.name),
            )
            if parent.inspect_entry(destination.name) is not None:
                raise ResourcePortabilityError("RESOURCE.EXPORT.STAGE_FAILED")
            candidate = parent.create_candidate(candidate_name, private=True)
            candidate_identity = candidate.identity()
            facts = candidate.write_chunks(
                iter_bound_chunks(
                    source,
                    self._snapshot,
                    start=self._payload.data_offset,
                    length=self._payload.byte_count,
                ),
                maximum_bytes=MAX_PAYLOAD_BYTES,
            )
            if (
                facts.content_sha256.hex() != self.validation.payload_digest
                or facts.byte_count != self._payload.byte_count
            ):
                raise ResourcePortabilityError("RESOURCE.PACKAGE.DIGEST_MISMATCH")
            candidate.flush_content()
            pending = parent.begin_publish(
                candidate,
                destination.name,
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )
            candidate = None
            candidate_identity = None
            preliminary = pending.preliminary_facts()
            retained = pending.retained_destination()
            retained_snapshot = retained.snapshot()
            if (
                preliminary.content_sha256 != facts.content_sha256
                or preliminary.byte_count != facts.byte_count
                or retained_snapshot.byte_count != facts.byte_count
                or digest_bound(retained, retained_snapshot) != facts.content_sha256
                or pending.terminal_reproof() != preliminary
            ):
                raise ResourcePortabilityError("RESOURCE.PACKAGE.DIGEST_MISMATCH")
            self.reprove()
        except BaseException:
            if candidate is not None:
                candidate.close()
            if candidate_identity is not None and parent is not None:
                try:
                    parent.unlink_owned(candidate_name, candidate_identity)
                except PlatformFileError:
                    pass
            if pending is not None and parent is not None:
                identity = pending.preliminary_facts().destination_identity
                pending.close()
                pending = None
                try:
                    parent.unlink_owned(destination.name, identity)
                except PlatformFileError:
                    pass
            raise
        finally:
            for authority in (pending, parent, root):
                if authority is not None:
                    authority.close()

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
        """Transfer the exact retained authority into a path-free artifact port."""

        self.reprove()
        source = self._require_open()
        self._source = None
        parent = self._parent
        root = self._root
        self._parent = None
        self._root = None
        for authority in (parent, root):
            if authority is not None:
                authority.close()
        return ResourcePackageArtifact(
            source=source,
            snapshot=self._snapshot,
            artifact_digest=self._artifact_digest,
            metadata=self.transfer_metadata(),
        )

    def _require_open(self) -> BoundRegularFile:
        source = self._source
        if source is None:
            raise ValueError("sealed ResourcePackage is closed")
        return source


class ResourcePackageArtifact:
    """Path-free bounded byte source for a future provider/sync consumer."""

    __slots__ = ("_artifact_digest", "_snapshot", "_source", "metadata")

    def __init__(
        self,
        *,
        source: BoundRegularFile,
        snapshot: EntrySnapshot,
        artifact_digest: str,
        metadata: ResourcePackageTransferMetadata,
    ) -> None:
        self._source: BoundRegularFile | None = source
        self._snapshot = snapshot
        self._artifact_digest = artifact_digest
        self.metadata = metadata

    @property
    def closed(self) -> bool:
        return self._source is None

    def close(self) -> None:
        source = self._source
        if source is not None:
            self._source = None
            source.close()

    def __enter__(self) -> ResourcePackageArtifact:
        self._reprove()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def open_bounded_stream(self) -> Iterator[BinaryIO]:
        """Yield only the retained validated artifact bytes, never a local path."""

        self._reprove()
        source = self._require_open()
        handle = BoundRegularFileStream(source, self._snapshot)
        try:
            yield handle
        finally:
            handle.close()

    def _reprove(self) -> None:
        source = self._require_open()
        if (
            source.snapshot() != self._snapshot
            or digest_bound(source, self._snapshot).hex() != self._artifact_digest
            or self.metadata.artifact_sha256 != self._artifact_digest
            or self.metadata.artifact_byte_count != self._snapshot.byte_count
        ):
            raise ResourcePortabilityError("RESOURCE.PACKAGE.SOURCE_UNSAFE")

    def _require_open(self) -> BoundRegularFile:
        source = self._source
        if source is None:
            raise ValueError("ResourcePackage artifact is closed")
        return source


def write_resource_package(
    destination: Path,
    manifest: ResourcePackageManifest,
    payload_source: Path,
    *,
    backend: PlatformFileBackend | None = None,
) -> ResourcePackageValidationReport:
    """Write one canonical ResourcePackage to a new caller-owned path."""

    _require_absolute_path(payload_source, "package payload source")
    return _write_resource_package(destination, manifest, payload_source, backend=backend)


def write_resource_package_bytes(
    destination: Path,
    manifest: ResourcePackageManifest,
    payload_bytes: bytes,
    *,
    backend: PlatformFileBackend | None = None,
) -> ResourcePackageValidationReport:
    """Stream exact immutable payload bytes through the ordinary carrier publisher."""
    if type(payload_bytes) is not bytes:
        raise TypeError("package payload must be exact immutable bytes")
    return _write_resource_package(destination, manifest, payload_bytes, backend=backend)


def _write_resource_package(
    destination: Path,
    manifest: ResourcePackageManifest,
    payload_source: Path | bytes,
    *,
    backend: PlatformFileBackend | None,
) -> ResourcePackageValidationReport:
    _require_absolute_path(destination, "package destination")
    manifest_bytes = manifest_to_bytes(manifest)
    resolved_backend = _platform_backend(backend, destination.parent)
    payload = None
    root = None
    parent = None
    candidate = None
    pending = None
    candidate_name: str | None = None
    candidate_identity = None
    try:
        if type(payload_source) is bytes:
            payload_size = len(payload_source)
            if payload_size > MAX_PAYLOAD_BYTES:
                raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
            payload_digest = hashlib.sha256(payload_source).hexdigest()
            payload_crc = binascii.crc32(payload_source) & 0xFFFFFFFF
            payload_chunks = _bounded_chunks(payload_source)
        else:
            payload = bind_rooted_regular(resolved_backend, payload_source)
            payload_snapshot = payload.snapshot()
            payload_digest, payload_crc = _measure_bound_payload(payload, payload_snapshot)
            payload_size = payload_snapshot.byte_count
            payload_chunks = iter_bound_chunks(payload, payload_snapshot)
        if (
            payload_digest != manifest.payload.sha256
            or payload_size != manifest.payload.byte_count
        ):
            raise ResourcePortabilityError("RESOURCE.PACKAGE.DIGEST_MISMATCH")
        chunks, artifact_size = _package_chunks(
            manifest_bytes,
            manifest.payload.path.encode("utf-8"),
            payload_chunks,
            payload_size,
            payload_crc,
        )
        if artifact_size > MAX_ARTIFACT_BYTES:
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
        root = resolved_backend.bind_root(destination.parent)
        parent = resolved_backend.bind_parent(
            root,
            _platform_relative_path(destination.name),
        )
        if parent.inspect_entry(destination.name) is not None:
            raise ResourcePortabilityError("RESOURCE.EXPORT.STAGE_FAILED")
        candidate_name = f".resource-package-{hashlib.sha256(destination.name.encode('utf-8')).hexdigest()[:16]}-{os.urandom(8).hex()}.tmp"
        candidate = parent.create_candidate(candidate_name, private=False)
        candidate_identity = candidate.identity()
        written = candidate.write_chunks(chunks, maximum_bytes=MAX_ARTIFACT_BYTES)
        if written.byte_count != artifact_size:
            raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
        candidate.flush_content()
        pending = parent.begin_publish(
            candidate,
            destination.name,
            mode=PublishMode.CREATE_IF_ABSENT,
            lease=None,
        )
        candidate = None
        candidate_identity = None
        preliminary = pending.preliminary_facts()
        retained = pending.retained_destination()
        retained_snapshot = retained.snapshot()
        if (
            preliminary.content_sha256 != written.content_sha256
            or preliminary.byte_count != written.byte_count
            or retained_snapshot.byte_count != written.byte_count
            or digest_bound(retained, retained_snapshot) != written.content_sha256
            or parent.inspect_entry(destination.name) != retained_snapshot
        ):
            raise ResourcePortabilityError("RESOURCE.PACKAGE.DIGEST_MISMATCH")
        with open_resource_package(destination, backend=resolved_backend) as sealed:
            report = sealed.validation
        if (
            report.artifact_digest != written.content_sha256.hex()
            or report.artifact_byte_count != written.byte_count
            or parent.inspect_entry(destination.name) != retained_snapshot
            or pending.terminal_reproof() != preliminary
        ):
            raise ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
        return report
    except BaseException:
        if candidate is not None:
            try:
                candidate.close()
            except BaseException:
                pass
        if pending is not None and parent is not None:
            try:
                published_identity = pending.preliminary_facts().destination_identity
                pending.close()
                pending = None
                parent.unlink_owned(destination.name, published_identity)
            except BaseException:
                pass
        if (
            candidate_name is not None
            and candidate_identity is not None
            and parent is not None
        ):
            try:
                parent.unlink_owned(candidate_name, candidate_identity)
            except BaseException:
                pass
        raise
    finally:
        primary = sys.exception()
        close_error = None
        for authority in (pending, parent, root, payload):
            if authority is not None:
                try:
                    authority.close()
                except BaseException as error:
                    if close_error is None:
                        close_error = error
        if primary is None and close_error is not None:
            raise close_error


def open_resource_package(
    source: Path,
    *,
    backend: PlatformFileBackend | None = None,
) -> SealedResourcePackage:
    """Open, raw-validate and retain one exact ResourcePackage artifact."""

    _require_absolute_path(source, "package source")
    root = None
    parent = None
    opened = None
    try:
        resolved_backend = _platform_backend(backend, source.parent)
        root = resolved_backend.bind_root(source.parent)
        parent = resolved_backend.bind_parent(
            root,
            _platform_relative_path(source.name),
        )
        opened = resolved_backend.open_regular(
            root,
            _platform_relative_path(source.name),
        )
    except PlatformFileError:
        for authority in (opened, parent, root):
            if authority is not None:
                authority.close()
        raise ResourcePortabilityError("RESOURCE.PACKAGE.SOURCE_UNSAFE") from None
    try:
        snapshot = opened.snapshot()
        artifact_size = snapshot.byte_count
        if artifact_size > MAX_ARTIFACT_BYTES or artifact_size < _EOCD.size:
            raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
        members = _parse_carrier(opened, snapshot)
        manifest_member, payload_member = members
        manifest_bytes = _read_member_bytes(
            opened,
            snapshot,
            manifest_member,
            MAX_MANIFEST_BYTES,
        )
        manifest = manifest_from_bytes(manifest_bytes)
        expected_payload_name = manifest.payload.path.encode("utf-8")
        if payload_member.name != expected_payload_name:
            raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
        payload_digest, payload_crc = _digest_bound_member(
            opened,
            snapshot,
            payload_member,
        )
        if (
            payload_digest != manifest.payload.sha256
            or payload_member.byte_count != manifest.payload.byte_count
            or payload_crc != payload_member.crc32
        ):
            raise ResourcePortabilityError("RESOURCE.PACKAGE.DIGEST_MISMATCH")
        artifact_digest = digest_bound(opened, snapshot).hex()
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
        if (
            opened.snapshot() != snapshot
            or parent.inspect_entry(source.name) != snapshot
        ):
            raise ResourcePortabilityError("RESOURCE.PACKAGE.SOURCE_UNSAFE")
        return SealedResourcePackage(
            backend=resolved_backend,
            root=root,
            parent=parent,
            source=opened,
            source_name=source.name,
            snapshot=snapshot,
            artifact_digest=artifact_digest,
            manifest=manifest,
            validation=report,
            payload=payload_member,
        )
    except BaseException:
        for authority in (opened, parent, root):
            if authority is not None:
                authority.close()
        raise


def validate_resource_package(
    source: Path,
    *,
    backend: PlatformFileBackend | None = None,
) -> ResourcePackageValidationReport:
    with open_resource_package(source, backend=backend) as sealed:
        return sealed.validation


def _measure_bound_payload(
    source: BoundRegularFile,
    snapshot: EntrySnapshot,
) -> tuple[str, int]:
    if snapshot.byte_count > MAX_PAYLOAD_BYTES:
        raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
    digest = hashlib.sha256()
    crc = 0
    for chunk in iter_bound_chunks(source, snapshot):
        digest.update(chunk)
        crc = binascii.crc32(chunk, crc)
    return digest.hexdigest(), crc & 0xFFFFFFFF


def _package_chunks(
    manifest_bytes: bytes,
    payload_name: bytes,
    payload_chunks: Iterator[bytes],
    payload_size: int,
    payload_crc: int,
) -> tuple[Iterator[bytes], int]:
    manifest_crc = binascii.crc32(manifest_bytes) & 0xFFFFFFFF
    first = _CarrierMember(
        name=_MANIFEST_NAME,
        crc32=manifest_crc,
        byte_count=len(manifest_bytes),
        local_offset=0,
        data_offset=_LOCAL.size + len(_MANIFEST_NAME),
    )
    second_offset = first.data_offset + first.byte_count
    second = _CarrierMember(
        name=payload_name,
        crc32=payload_crc,
        byte_count=payload_size,
        local_offset=second_offset,
        data_offset=second_offset + _LOCAL.size + len(payload_name),
    )
    central_offset = second.data_offset + second.byte_count
    central_parts = tuple(_central_bytes(member) for member in (first, second))
    central_size = sum(len(item) for item in central_parts)
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
    artifact_size = central_offset + central_size + len(eocd)

    def chunks() -> Iterator[bytes]:
        yield from _bounded_chunks(_local_header(first))
        yield from _bounded_chunks(first.name)
        yield from _bounded_chunks(manifest_bytes)
        yield from _bounded_chunks(_local_header(second))
        yield from _bounded_chunks(second.name)
        yield from payload_chunks
        for central in central_parts:
            yield from _bounded_chunks(central)
        yield from _bounded_chunks(eocd)

    return chunks(), artifact_size


def _local_header(member: _CarrierMember) -> bytes:
    return _LOCAL.pack(
        _LOCAL_SIGNATURE,
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
    )


def _central_bytes(member: _CarrierMember) -> bytes:
    return _CENTRAL.pack(
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
    ) + member.name


def _bounded_chunks(payload: bytes) -> Iterator[bytes]:
    for offset in range(0, len(payload), _BOUND_READ_CHUNK):
        yield payload[offset : offset + _BOUND_READ_CHUNK]


def _parse_carrier(
    source: BoundRegularFile,
    snapshot: EntrySnapshot,
) -> tuple[_CarrierMember, _CarrierMember]:
    artifact_size = snapshot.byte_count
    eocd_offset = artifact_size - _EOCD.size
    eocd = _read_exact_bound(source, snapshot, eocd_offset, _EOCD.size)
    values = _EOCD.unpack(eocd)
    if values[:5] != (_EOCD_SIGNATURE, 0, 0, 2, 2) or values[7] != 0:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.FORMAT_UNSUPPORTED")
    central_size, central_offset = values[5], values[6]
    if central_offset + central_size != eocd_offset:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
    cursor = central_offset
    central_members: list[_CarrierMember] = []
    for _index in range(2):
        fixed = _read_exact_bound(source, snapshot, cursor, _CENTRAL.size)
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
        name = _read_exact_bound(
            source,
            snapshot,
            cursor + _CENTRAL.size,
            name_length,
        )
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
        fixed = _read_exact_bound(
            source,
            snapshot,
            central.local_offset,
            _LOCAL.size,
        )
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
        name = _read_exact_bound(
            source,
            snapshot,
            central.local_offset + _LOCAL.size,
            item[9],
        )
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


def _read_member_bytes(
    source: BoundRegularFile,
    snapshot: EntrySnapshot,
    member: _CarrierMember,
    limit: int,
) -> bytes:
    if member.byte_count > limit:
        raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
    payload = _read_exact_bound(
        source,
        snapshot,
        member.data_offset,
        member.byte_count,
    )
    if binascii.crc32(payload) & 0xFFFFFFFF != member.crc32:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.DIGEST_MISMATCH")
    return payload


def _digest_bound_member(
    source: BoundRegularFile,
    snapshot: EntrySnapshot,
    member: _CarrierMember,
) -> tuple[str, int]:
    if member.byte_count > MAX_PAYLOAD_BYTES:
        raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
    remaining = member.byte_count
    offset = member.data_offset
    digest = hashlib.sha256()
    crc = 0
    while remaining:
        chunk = source.read_at(
            offset,
            min(_BOUND_READ_CHUNK, remaining),
            snapshot,
        )
        if not chunk:
            raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
        digest.update(chunk)
        crc = binascii.crc32(chunk, crc)
        remaining -= len(chunk)
        offset += len(chunk)
    return digest.hexdigest(), crc & 0xFFFFFFFF


def _read_exact_bound(
    source: BoundRegularFile,
    snapshot: EntrySnapshot,
    offset: int,
    size: int,
) -> bytes:
    if type(offset) is not int or type(size) is not int or offset < 0 or size < 0:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
    if offset + size > snapshot.byte_count:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.MEMBER_INVALID")
    chunks: list[bytes] = []
    cursor = offset
    remaining = size
    while remaining:
        chunk = source.read_at(
            cursor,
            min(_BOUND_READ_CHUNK, remaining),
            snapshot,
        )
        chunks.append(chunk)
        cursor += len(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _require_absolute_path(path: object, name: str) -> None:
    if type(path) is not _NATIVE_PATH_TYPE or not path.is_absolute():
        raise TypeError(f"{name} must be an absolute Path")


def _platform_backend(
    backend: PlatformFileBackend | None,
    probe_root: Path,
) -> PlatformFileBackend:
    if backend is not None:
        if not isinstance(backend, PlatformFileBackend):
            raise TypeError("resource package backend must satisfy PlatformFileBackend")
        return backend
    from platform_fs import compose_platform_file_backend

    return compose_platform_file_backend(probe_root)


__all__ = [
    "write_resource_package_bytes",
    "ResourcePackageArtifact",
    "SealedResourcePackage",
    "open_resource_package",
    "validate_resource_package",
    "write_resource_package",
]
