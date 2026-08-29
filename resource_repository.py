"""Persistent, Qt-free registry for local translation resources."""

from __future__ import annotations

import json
import hashlib
import logging
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast
from uuid import uuid4

from editor_contracts import ResourceConfig, ResourceKind
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
    validate_relative_name,
)
from resource_platform_io import (
    bind_rooted_regular,
    digest_bound,
    iter_bound_chunks,
    platform_relative_path,
    read_bound_all,
)


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 1
_MAX_REGISTRY_BYTES = 4 * 1024 * 1024
_REPOSITORY_LOCK_NAME = ".resource-repository.lock"
_REPOSITORY_LOCK_PAYLOAD = b"localcat.resource-repository.lock.v1\0"


class ResourceError(RuntimeError):
    """Raised when the resource registry cannot be handled safely."""


@dataclass(frozen=True, slots=True)
class PreparedResourceCreate:
    """Repository-issued unpublished local identity for one create operation."""

    operation_id: str
    resource: ResourceConfig

    def __post_init__(self) -> None:
        if type(self.operation_id) is not str or not self.operation_id:
            raise TypeError("prepared resource operation id must be nonempty str")
        if type(self.resource) is not ResourceConfig:
            raise TypeError("prepared resource must be exact ResourceConfig")


class ResourceRepository:
    """Own configured TM/termbase metadata and managed resource files."""

    def __init__(
        self,
        config_dir: Path,
        default_tm_path: Path | None = None,
        default_termbase_path: Path | None = None,
        *,
        backend: PlatformFileBackend | None = None,
    ) -> None:
        if type(config_dir) is not type(Path()) or not config_dir.is_absolute():
            raise TypeError("resource config directory must be an absolute concrete Path")
        if backend is not None and not isinstance(backend, PlatformFileBackend):
            raise TypeError("resource repository backend must satisfy PlatformFileBackend")
        self.config_dir = config_dir
        self.managed_dir = self.config_dir / "resources"
        self.registry_path = self.config_dir / "resources.json"
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            self.managed_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ResourceError(f"unable to prepare resource directory: {exc}") from exc
        if backend is None:
            from platform_fs import compose_platform_file_backend

            backend = compose_platform_file_backend(self.config_dir)
        self._backend = backend
        root = None
        lease = None
        try:
            root, lease = self._lock_repository()
            if root.inspect_entry(self.registry_path.name) is not None:
                self._resources = self._read_registry_bound(root)
            else:
                self._resources = self._bootstrap(default_tm_path, default_termbase_path)
                self._write_registry_bound(root, lease, self._resources)
        except PlatformFileError as exc:
            raise ResourceError("unable to initialize resource registry") from exc
        finally:
            _close_authorities(lease, root)
        self._prepared_creates: dict[
            str,
            tuple[PreparedResourceCreate, tuple[ResourceConfig, ...]],
        ] = {}

    @property
    def platform_backend(self) -> PlatformFileBackend:
        return self._backend

    def list_resources(self) -> tuple[ResourceConfig, ...]:
        """Return configured resources in their persistent display order."""

        return tuple(self._resources)

    def get(self, resource_id: str) -> ResourceConfig:
        """Resolve one resource by its stable identifier."""

        for resource in self._resources:
            if resource.id == resource_id:
                return resource
        raise ResourceError(f"unknown resource: {resource_id}")

    def create_resource(self, name: str, kind: ResourceKind | str) -> ResourceConfig:
        """Create an empty TM or termbase under the managed application directory."""

        clean_name = name.strip()
        if not clean_name:
            raise ResourceError("resource name must not be empty")
        try:
            normalized_kind = kind if isinstance(kind, ResourceKind) else ResourceKind(kind)
        except (TypeError, ValueError) as exc:
            raise ResourceError(f"unsupported resource kind: {kind}") from exc

        resource_id = uuid4().hex
        suffix = (
            ".jsonl"
            if normalized_kind is ResourceKind.TRANSLATION_MEMORY
            else ".csv"
        )
        stem = _safe_stem(clean_name) or normalized_kind.value
        path = self.managed_dir / f"{stem}-{resource_id[:8]}{suffix}"

        resource = ResourceConfig(
            id=resource_id,
            name=clean_name,
            kind=normalized_kind,
            path=path,
        )
        config_root = None
        lease = None
        managed_root = None
        created_identity = None
        try:
            config_root, lease = self._lock_repository()
            self._resources = self._read_registry_bound(config_root)
            managed_root = self._backend.bind_root(self.managed_dir)
            updated = [*self._resources, resource]
            created_identity = self._publish_new_bytes(
                managed_root,
                path.name,
                (
                    b""
                    if normalized_kind is ResourceKind.TRANSLATION_MEMORY
                    else b"\xef\xbb\xbf"
                ),
                private=True,
                owner_commit=lambda _identity: self._write_registry_bound(
                    config_root,
                    lease,
                    updated,
                ),
            )
        except PlatformFileError as error:
            if (
                error.code != PlatformFileErrorCode.RECOVERY_REQUIRED.value
                and managed_root is not None
                and created_identity is not None
            ):
                try:
                    managed_root.unlink_owned(path.name, created_identity)
                except PlatformFileError:
                    pass
            raise ResourceError("unable to save resource registry") from None
        except ResourceError:
            if managed_root is not None and created_identity is not None:
                try:
                    managed_root.unlink_owned(path.name, created_identity)
                except PlatformFileError:
                    pass
            raise
        finally:
            _close_authorities(managed_root, lease, config_root)
        self._resources = updated
        LOGGER.info("Created managed %s resource %s", normalized_kind.value, resource_id)
        return resource

    def prepare_resource_create(
        self,
        name: str,
        kind: ResourceKind | str,
    ) -> PreparedResourceCreate:
        """Issue an unpublished managed identity without creating a public row."""

        clean_name = name.strip()
        if not clean_name:
            raise ResourceError("resource name must not be empty")
        try:
            normalized_kind = kind if isinstance(kind, ResourceKind) else ResourceKind(kind)
        except (TypeError, ValueError) as exc:
            raise ResourceError(f"unsupported resource kind: {kind}") from exc
        resource_id = uuid4().hex
        suffix = ".jsonl" if normalized_kind is ResourceKind.TRANSLATION_MEMORY else ".csv"
        stem = _safe_stem(clean_name) or normalized_kind.value
        path = self.managed_dir / f"{stem}-{resource_id[:8]}{suffix}"
        config_root = None
        lease = None
        managed_root = None
        try:
            config_root, lease = self._lock_repository()
            self._resources = self._read_registry_bound(config_root)
            managed_root = self._backend.bind_root(self.managed_dir)
            if managed_root.inspect_entry(path.name) is not None:
                raise ResourceError("managed resource create path is unavailable")
        except PlatformFileError as error:
            raise ResourceError("managed resource create path is unavailable") from None
        finally:
            _close_authorities(managed_root, lease, config_root)
        resource = ResourceConfig(
            id=resource_id,
            name=clean_name,
            kind=normalized_kind,
            path=path,
        )
        prepared = PreparedResourceCreate(uuid4().hex, resource)
        self._prepared_creates[prepared.operation_id] = (
            prepared,
            tuple(self._resources),
        )
        return prepared

    def publish_prepared_create(
        self,
        prepared: PreparedResourceCreate,
    ) -> ResourceConfig:
        """Publish one issued identity after its owner-created file is proven."""

        if type(prepared) is not PreparedResourceCreate:
            raise TypeError("prepared resource create must be exact")
        issued = self._prepared_creates.get(prepared.operation_id)
        if issued is None or issued[0] is not prepared:
            raise ResourceError("prepared resource create is stale")
        resource = prepared.resource
        config_root = None
        lease = None
        source = None
        try:
            config_root, lease = self._lock_repository()
            self._resources = self._read_registry_bound(config_root)
            if tuple(self._resources) != issued[1]:
                raise ResourceError("resource registry changed after create preview")
            if resource.path.parent != self.managed_dir:
                raise ResourceError("prepared resource file is unsafe")
            source = bind_rooted_regular(self._backend, resource.path)
            source.snapshot()
            updated = [*self._resources, resource]
            self._write_registry_bound(config_root, lease, updated)
        except PlatformFileError as error:
            raise ResourceError("prepared resource file is unavailable") from None
        finally:
            _close_authorities(source, lease, config_root)
        self._resources = updated
        del self._prepared_creates[prepared.operation_id]
        return resource

    def cancel_prepared_create(
        self,
        prepared: PreparedResourceCreate,
        *,
        remove_owned_file: bool = False,
        expected_identity: FileObjectIdentity | None = None,
    ) -> None:
        """Cancel one unpublished identity and optionally remove its exact path."""

        if type(prepared) is not PreparedResourceCreate:
            raise TypeError("prepared resource create must be exact")
        issued = self._prepared_creates.get(prepared.operation_id)
        if issued is None or issued[0] is not prepared:
            raise ResourceError("prepared resource create is stale")
        if remove_owned_file and type(expected_identity) is not FileObjectIdentity:
            raise ResourceError("prepared resource cleanup requires exact owner identity")
        if not remove_owned_file and expected_identity is not None:
            raise ValueError("prepared resource identity is only valid for owned cleanup")
        if remove_owned_file:
            root = None
            try:
                root = self._backend.bind_root(self.managed_dir)
                observed = root.inspect_entry(prepared.resource.path.name)
                if observed is not None:
                    root.unlink_owned(prepared.resource.path.name, expected_identity)
            except PlatformFileError as error:
                raise ResourceError("prepared resource file is unsafe") from None
            finally:
                _close_authorities(root)
        del self._prepared_creates[prepared.operation_id]

    def recover_resource_create(
        self,
        *,
        resource_id: str,
        name: str,
        kind: ResourceKind,
        relative_path: str,
        expected_digest: str,
    ) -> ResourceConfig:
        """Cold-publish one owner-proven managed file after a registry fault."""

        if type(kind) is not ResourceKind:
            raise TypeError("recovered resource kind must be exact")
        if (
            type(relative_path) is not str
            or not relative_path
            or Path(relative_path).is_absolute()
            or len(Path(relative_path).parts) != 1
        ):
            raise ResourceError("recovered resource path is unsafe")
        try:
            validate_relative_name(relative_path)
        except (TypeError, ValueError) as error:
            raise ResourceError("recovered resource path is unsafe") from error
        if (
            type(expected_digest) is not str
            or len(expected_digest) != 64
            or any(character not in "0123456789abcdef" for character in expected_digest)
        ):
            raise ResourceError("recovered resource digest is invalid")
        path = self.managed_dir / relative_path
        if path.parent != self.managed_dir:
            raise ResourceError("recovered resource path escaped the managed directory")
        resource = ResourceConfig(
            id=resource_id,
            name=name,
            kind=kind,
            path=path,
        )
        config_root = None
        lease = None
        source = None
        try:
            config_root, lease = self._lock_repository()
            self._resources = self._read_registry_bound(config_root)
            existing_id = next(
                (configured for configured in self._resources if configured.id == resource_id),
                None,
            )
            if existing_id is not None:
                if existing_id != resource:
                    raise ResourceError("recovered resource id is already claimed")
                return existing_id
            if any(configured.path == path for configured in self._resources):
                raise ResourceError("recovered resource path is already claimed")
            source = bind_rooted_regular(self._backend, path)
            snapshot = source.snapshot()
            if digest_bound(source, snapshot).hex() != expected_digest:
                raise ResourceError("recovered resource file is not owner-proven")
            updated = [*self._resources, resource]
            self._write_registry_bound(config_root, lease, updated)
        except PlatformFileError as error:
            raise ResourceError("recovered resource file is unavailable") from None
        finally:
            _close_authorities(source, lease, config_root)
        self._resources = updated
        return resource

    def update_resource(self, resource: ResourceConfig) -> ResourceConfig:
        """Atomically persist editable metadata and Lookup/Update state."""

        if not resource.name.strip():
            raise ResourceError("resource name must not be empty")
        if not all(isinstance(value, bool) for value in (resource.active, resource.lookup, resource.update)):
            raise ResourceError("resource state flags must be booleans")
        root = None
        lease = None
        try:
            root, lease = self._lock_repository()
            self._resources = self._read_registry_bound(root)
            current = self.get(resource.id)
            if current.path != resource.path or current.kind is not resource.kind:
                raise ResourceError("resource path and kind are immutable")
            replacement = ResourceConfig(
                id=current.id,
                name=resource.name.strip(),
                kind=current.kind,
                path=current.path,
                active=resource.active,
                lookup=resource.lookup,
                update=resource.update,
            )
            updated = [
                replacement if configured.id == replacement.id else configured
                for configured in self._resources
            ]
            self._write_registry_bound(root, lease, updated)
        except PlatformFileError as error:
            raise ResourceError("unable to save resource registry") from None
        finally:
            _close_authorities(lease, root)
        self._resources = updated
        return replacement

    def delete_resource(self, resource_id: str) -> ResourceConfig:
        """Unregister one resource and remove only files owned by this repository."""

        config_root = None
        lease = None
        managed_root = None
        source = None
        backup: PendingPublication | None = None
        backup_name: str | None = None
        backup_identity: FileObjectIdentity | None = None
        registry_committed = False
        preserve_backup = False
        try:
            config_root, lease = self._lock_repository()
            current_resources = self._read_registry_bound(config_root)
            resource = next(
                (
                    configured
                    for configured in current_resources
                    if configured.id == resource_id
                ),
                None,
            )
            if resource is None:
                raise ResourceError(f"unknown resource: {resource_id}")
            updated = [
                configured
                for configured in current_resources
                if configured.id != resource_id
            ]
            managed_path = resource.path.parent == self.managed_dir
            if not managed_path:
                self._write_registry_bound(config_root, lease, updated)
                registry_committed = True
                self._resources = updated
                return resource

            validate_relative_name(resource.path.name)
            managed_root = self._backend.bind_root(self.managed_dir)
            observed = managed_root.inspect_entry(resource.path.name)
            if observed is None:
                self._write_registry_bound(config_root, lease, updated)
                registry_committed = True
                self._resources = updated
                return resource
            if (
                observed.identity.kind != "regular"
                or observed.identity.link_count != 1
                or not observed.reparse_free
            ):
                raise ResourceError("managed resource deletion target is unsafe")
            source = self._backend.open_regular(
                managed_root,
                platform_relative_path(resource.path.name),
            )
            source_snapshot = source.snapshot()
            if source_snapshot != observed:
                raise ResourceError("managed resource deletion target is stale")
            backup_name = f".{resource.path.name}.{uuid4().hex}.deleted"
            backup = _stage_bound_copy(
                managed_root,
                backup_name,
                source,
                source_snapshot,
                private=True,
            )
            backup_facts = backup.preliminary_facts()
            backup_identity = backup_facts.destination_identity
            source.close()
            source = None
            managed_root.unlink_owned(resource.path.name, observed.identity)
            if managed_root.inspect_entry(resource.path.name) is not None:
                raise ResourceError("managed resource deletion is indeterminate")

            try:
                self._write_registry_bound(config_root, lease, updated)
                registry_committed = True
                self._resources = updated
            except PlatformFileError as error:
                if error.code == PlatformFileErrorCode.RECOVERY_REQUIRED.value:
                    preserve_backup = True
                    raise ResourceError("managed resource deletion requires recovery") from None
                _restore_deleted_resource(
                    managed_root,
                    resource.path.name,
                    backup,
                    source_snapshot,
                )
                raise
            except BaseException:
                _restore_deleted_resource(
                    managed_root,
                    resource.path.name,
                    backup,
                    source_snapshot,
                )
                raise

            if backup.terminal_reproof() != backup_facts:
                preserve_backup = True
                raise ResourceError("managed resource deletion requires recovery")
            backup.close()
            backup = None
            managed_root.unlink_owned(backup_name, backup_identity)
            backup_name = None
            backup_identity = None
        except PlatformFileError:
            if not registry_committed:
                preserve_backup = True
            raise ResourceError("unable to delete resource safely") from None
        finally:
            if not preserve_backup and backup is not None:
                try:
                    backup.close()
                except PlatformFileError:
                    preserve_backup = True
                backup = None
            if (
                not preserve_backup
                and backup_name is not None
                and backup_identity is not None
                and managed_root is not None
            ):
                try:
                    managed_root.unlink_owned(backup_name, backup_identity)
                except PlatformFileError:
                    pass
            _close_authorities(source, backup, managed_root, lease, config_root)
        LOGGER.info(
            "Deleted resource %s (%s file)",
            resource_id,
            "managed" if managed_path else "external",
        )
        return resource

    def _bootstrap(
        self,
        default_tm_path: Path | None,
        default_termbase_path: Path | None,
    ) -> list[ResourceConfig]:
        resources: list[ResourceConfig] = []
        candidates = (
            ("local-tm", "Local translation memory", ResourceKind.TRANSLATION_MEMORY, default_tm_path),
            ("local-termbase", "Local termbase", ResourceKind.TERMBASE, default_termbase_path),
        )
        for resource_id, name, kind, raw_path in candidates:
            if raw_path is None:
                continue
            path = raw_path.expanduser().resolve()
            if path.exists() and path.is_file():
                resources.append(
                    ResourceConfig(
                        id=resource_id,
                        name=name,
                        kind=kind,
                        path=path,
                    )
                )
        return resources

    def _read_registry(self) -> list[ResourceConfig]:
        root = None
        lease = None
        try:
            root, lease = self._lock_repository()
            return self._read_registry_bound(root)
        except PlatformFileError as exc:
            raise ResourceError("unable to read resource registry") from exc
        finally:
            _close_authorities(lease, root)

    def _read_registry_bound(
        self,
        root: RootedDirectoryAuthority,
    ) -> list[ResourceConfig]:
        source = self._backend.open_regular(
            root,
            platform_relative_path(self.registry_path.name),
        )
        try:
            snapshot = source.snapshot()
            serialized = read_bound_all(
                source,
                snapshot,
                maximum_bytes=_MAX_REGISTRY_BYTES,
            )
        finally:
            source.close()
        try:
            payload = cast(object, json.loads(serialized.decode("utf-8")))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ResourceError(f"unable to read resource registry: {exc}") from exc
        if not isinstance(payload, dict):
            raise ResourceError("resource registry root must be an object")
        mapping = cast(dict[str, object], payload)
        if mapping.get("schema_version") != SCHEMA_VERSION:
            raise ResourceError("unsupported resource registry schema")
        raw_resources = mapping.get("resources")
        if not isinstance(raw_resources, list):
            raise ResourceError("resource registry must contain a resources array")

        resources: list[ResourceConfig] = []
        seen_ids: set[str] = set()
        for index, entry in enumerate(cast(list[object], raw_resources), start=1):
            if not isinstance(entry, dict):
                raise ResourceError(f"resource entry {index} must be an object")
            item = cast(dict[str, object], entry)
            try:
                resource_id = _required_string(item.get("id"), "id")
                name = _required_string(item.get("name"), "name")
                kind = ResourceKind(_required_string(item.get("kind"), "kind"))
                raw_path = _required_string(item.get("path"), "path")
                path = Path(raw_path)
                if not path.is_absolute():
                    raise ResourceError(f"resource entry {index} path must be absolute")
                flags = tuple(item.get(field, True) for field in ("active", "lookup", "update"))
                if not all(isinstance(value, bool) for value in flags):
                    raise ResourceError(f"resource entry {index} state flags must be booleans")
                resource = ResourceConfig(
                    id=resource_id,
                    name=name,
                    kind=kind,
                    path=path,
                    active=cast(bool, flags[0]),
                    lookup=cast(bool, flags[1]),
                    update=cast(bool, flags[2]),
                )
            except (TypeError, ValueError) as exc:
                raise ResourceError(f"invalid resource entry {index}: {exc}") from exc
            if resource.id in seen_ids:
                raise ResourceError(f"duplicate resource id: {resource.id}")
            seen_ids.add(resource.id)
            resources.append(resource)
        return resources

    def _write_registry(self, resources: list[ResourceConfig]) -> None:
        root = None
        lease = None
        try:
            root, lease = self._lock_repository()
            self._write_registry_bound(root, lease, resources)
        except PlatformFileError as exc:
            raise ResourceError("unable to save resource registry") from exc
        finally:
            _close_authorities(lease, root)

    def _write_registry_bound(
        self,
        root: RootedDirectoryAuthority,
        lease: LockLease,
        resources: list[ResourceConfig],
    ) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "resources": [
                {
                    "id": resource.id,
                    "name": resource.name,
                    "kind": resource.kind.value,
                    "path": str(resource.path),
                    "active": resource.active,
                    "lookup": resource.lookup,
                    "update": resource.update,
                }
                for resource in resources
            ],
        }
        try:
            rendered = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            )
            if len(rendered) > _MAX_REGISTRY_BYTES:
                raise ResourceError("resource registry exceeds its byte limit")
            observed = root.inspect_entry(self.registry_path.name)
            self._publish_bytes(
                root,
                lease,
                self.registry_path.name,
                rendered,
                replace=observed is not None,
                private=True,
            )
        except (TypeError, ValueError) as exc:
            raise ResourceError(f"unable to save resource registry: {exc}") from exc

    def _lock_repository(
        self,
    ) -> tuple[RootedDirectoryAuthority, LockLease]:
        root = self._backend.bind_root(self.config_dir)
        try:
            lease = self._backend.acquire(
                root,
                _REPOSITORY_LOCK_NAME,
                _REPOSITORY_LOCK_PAYLOAD,
                LockPolicy(LockWait.BLOCK),
            )
        except BaseException:
            root.close()
            raise
        return root, lease

    def _publish_new_bytes(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
        *,
        private: bool,
        owner_commit: Callable[[FileObjectIdentity], None] | None = None,
    ) -> FileObjectIdentity:
        return self._publish_bytes(
            parent,
            None,
            name,
            payload,
            replace=False,
            private=private,
            owner_commit=owner_commit,
        )

    def _publish_bytes(
        self,
        parent: BoundDirectoryAuthority,
        lease: LockLease | None,
        name: str,
        payload: bytes,
        *,
        replace: bool,
        private: bool,
        owner_commit: Callable[[FileObjectIdentity], None] | None = None,
    ) -> FileObjectIdentity:
        candidate: CandidateFile | None = None
        pending: PendingPublication | None = None
        candidate_identity: FileObjectIdentity | None = None
        candidate_name = f".resource-repository-{uuid4().hex}.tmp"
        try:
            candidate = parent.create_candidate(candidate_name, private=private)
            candidate_identity = candidate.identity()
            written = candidate.write_chunks(
                _byte_chunks(payload),
                maximum_bytes=max(len(payload), _MAX_REGISTRY_BYTES),
            )
            candidate.flush_content()
            pending = parent.begin_publish(
                candidate,
                name,
                mode=(
                    PublishMode.REPLACE_UNDER_LOCK
                    if replace
                    else PublishMode.CREATE_IF_ABSENT
                ),
                lease=lease if replace else None,
            )
            candidate = None
            candidate_identity = None
            facts = pending.preliminary_facts()
            retained = pending.retained_destination()
            snapshot = retained.snapshot()
            if (
                facts.content_sha256 != written.content_sha256
                or facts.byte_count != written.byte_count
                or snapshot.byte_count != len(payload)
                or read_bound_all(
                    retained,
                    snapshot,
                    maximum_bytes=max(len(payload), _MAX_REGISTRY_BYTES),
                )
                != payload
            ):
                raise PlatformFileError(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                )
            if owner_commit is not None:
                owner_commit(facts.destination_identity)
            if pending.terminal_reproof() != facts:
                raise PlatformFileError(
                    PlatformFileErrorCode.RECOVERY_REQUIRED,
                    retryable=True,
                )
            return facts.destination_identity
        finally:
            _close_authorities(pending, candidate)
            if candidate_identity is not None:
                try:
                    parent.unlink_owned(candidate_name, candidate_identity)
                except PlatformFileError:
                    pass


