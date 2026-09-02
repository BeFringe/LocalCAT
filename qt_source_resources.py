"""Rooted current-source resolver for the narrow Qt resource surface."""

from __future__ import annotations

from pathlib import Path

from platform_fs_contracts import PlatformFileError
from platform_source_authority import RootedSourceAuthority
from qt_resource_contracts import QtResourceBytes, SourceQtResources


_SPEAKER_AVATAR_DIRECTORY = "speaker_avatars"
_SPEAKER_AVATAR_SUFFIX = "Half.png"


class _SourceQtResourceIdentityStale(RuntimeError):
    """A rooted resource changed between binding and catalog handoff."""


def _validate_filename(filename: str) -> str:
    if type(filename) is not str or not filename:
        raise TypeError("source Qt filename must be a non-empty exact string")
    if "/" in filename or "\\" in filename:
        raise ValueError("source Qt filename must not contain path separators")
    return filename


def _bind_asset(
    authority: RootedSourceAuthority,
    path: Path,
) -> QtResourceBytes:
    source = authority.bind_path(path)
    if not source.is_current():
        raise _SourceQtResourceIdentityStale(
            "source Qt resource identity became stale"
        )
    return QtResourceBytes(filename=path.name, content=source.content)


def _resolve_optional_speaker_avatars(
    authority: RootedSourceAuthority,
) -> tuple[QtResourceBytes, ...]:
    avatar_root = authority.root_path / _SPEAKER_AVATAR_DIRECTORY
    try:
        filenames = tuple(
            sorted(
                candidate.name
                for candidate in avatar_root.iterdir()
                if candidate.name.endswith(_SPEAKER_AVATAR_SUFFIX)
            )
        )
    except OSError:
        return ()

    assets: list[QtResourceBytes] = []
    for filename in filenames:
        try:
            assets.append(_bind_asset(authority, avatar_root / filename))
        except (
            OSError,
            PlatformFileError,
            _SourceQtResourceIdentityStale,
        ):
            # Optional presentation assets fail closed as one empty catalog.  A
            # rejected candidate must never make a colliding peer look unique.
            return ()
    return tuple(assets)


def resolve_source_qt_resources(
    authority: RootedSourceAuthority,
    *,
    logo_filename: str,
) -> SourceQtResources:
    """Resolve mandatory logo bytes and an optional rooted avatar catalog."""

    if type(authority) is not RootedSourceAuthority:
        raise TypeError("source Qt resources require RootedSourceAuthority")
    checked_logo_filename = _validate_filename(logo_filename)
    authority.reprove()
    logo = _bind_asset(authority, authority.root_path / checked_logo_filename)
    if not logo.content:
        raise ValueError("source Qt logo must not be empty")
    avatars = _resolve_optional_speaker_avatars(authority)
    authority.reprove()
    return SourceQtResources(logo=logo, speaker_avatars=avatars)


__all__ = ["resolve_source_qt_resources"]
