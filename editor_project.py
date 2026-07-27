"""Qt-free project codecs for LocalCAT editor JSON and line-based text files."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import cast

from editor_contracts import EditorProject, EditorSegment


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 1
SUPPORTED_PROJECT_SUFFIXES = frozenset({".json", ".txt"})


class ProjectError(RuntimeError):
    """Raised when an editor project cannot be loaded or saved safely."""


def _clean_string(value: object, field_name: str, *, required: bool = False) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ProjectError(f"segment field '{field_name}' must be a string")
    if required and not text:
        raise ProjectError(f"segment field '{field_name}' must not be empty")
    return text


def _segment_from_mapping(entry: object, index: int) -> EditorSegment:
    if not isinstance(entry, dict):
        raise ProjectError(f"segment {index + 1} must be an object")
    mapping = cast(dict[str, object], entry)
    segment_id = _clean_string(mapping.get("id"), "id") or f"segment-{index + 1}"
    source = _clean_string(mapping.get("source"), "source", required=True)
    target = _clean_string(mapping.get("target"), "target")
    speaker = _clean_string(mapping.get("speaker"), "speaker")
    confirmed_value = mapping.get("confirmed", False)
    if not isinstance(confirmed_value, bool):
        raise ProjectError(f"segment {index + 1} field 'confirmed' must be a boolean")
    return EditorSegment(
        id=segment_id,
        source=source,
        target=target,
        speaker=speaker,
        confirmed=confirmed_value,
    )


def _load_json_project(path: Path) -> EditorProject:
    try:
        payload = cast(object, json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectError(f"unable to read JSON project '{path}': {exc}") from exc

    if isinstance(payload, list):
        raw_segments = cast(list[object], payload)
        name = path.stem
        source_locale = "en-US"
        target_locale = "zh-CN"
    elif isinstance(payload, dict):
        mapping = cast(dict[str, object], payload)
        raw_segments_value = mapping.get("segments")
        if not isinstance(raw_segments_value, list):
            raise ProjectError("JSON project must contain a 'segments' array")
        raw_segments = cast(list[object], raw_segments_value)
        name = _clean_string(mapping.get("name"), "name") or path.stem
        source_locale = _clean_string(mapping.get("source_locale"), "source_locale") or "en-US"
        target_locale = _clean_string(mapping.get("target_locale"), "target_locale") or "zh-CN"
    else:
        raise ProjectError("JSON project root must be an array or object")

    segments = tuple(_segment_from_mapping(entry, index) for index, entry in enumerate(raw_segments))
    if not segments:
        raise ProjectError("project contains no translatable segments")
    try:
        return EditorProject(
            name=name,
            segments=segments,
            source_locale=source_locale,
            target_locale=target_locale,
            path=path,
        )
    except (TypeError, ValueError) as exc:
        raise ProjectError(f"invalid project contract: {exc}") from exc


def _load_text_project(path: Path) -> EditorProject:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ProjectError(f"unable to read text project '{path}': {exc}") from exc
    segments = tuple(
        EditorSegment(id=f"segment-{index}", source=line.strip())
        for index, line in enumerate((line for line in lines if line.strip()), start=1)
    )
    if not segments:
        raise ProjectError("text project contains no non-empty lines")
    return EditorProject(name=path.stem, segments=segments, path=path)


def load_project(file_path: Path) -> EditorProject:
    """Load a supported local project without mutating any current session."""

    path = file_path.expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ProjectError(f"project file does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_PROJECT_SUFFIXES:
        raise ProjectError(f"unsupported project format: {suffix or '<none>'}")
    LOGGER.info("Loading editor project from %s", path)
    return _load_json_project(path) if suffix == ".json" else _load_text_project(path)


def _project_payload(project: EditorProject) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "name": project.name,
        "source_locale": project.source_locale,
        "target_locale": project.target_locale,
        "segments": [
            {
                "id": segment.id,
                "source": segment.source,
                "target": segment.target,
                "speaker": segment.speaker,
                "confirmed": segment.confirmed,
            }
            for segment in project.segments
        ],
    }


def save_project(project: EditorProject, file_path: Path) -> Path:
    """Atomically save a project in LocalCAT's versioned JSON format."""

    path = file_path.expanduser().resolve()
    if path.suffix.lower() != ".json":
        raise ProjectError("editor projects can only be saved as .json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(_project_payload(project), ensure_ascii=False, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            _ = handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except (OSError, TypeError, ValueError) as exc:
        if "temp_path" in locals():
            Path(temp_path).unlink(missing_ok=True)
        raise ProjectError(f"unable to save project '{path}': {exc}") from exc
    LOGGER.info("Saved editor project to %s", path)
    return path


def sample_project() -> EditorProject:
    """Return a small original sample that demonstrates the complete editor flow."""

    return EditorProject(
        name="LocalCAT Welcome",
        source_locale="en-US",
        target_locale="zh-CN",
        segments=(
            EditorSegment(
                id="welcome-1",
                source="Welcome to your local translation workspace.",
                target="欢迎来到你的本地翻译工作区。",
                speaker="LocalCAT",
            ),
            EditorSegment(
                id="welcome-2",
                source="Translation memories keep repeated work consistent.",
                speaker="LocalCAT",
            ),
            EditorSegment(
                id="welcome-3",
                source="Termbases help every project use the right words.",
                speaker="LocalCAT",
            ),
        ),
    )


if __name__ == "__main__":
    demo = sample_project()
    assert len(demo.segments) == 3
    assert demo.path is None
    print("Editor project self-test passed.")