def _stage_bound_copy(
    parent: BoundDirectoryAuthority,
    destination_name: str,
    source: BoundRegularFile,
    source_snapshot: EntrySnapshot,
    *,
    private: bool,
) -> PendingPublication:
    candidate: CandidateFile | None = None
    pending: PendingPublication | None = None
    candidate_identity: FileObjectIdentity | None = None
    candidate_name = f".resource-repository-{uuid4().hex}.tmp"
    preserve_residue = False
    try:
        candidate = parent.create_candidate(candidate_name, private=private)
        candidate_identity = candidate.identity()
        source_digest = digest_bound(source, source_snapshot)
        written = candidate.write_chunks(
            iter_bound_chunks(source, source_snapshot),
            maximum_bytes=source_snapshot.byte_count,
        )
        candidate.flush_content()
        try:
            pending = parent.begin_publish(
                candidate,
                destination_name,
                mode=PublishMode.CREATE_IF_ABSENT,
                lease=None,
            )
        except PlatformFileError as error:
            preserve_residue = (
                error.code == PlatformFileErrorCode.RECOVERY_REQUIRED.value
            )
            raise
        candidate = None
        candidate_identity = None
        facts = pending.preliminary_facts()
        retained = pending.retained_destination()
        retained_snapshot = retained.snapshot()
        if (
            written.content_sha256 != source_digest
            or facts.content_sha256 != source_digest
            or written.byte_count != source_snapshot.byte_count
            or facts.byte_count != source_snapshot.byte_count
            or retained_snapshot.byte_count != source_snapshot.byte_count
            or digest_bound(retained, retained_snapshot) != source_digest
        ):
            preserve_residue = True
            raise PlatformFileError(
                PlatformFileErrorCode.RECOVERY_REQUIRED,
                retryable=True,
            )
        result = pending
        pending = None
        return result
    except BaseException:
        if pending is not None:
            facts = pending.preliminary_facts()
            pending.close()
            if not preserve_residue:
                try:
                    parent.unlink_owned(destination_name, facts.destination_identity)
                except PlatformFileError:
                    pass
        raise
    finally:
        _close_authorities(candidate)
        if (
            not preserve_residue
            and candidate_identity is not None
        ):
            try:
                parent.unlink_owned(candidate_name, candidate_identity)
            except PlatformFileError:
                pass


