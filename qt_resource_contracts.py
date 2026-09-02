"""Immutable byte resources consumed only by Qt presentation code."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QtResourceBytes:
    """One rooted resource snapshot with no path authority."""

    filename: str
    content: bytes

    def __post_init__(self) -> None:
        if type(self.filename) is not str or not self.filename:
            raise TypeError("Qt resource filename must be a non-empty exact string")
        if "/" in self.filename or "\\" in self.filename:
            raise ValueError("Qt resource filename must not contain path separators")
        if type(self.content) is not bytes:
            raise TypeError("Qt resource content must be exact bytes")


@dataclass(frozen=True, slots=True)
class SourceQtResources:
    """Rooted source assets handed to Qt without filesystem locators."""

    logo: QtResourceBytes
    speaker_avatars: tuple[QtResourceBytes, ...] = ()

    def __post_init__(self) -> None:
        if type(self.logo) is not QtResourceBytes:
            raise TypeError("source Qt resources require one exact logo asset")
        if type(self.speaker_avatars) is not tuple or any(
            type(asset) is not QtResourceBytes for asset in self.speaker_avatars
        ):
            raise TypeError("source Qt avatar assets must be an exact tuple")


__all__ = ["QtResourceBytes", "SourceQtResources"]
