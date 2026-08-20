"""Qt presentation-only lookup for bundled speaker inventory avatars."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImageReader, QPixmap


DEFAULT_SPEAKER_AVATAR_ROOT = Path(__file__).resolve().parent / "speaker_avatars"
_AVATAR_SUFFIX = "Half.png"
_MAX_SOURCE_EDGE = 2048


class SpeakerAvatarCatalog:
    """Fail-closed index of fixed application avatar assets."""

    def __init__(self, root: Path = DEFAULT_SPEAKER_AVATAR_ROOT) -> None:
        self._root = root.resolve()
        self._paths = self._build_index(root)

    @staticmethod
    def _build_index(root: Path) -> dict[str, Path | None]:
        if not root.is_dir() or root.is_symlink():
            return {}
        indexed: dict[str, Path | None] = {}
        try:
            children = tuple(root.iterdir())
        except OSError:
            return {}
        for candidate in children:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            name = candidate.name
            if not name.endswith(_AVATAR_SUFFIX):
                continue
            speaker_name = name[: -len(_AVATAR_SUFFIX)]
            if not speaker_name or speaker_name.startswith("."):
                continue
            key = speaker_name.casefold()
            if key in indexed:
                indexed[key] = None
                continue
            indexed[key] = candidate.resolve()
        return indexed

    def avatar_pixmap(self, raw_speaker: str, edge: int = 48) -> QPixmap | None:
        """Decode one unique allowlisted asset without deriving a path from input."""

        if not isinstance(raw_speaker, str) or not raw_speaker or edge <= 0:
            return None
        candidate = self._paths.get(raw_speaker.casefold())
        if candidate is None:
            return None
        try:
            if candidate.parent != self._root or candidate.is_symlink():
                return None
        except OSError:
            return None
        reader = QImageReader(str(candidate))
        size = reader.size()
        if (
            not size.isValid()
            or size.width() > _MAX_SOURCE_EDGE
            or size.height() > _MAX_SOURCE_EDGE
        ):
            return None
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            return None
        return QPixmap.fromImage(image).scaled(
            QSize(edge, edge),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
