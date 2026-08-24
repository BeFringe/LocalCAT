"""Narrow injected payload ports used by the ResourcePackage owner."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from editor_contracts import ResourceConfig
from resource_package_contracts import (
    PortableResourceSnapshot,
    ResourcePayloadProfile,
)


@runtime_checkable
class ResourcePackagePayloadHandler(Protocol):
    """Semantic payload owner; contains no manifest, ZIP or receipt behavior."""

    @property
    def profile(self) -> ResourcePayloadProfile:
        """Return the one exact payload profile handled by this port."""

    def export_snapshot(
        self,
        resource: ResourceConfig,
        destination: Path,
    ) -> PortableResourceSnapshot:
        """Write one deterministic payload from a complete managed resource."""

    def validate_snapshot(self, source: Path) -> PortableResourceSnapshot:
        """Cold-validate one private payload and return body-safe facts."""

    def reprove_snapshot(
        self,
        resource: ResourceConfig,
        snapshot: PortableResourceSnapshot,
    ) -> None:
        """Reprove the owner binding captured by ``export_snapshot``."""


__all__ = ["ResourcePackagePayloadHandler"]
