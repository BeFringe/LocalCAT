"""Qt-free Application facade for one LocalCAT editor project document."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from editor_contracts import EditorProject, EditorSegment
from parser_composition import create_parser_application_surface
from parser_contracts import (
    CanonicalDocumentWrite,
    CanonicalSegmentWrite,
    CanonicalSerializeRequest,
    ContractViolation,
    DocumentHeader,
    EffectivePurpose,
    LINE_TEXT_V1,
    LOCALCAT_JSON_V1,
    ParsedSegment,
    RawSpeaker,
    ReadRequest,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
    TargetReference,
    TranslationState,
)


LOGGER = logging.getLogger(__name__)


class ProjectError(RuntimeError):
    """Raised when an editor project cannot be loaded or saved safely."""


def _absolute_lexical_path(file_path: Path) -> Path:
    """Return an absolute selection without resolving symlinks or real paths."""

    expanded = Path(file_path).expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def _format_for_suffix(suffix: str):
    if suffix == ".json":
        return LOCALCAT_JSON_V1
    if suffix == ".txt":
        return LINE_TEXT_V1
    raise ProjectError(f"unsupported project format: {suffix or '<none>'}")


def load_project(file_path: Path) -> EditorProject:
    """Load a supported local project without mutating any current session."""

    path = _absolute_lexical_path(file_path)
    suffix = path.suffix.lower()
    format_id = _format_for_suffix(suffix)
    purpose = EffectivePurpose.PROJECT_DOCUMENT
    try:
        surface = create_parser_application_surface()
        opened = surface.open_input(
            SourceReference(
                safe_root=str(path.parent),
                selected_path=str(path),
                display_hint=path.name,
            ),
            SelectionRequest(purpose=purpose, format_id=format_id),
            ReadRequest(purpose=purpose, format_id=format_id),
        )
        if isinstance(opened, SelectionFailure):
            raise ProjectError(f"unable to read project: {opened.code}")

        header: DocumentHeader | None = None
        staged_segments: list[ParsedSegment] = []
        with opened:
            session = opened.stream()
            try:
                for event in session:
                    if type(event) is DocumentHeader:
                        header = event
                    elif type(event) is ParsedSegment:
                        staged_segments.append(event)
                _ = session.verified_terminal()
            finally:
                session.close()

        if header is None:
            raise ProjectError("unable to read project: verified document header is missing")
        project = EditorProject(
            name=header.name,
            source_locale=header.source_locale or "en-US",
            target_locale=header.target_locale or "zh-CN",
            segments=tuple(
                EditorSegment(
                    id=segment.local_id,
                    source=segment.source,
                    target=segment.target if segment.target is not None else "",
                    speaker=segment.speaker.value,
                    confirmed=(
                        segment.translation_state is TranslationState.CONFIRMED
                    ),
                )
                for segment in staged_segments
            ),
            path=path,
        )
    except ProjectError:
        raise
    except (ContractViolation, OSError, TypeError, ValueError) as exc:
        raise ProjectError(f"unable to read project '{path}': {exc}") from exc
    LOGGER.info("Loaded editor project from %s", path)
    return project


def save_project(project: EditorProject, file_path: Path) -> Path:
    """Atomically save a project in LocalCAT's versioned JSON format."""

    path = _absolute_lexical_path(file_path)
    if path.suffix.lower() != ".json":
        raise ProjectError("editor projects can only be saved as .json")
    try:
        document = CanonicalDocumentWrite(
            name=project.name,
            source_locale=project.source_locale,
            target_locale=project.target_locale,
            segments=tuple(
                CanonicalSegmentWrite(
                    local_id=segment.id,
                    source=segment.source,
                    target=segment.target,
                    speaker=RawSpeaker(segment.speaker),
                    confirmed=segment.confirmed,
                )
                for segment in project.segments
            ),
        )
        surface = create_parser_application_surface()
        prepared = surface.prepare_canonical(
            EffectivePurpose.PROJECT_DOCUMENT,
            CanonicalSerializeRequest(
                format_id=LOCALCAT_JSON_V1,
                document=document,
            ),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = prepared.write(
            TargetReference(
                safe_root=str(path.parent),
                selected_path=str(path),
                display_hint=path.name,
            ),
        )
    except (ContractViolation, OSError, TypeError, ValueError) as exc:
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
