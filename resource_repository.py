"""Persistent, Qt-free registry for local translation resources."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from editor_contracts import ResourceConfig, ResourceKind


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 1


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
    ) -> None:
        self.config_dir = config_dir.expanduser().resolve()
        self.managed_dir = self.config_dir / "resources"
        self.registry_path = self.config_dir / "resources.json"
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            self.managed_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ResourceError(f"unable to prepare resource directory: {exc}") from exc

        if self.registry_path.exists():
            self._resources = self._read_registry()
        else:
            self._resources = self._bootstrap(default_tm_path, default_termbase_path)
            self._write_registry(self._resources)
        self._prepared_creates: dict[
            str,
            tuple[PreparedResourceCreate, tuple[ResourceConfig, ...]],
        ] = {}

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
        path = (self.managed_dir / f"{stem}-{resource_id[:8]}{suffix}").resolve()
        if not path.is_relative_to(self.managed_dir):
            raise ResourceError("managed resource path escaped the application directory")

        resource = ResourceConfig(
            id=resource_id,
            name=clean_name,
            kind=normalized_kind,
            path=path,
        )
        try:
            encoding = (
                "utf-8"
                if normalized_kind is ResourceKind.TRANSLATION_MEMORY
                else "utf-8-sig"
            )
            path.write_text("", encoding=encoding)
            updated = [*self._resources, resource]
            self._write_registry(updated)
        except (OSError, ResourceError):
            path.unlink(missing_ok=True)
            raise
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
        path = (self.managed_dir / f"{stem}-{resource_id[:8]}{suffix}").resolve()
        if not path.is_relative_to(self.managed_dir) or path.exists():
            raise ResourceError("managed resource create path is unavailable")
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
        if tuple(self._resources) != issued[1]:
            raise ResourceError("resource registry changed after create preview")
        resource = prepared.resource
        try:
            observed = os.lstat(resource.path)
        except OSError as error:
            raise ResourceError("prepared resource file is unavailable") from error
        if not resource.path.is_relative_to(self.managed_dir) or not (
            stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1
        ):
            raise ResourceError("prepared resource file is unsafe")
        updated = [*self._resources, resource]
        self._write_registry(updated)
        self._resources = updated
        del self._prepared_creates[prepared.operation_id]
        return resource

    def cancel_prepared_create(
        self,
        prepared: PreparedResourceCreate,
        *,
        remove_owned_file: bool = False,
    ) -> None:
        """Cancel one unpublished identity and optionally remove its exact path."""

        if type(prepared) is not PreparedResourceCreate:
            raise TypeError("prepared resource create must be exact")
        issued = self._prepared_creates.get(prepared.operation_id)
        if issued is None or issued[0] is not prepared:
            raise ResourceError("prepared resource create is stale")
        if remove_owned_file and prepared.resource.path.exists():
            observed = os.lstat(prepared.resource.path)
            if (
                not prepared.resource.path.is_relative_to(self.managed_dir)
                or not stat.S_ISREG(observed.st_mode)
                or observed.st_nlink != 1
            ):
                raise ResourceError("prepared resource file is unsafe")
            prepared.resource.path.unlink()
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
        if (
            type(expected_digest) is not str
            or len(expected_digest) != 64
            or any(character not in "0123456789abcdef" for character in expected_digest)
        ):
            raise ResourceError("recovered resource digest is invalid")
        path = (self.managed_dir / relative_path).resolve()
        if not path.is_relative_to(self.managed_dir):
            raise ResourceError("recovered resource path escaped the managed directory")
        resource = ResourceConfig(
            id=resource_id,
            name=name,
            kind=kind,
            path=path,
        )
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
        try:
            observed = os.lstat(path)
            actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise ResourceError("recovered resource file is unavailable") from error
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or actual_digest != expected_digest
        ):
            raise ResourceError("recovered resource file is not owner-proven")
        updated = [*self._resources, resource]
        self._write_registry(updated)
        self._resources = updated
        return resource

    def update_resource(self, resource: ResourceConfig) -> ResourceConfig:
        """Atomically persist editable metadata and Lookup/Update state."""

        current = self.get(resource.id)
        if current.path != resource.path or current.kind is not resource.kind:
            raise ResourceError("resource path and kind are immutable")
        if not resource.name.strip():
            raise ResourceError("resource name must not be empty")
        if not all(isinstance(value, bool) for value in (resource.active, resource.lookup, resource.update)):
            raise ResourceError("resource state flags must be booleans")

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
        self._write_registry(updated)
        self._resources = updated
        return replacement

    def delete_resource(self, resource_id: str) -> ResourceConfig:
        """Unregister one resource and remove only files owned by this repository."""

        resource = self.get(resource_id)
        updated = [
            configured
            for configured in self._resources
            if configured.id != resource_id
        ]
        try:
            managed_path = resource.path.resolve(strict=False).is_relative_to(
                self.managed_dir
            )
        except (OSError, RuntimeError) as exc:
            raise ResourceError(f"unable to resolve resource path safely: {exc}") from exc
        tombstone: Path | None = None
        if managed_path and resource.path.exists():
            if not resource.path.is_file():
                raise ResourceError(
                    f"managed resource is not a regular file: {resource.path}"
                )
            tombstone = (
                self.managed_dir
                / f".{resource.path.name}.{uuid4().hex}.deleted"
            )
            try:
                os.replace(resource.path, tombstone)
            except OSError as exc:
                raise ResourceError(f"unable to stage resource deletion: {exc}") from exc
        try:
            self._write_registry(updated)
        except ResourceError:
            if tombstone is not None:
                try:
                    os.replace(tombstone, resource.path)
                except OSError as rollback_exc:
                    raise ResourceError(
                        "unable to save resource registry and deletion rollback failed: "
                        f"{rollback_exc}"
                    ) from rollback_exc
            raise

        self._resources = updated
        if tombstone is not None:
            try:
                tombstone.unlink()
            except OSError as exc:
                LOGGER.warning("Unable to remove resource tombstone %s: %s", tombstone, exc)
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
        try:
            payload = cast(object, json.loads(self.registry_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
        temp_path: Path | None = None
        try:
            rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.config_dir,
                prefix=".resources.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                _ = handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.registry_path)
        except (OSError, TypeError, ValueError) as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise ResourceError(f"unable to save resource registry: {exc}") from exc


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResourceError(f"resource field '{field_name}' must be a non-empty string")
    return value.strip()


def _safe_stem(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:48]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp_dir:
        repository = ResourceRepository(Path(temp_dir))
        created = repository.create_resource("Self test", ResourceKind.TRANSLATION_MEMORY)
        assert repository.get(created.id) == created
    print("Resource repository self-test passed.")
