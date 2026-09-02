"""Shared real source authority for CapabilityHost contract tests."""

from __future__ import annotations

from pathlib import Path

from platform_source_authority import (
    RootedSourceAuthority,
    compose_rooted_source_authority,
)


_SOURCE_ROOT = Path(__file__).absolute().parents[1]
_SOURCE_AUTHORITY: RootedSourceAuthority | None = None


def current_source_authority() -> RootedSourceAuthority:
    """Return the process-owned product authority for this exact checkout."""

    global _SOURCE_AUTHORITY
    if _SOURCE_AUTHORITY is None:
        _SOURCE_AUTHORITY = compose_rooted_source_authority(_SOURCE_ROOT)
    _SOURCE_AUTHORITY.reprove()
    return _SOURCE_AUTHORITY
