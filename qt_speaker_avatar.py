"""Qt presentation-only decoding for rooted speaker inventory avatars."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QImageReader, QPixmap

from qt_resource_contracts import QtResourceBytes

_AVATAR_SUFFIX = "Half.png"
_MAX_SOURCE_EDGE = 2048


def resource_png_pixmap(
    asset: QtResourceBytes,
    *,
    edge: int | None = None,
    maximum_source_edge: int | None = None,
) -> QPixmap | None:
    """Decode exact PNG bytes without returning to a filesystem locator."""

    if type(asset) is not QtResourceBytes:
        raise TypeError("Qt image decoding requires QtResourceBytes")
    if edge is not None and (type(edge) is not int or edge <= 0):
        return None
    if maximum_source_edge is not None and (
        type(maximum_source_edge) is not int or maximum_source_edge <= 0
    ):
        return None
    buffer = QBuffer()
    buffer.setData(QByteArray(asset.content))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return None
    try:
        reader = QImageReader(buffer)
        if bytes(reader.format()).lower() != b"png":
            return None
        size = reader.size()
        if not size.isValid():
            return None
        if maximum_source_edge is not None and (
            size.width() > maximum_source_edge
            or size.height() > maximum_source_edge
        ):
            return None
        reader.setAutoTransform(True)
        image = reader.read()
    finally:
        buffer.close()
    if image.isNull():
        return None
    pixmap = QPixmap.fromImage(image)
    if edge is None:
        return pixmap
    return pixmap.scaled(
        QSize(edge, edge),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class SpeakerAvatarCatalog:
    """Fail-closed index of fixed application avatar assets."""

    def __init__(self, assets: tuple[QtResourceBytes, ...] = ()) -> None:
        if type(assets) is not tuple or any(
            type(asset) is not QtResourceBytes for asset in assets
        ):
            raise TypeError("speaker avatar catalog requires an exact asset tuple")
        self._assets = assets
        self._index: Mapping[str, QtResourceBytes | None] = MappingProxyType(
            self._build_index(assets)
        )

    @staticmethod
    def _build_index(
        assets: tuple[QtResourceBytes, ...],
    ) -> dict[str, QtResourceBytes | None]:
        indexed: dict[str, QtResourceBytes | None] = {}
        for asset in assets:
            name = asset.filename
            if not name.endswith(_AVATAR_SUFFIX):
                continue
            speaker_name = name[: -len(_AVATAR_SUFFIX)]
            if not speaker_name or speaker_name.startswith("."):
                continue
            key = speaker_name.casefold()
            if key in indexed:
                indexed[key] = None
                continue
            indexed[key] = asset
        return indexed

    def avatar_pixmap(self, raw_speaker: str, edge: int = 48) -> QPixmap | None:
        """Decode one unique allowlisted asset without deriving a path from input."""

        if (
            type(raw_speaker) is not str
            or not raw_speaker
            or type(edge) is not int
            or edge <= 0
        ):
            return None
        asset = self._index.get(raw_speaker.casefold())
        if asset is None:
            return None
        return resource_png_pixmap(
            asset,
            edge=edge,
            maximum_source_edge=_MAX_SOURCE_EDGE,
        )


__all__ = ["SpeakerAvatarCatalog", "resource_png_pixmap"]
