"""Bound destination publication for direct and packaged resource artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import secrets
from typing import Callable, TypeVar

from platform_fs_contracts import (
    BoundDirectoryAuthority,
    BoundRegularFile,
    CandidateFile,
    EntrySnapshot,
    FileObjectIdentity,
    LockLease,
    LockPolicy,
    LockWait,
    PendingPublication,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PublishMode,
    RootedDirectoryAuthority,
)
from resource_package_contracts import ResourcePortabilityError
from resource_platform_io import (
    bind_rooted_regular,
    digest_bound,
    iter_bound_chunks,
    platform_relative_path,
)


_ValidationT = TypeVar("_ValidationT")
_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_LOCK_PREFIX = b"localcat.resource-artifact.lock.v1\0"


@dataclass(frozen=True, slots=True)
class ResourceArtifactPublication:
    destination_before_digest: str | None
    destination_after_digest: str


class ResourceArtifactSaveService:
    """Publish one validated candidate through the shared rooted lifecycle."""

    def __init__(self, backend: PlatformFileBackend | None = None) -> None:
        if backend is not None and not isinstance(backend, PlatformFileBackend):
            raise TypeError("resource artifact backend must satisfy PlatformFileBackend")
        self._backend = backend

    def publish(
        self,
        candidate: Path,
        destination: Path,
        validator: Callable[[Path], _ValidationT],
        *,
        owner_commit: Callable[[ResourceArtifactPublication, _ValidationT], None],
    ) -> tuple[ResourceArtifactPublication, _ValidationT]:
        if type(candidate) is not type(Path()) or not candidate.is_absolute():
            raise TypeError("artifact candidate must be an absolute concrete Path")
        if type(destination) is not type(Path()) or not destination.is_absolute():
            raise TypeError("artifact destination must be an absolute concrete Path")
        if candidate.parent != destination.parent or candidate == destination:
            raise ValueError("artifact candidate must share the destination directory")
        if not callable(validator):
            raise TypeError("artifact validator must be callable")
        if not callable(owner_commit):
            raise TypeError("artifact owner commit must be callable")
        backend = self._backend
        if backend is None:
            from platform_fs import compose_platform_file_backend

            try:
                backend = compose_platform_file_backend(destination.parent)
            except PlatformFileError as error:
                raise _map_platform_error(error, published=False) from None

        source: BoundRegularFile | None = None
        source_root: RootedDirectoryAuthority | None = None
        source_parent: BoundDirectoryAuthority | None = None
        root: RootedDirectoryAuthority | None = None
        parent: BoundDirectoryAuthority | None = None
        lease: LockLease | None = None
        pending: PendingPublication | None = None
        lkg_name: str | None = None
        lkg_identity: FileObjectIdentity | None = None
        published = False
        before: EntrySnapshot | None = None
        before_digest: str | None = None
        output_candidate: CandidateFile | None = None
        output_candidate_name: str | None = None
        output_candidate_identity: FileObjectIdentity | None = None
        owner_commit_started = False
        preserve_recovery = False
        try:
            source = bind_rooted_regular(backend, candidate)
            source_snapshot = source.snapshot()
            if source_snapshot.byte_count > _MAX_ARTIFACT_BYTES:
                raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
            source_digest = digest_bound(source, source_snapshot)
            source_root = backend.bind_root(candidate.parent)
            source_parent = backend.bind_parent(
                source_root,
                platform_relative_path(candidate.name),
            )
            if source_parent.inspect_entry(candidate.name) != source_snapshot:
                raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_CHANGED")
            try:
                validator(candidate)
            except Exception as error:
                raise ResourcePortabilityError(
                    "RESOURCE.EXPORT.VALIDATION_FAILED"
                ) from error
            if (
                source.snapshot() != source_snapshot
                or source_parent.inspect_entry(candidate.name) != source_snapshot
            ):
                raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_CHANGED")

            root = backend.bind_root(destination.parent)
            parent = backend.bind_parent(root, platform_relative_path(destination.name))
            lease = backend.acquire(
                parent,
                _lock_name(destination.name),
                _lock_payload(destination.name),
                LockPolicy(LockWait.BLOCK),
            )
            before = parent.inspect_entry(destination.name)
            _require_safe_snapshot(before)
            if before is not None:
                prior = backend.open_regular(root, platform_relative_path(destination.name))
                try:
                    if prior.snapshot() != before:
                        raise ResourcePortabilityError(
                            "RESOURCE.EXPORT.DESTINATION_STALE"
                        )
                    before_digest = digest_bound(prior, before).hex()
                    lkg_name = _temporary_name(destination.name, "lkg")
                    lkg_candidate = _candidate_from_bound(parent, lkg_name, prior, before)
                    lkg_pending = parent.begin_publish(
                        lkg_candidate,
                        lkg_name,
                        mode=PublishMode.CREATE_IF_ABSENT,
                        lease=None,
                    )
                    try:
                        lkg_facts = lkg_pending.preliminary_facts()
                        lkg_retained = lkg_pending.retained_destination()
                        lkg_snapshot = lkg_retained.snapshot()
                        if (
                            lkg_facts.content_sha256.hex() != before_digest
                            or lkg_snapshot.byte_count != before.byte_count
                            or digest_bound(lkg_retained, lkg_snapshot)
                            != lkg_facts.content_sha256
                            or lkg_pending.terminal_reproof() != lkg_facts
                        ):
                            raise ResourcePortabilityError(
                                "RESOURCE.EXPORT.STAGE_FAILED"
                            )
                        lkg_identity = lkg_facts.destination_identity
                    finally:
                        lkg_pending.close()
                finally:
                    prior.close()

            if parent.inspect_entry(destination.name) != before:
                raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")
            output_candidate_name = _temporary_name(destination.name, "candidate")
            output_candidate = _candidate_from_bound(
                parent,
                output_candidate_name,
                source,
                source_snapshot,
            )
            output_candidate_identity = output_candidate.identity()
            mode = PublishMode.CREATE_IF_ABSENT if before is None else PublishMode.REPLACE_UNDER_LOCK
            try:
                pending = parent.begin_publish(
                    output_candidate,
                    destination.name,
                    mode=mode,
                    lease=lease if mode is PublishMode.REPLACE_UNDER_LOCK else None,
                )
            except PlatformFileError as error:
                if error.code == PlatformFileErrorCode.RECOVERY_REQUIRED.value:
                    published = True
                    preserve_recovery = True
                raise
            output_candidate = None
            output_candidate_identity = None
            published = True
            facts = pending.preliminary_facts()
            retained = pending.retained_destination()
            retained_snapshot = retained.snapshot()
            if (
                facts.content_sha256 != source_digest
                or facts.byte_count != source_snapshot.byte_count
                or retained_snapshot.byte_count != source_snapshot.byte_count
                or digest_bound(retained, retained_snapshot) != source_digest
            ):
                raise ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
            try:
                if parent.inspect_entry(destination.name) != retained_snapshot:
                    raise ResourcePortabilityError(
                        "RESOURCE.EXPORT.RECOVERY_REQUIRED"
                    )
                validation = validator(destination)
            except Exception as error:
                raise ResourcePortabilityError(
                    "RESOURCE.EXPORT.VALIDATION_FAILED"
                ) from error
            if parent.inspect_entry(destination.name) != retained_snapshot:
                raise ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
            publication = ResourceArtifactPublication(
                destination_before_digest=before_digest,
                destination_after_digest=source_digest.hex(),
            )
            owner_commit_started = True
            owner_commit(publication, validation)
            if (
                parent.inspect_entry(destination.name) != retained_snapshot
                or pending.terminal_reproof() != facts
            ):
                raise ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
            if lkg_name is not None and lkg_identity is not None:
                parent.unlink_owned(lkg_name, lkg_identity)
                lkg_name = None
                lkg_identity = None
            return publication, validation
        except BaseException as primary:
            if (
                published
                and not owner_commit_started
                and pending is not None
                and parent is not None
                and lease is not None
            ):
                try:
                    _restore_bound_publication(
                        backend,
                        root,
                        parent,
                        lease,
                        pending,
                        destination.name,
                        before,
                        lkg_name,
                    )
                except BaseException as rollback_error:
                    raise ResourcePortabilityError(
                        "RESOURCE.EXPORT.RECOVERY_REQUIRED"
                    ) from rollback_error
            elif published:
                preserve_recovery = True
            if isinstance(primary, ResourcePortabilityError):
                raise primary
            if isinstance(primary, PlatformFileError):
                raise _map_platform_error(primary, published=published) from None
            raise ResourcePortabilityError(
                "RESOURCE.EXPORT.PUBLICATION_FAILED"
            ) from primary
        finally:
            if (
                not preserve_recovery
                and lkg_name is not None
                and lkg_identity is not None
                and parent is not None
            ):
                try:
                    parent.unlink_owned(lkg_name, lkg_identity)
                except PlatformFileError:
                    pass
            if output_candidate is not None:
                try:
                    output_candidate.close()
                except PlatformFileError:
                    pass
            if (
                not preserve_recovery
                and output_candidate_name is not None
                and output_candidate_identity is not None
                and parent is not None
            ):
                try:
                    parent.unlink_owned(
                        output_candidate_name,
                        output_candidate_identity,
                    )
                except PlatformFileError:
                    pass
            for authority in (
                pending,
                lease,
                parent,
                root,
                source_parent,
                source_root,
                source,
            ):
                if authority is not None:
                    try:
                        authority.close()
                    except PlatformFileError:
                        pass


def _candidate_from_bound(
    parent: BoundDirectoryAuthority,
    name: str,
    source: BoundRegularFile,
    snapshot: EntrySnapshot,
) -> CandidateFile:
    candidate: CandidateFile | None = None
    identity: FileObjectIdentity | None = None
    try:
        candidate = parent.create_candidate(name, private=False)
        identity = candidate.identity()
        facts = candidate.write_chunks(
            iter_bound_chunks(source, snapshot),
            maximum_bytes=_MAX_ARTIFACT_BYTES,
        )
        if facts.byte_count != snapshot.byte_count:
            raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_CHANGED")
        candidate.flush_content()
        return candidate
    except BaseException:
        if candidate is not None:
            candidate.close()
        if identity is not None:
            try:
                parent.unlink_owned(name, identity)
            except PlatformFileError:
                pass
        raise


def _restore_bound_publication(
    backend: PlatformFileBackend,
    root: RootedDirectoryAuthority | None,
    parent: BoundDirectoryAuthority,
    lease: LockLease,
    pending: PendingPublication,
    destination_name: str,
    before: EntrySnapshot | None,
    lkg_name: str | None,
) -> None:
    if root is None:
        raise ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
    published = pending.preliminary_facts()
    observed = parent.inspect_entry(destination_name)
    if observed is None or observed.identity != published.destination_identity:
        raise ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
    pending.close()
    if before is None:
        parent.unlink_owned(destination_name, published.destination_identity)
        if parent.inspect_entry(destination_name) is not None:
            raise ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
        return
    if lkg_name is None:
        raise ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
    lkg = backend.open_regular(root, platform_relative_path(lkg_name))
    try:
        lkg_snapshot = lkg.snapshot()
        parent.unlink_owned(destination_name, published.destination_identity)
        if parent.inspect_entry(destination_name) is not None:
            raise ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
        rollback_candidate = _candidate_from_bound(
            parent,
            _temporary_name(destination_name, "rollback"),
            lkg,
            lkg_snapshot,
        )
        rollback = parent.begin_publish(
            rollback_candidate,
            destination_name,
            mode=PublishMode.CREATE_IF_ABSENT,
            lease=None,
        )
        try:
            facts = rollback.preliminary_facts()
            retained = rollback.retained_destination()
            retained_snapshot = retained.snapshot()
            lkg_digest = digest_bound(lkg, lkg_snapshot)
            if (
                facts.content_sha256 != lkg_digest
                or retained_snapshot.byte_count != lkg_snapshot.byte_count
                or digest_bound(retained, retained_snapshot) != lkg_digest
                or rollback.terminal_reproof() != facts
            ):
                raise ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
        finally:
            rollback.close()
    finally:
        lkg.close()


def _require_safe_snapshot(snapshot: EntrySnapshot | None) -> None:
    if snapshot is not None and (
        snapshot.identity.kind != "regular"
        or snapshot.identity.link_count != 1
        or not snapshot.reparse_free
    ):
        raise ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")


def _lock_name(destination_name: str) -> str:
    digest = hashlib.sha256(destination_name.encode("utf-8", "strict")).hexdigest()
    return f".resource-artifact-{digest[:32]}.lock"


def _lock_payload(destination_name: str) -> bytes:
    return _LOCK_PREFIX + hashlib.sha256(
        destination_name.encode("utf-8", "strict")
    ).digest()


def _temporary_name(destination_name: str, role: str) -> str:
    digest = hashlib.sha256(destination_name.encode("utf-8", "strict")).hexdigest()
    return f".resource-{role}-{digest[:16]}-{secrets.token_hex(8)}.tmp"


def _map_platform_error(
    error: PlatformFileError,
    *,
    published: bool,
) -> ResourcePortabilityError:
    if published or error.code == PlatformFileErrorCode.RECOVERY_REQUIRED.value:
        return ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
    if error.code in {
        PlatformFileErrorCode.IDENTITY_STALE.value,
        PlatformFileErrorCode.REPARSE_REJECTED.value,
        PlatformFileErrorCode.OUTSIDE_ROOT.value,
        PlatformFileErrorCode.ENTRY_UNAVAILABLE.value,
    }:
        return ResourcePortabilityError("RESOURCE.EXPORT.DESTINATION_STALE")
    return ResourcePortabilityError("RESOURCE.EXPORT.PUBLICATION_FAILED")


__all__ = [
    "ResourceArtifactPublication",
    "ResourceArtifactSaveService",
]