def _restore_deleted_resource(
    parent: BoundDirectoryAuthority,
    destination_name: str,
    backup: PendingPublication,
    original_snapshot: EntrySnapshot,
) -> None:
    retained = backup.retained_destination()
    retained_snapshot = retained.snapshot()
    if (
        retained_snapshot.byte_count != original_snapshot.byte_count
        or digest_bound(retained, retained_snapshot)
        != backup.preliminary_facts().content_sha256
    ):
        raise PlatformFileError(
            PlatformFileErrorCode.RECOVERY_REQUIRED,
            retryable=True,
        )
    restored = _stage_bound_copy(
        parent,
        destination_name,
        retained,
        retained_snapshot,
        private=True,
    )
    try:
        facts = restored.preliminary_facts()
        if restored.terminal_reproof() != facts:
            raise PlatformFileError(
                PlatformFileErrorCode.RECOVERY_REQUIRED,
                retryable=True,
            )
    finally:
        restored.close()


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResourceError(f"resource field '{field_name}' must be a non-empty string")
    return value.strip()


def _safe_stem(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:48]


def _byte_chunks(payload: bytes) -> tuple[bytes, ...]:
    return tuple(
        payload[offset : offset + 64 * 1024]
        for offset in range(0, len(payload), 64 * 1024)
    )


def _close_authorities(*authorities: object) -> None:
    for authority in authorities:
        if authority is not None:
            try:
                authority.close()  # type: ignore[attr-defined]
            except PlatformFileError:
                pass


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp_dir:
        repository = ResourceRepository(Path(temp_dir))
        created = repository.create_resource("Self test", ResourceKind.TRANSLATION_MEMORY)
        assert repository.get(created.id) == created
    print("Resource repository self-test passed.")
