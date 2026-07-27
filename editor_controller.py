"""Qt-free editing session and language-resource coordination."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    SuggestionBundle,
    TMSuggestion,
    TermSuggestion,
)
from editor_project import load_project, sample_project
from glossary_engine import GlossaryEngine, GlossaryLoader
from resource_repository import ResourceRepository
from tm_engine import TMEngine


class EditorControllerError(RuntimeError):
    """Raised when an editor operation cannot be completed."""


class EditorController:
    """Own one immutable editor session while engines remain UI-state free."""

    def __init__(self, repository: ResourceRepository) -> None:
        self.repository = repository
        self._project: EditorProject | None = None
        self._current_index = 0
        self._dirty = False
        self._tm_engines: dict[str, TMEngine] = {}
        self._glossary_engines: dict[str, GlossaryEngine] = {}
        self._rebuild_engines()

    @property
    def project(self) -> EditorProject:
        if self._project is None:
            raise EditorControllerError("no project is open")
        return self._project

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def current_segment(self) -> EditorSegment:
        return self.project.segments[self._current_index]

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def confirmed_count(self) -> int:
        return sum(segment.confirmed for segment in self.project.segments)

    @property
    def completion_ratio(self) -> float:
        segments = self.project.segments
        return self.confirmed_count / len(segments) if segments else 0.0

    def open_project(self, path: Path) -> EditorProject:
        """Load a local JSON/TXT project and reset navigation only after success."""

        project = load_project(path)
        return self.set_project(project)

    def load_sample(self) -> EditorProject:
        """Open the bundled original sample project."""

        return self.set_project(sample_project())

    def set_project(self, project: EditorProject) -> EditorProject:
        """Install an already validated project contract as the current session."""

        if not project.segments:
            raise EditorControllerError("project contains no segments")
        self._project = project
        self._current_index = 0
        self._dirty = False
        return project

    def update_target(self, target: str) -> EditorProject:
        """Persist the current edit in the immutable session and reopen confirmation."""

        if not isinstance(target, str):
            raise EditorControllerError("target text must be a string")
        current = self.current_segment
        if target == current.target:
            return self.project
        segments = list(self.project.segments)
        segments[self._current_index] = replace(current, target=target, confirmed=False)
        self._project = replace(self.project, segments=tuple(segments))
        self._dirty = True
        return self.project

    def move(self, direction: int, unconfirmed_only: bool = False) -> EditorProject:
        """Move one segment or find the next unconfirmed segment without losing edits."""

        if direction == 0:
            raise EditorControllerError("navigation direction must not be zero")
        segments = self.project.segments
        step = 1 if direction > 0 else -1
        if unconfirmed_only:
            candidates = range(
                self._current_index + step,
                len(segments) if step > 0 else -1,
                step,
            )
            destination = next(
                (index for index in candidates if not segments[index].confirmed),
                self._current_index,
            )
        else:
            destination = min(max(self._current_index + step, 0), len(segments) - 1)
        self._current_index = destination
        return self.project

    def suggestions(self) -> SuggestionBundle:
        """Query every currently active Lookup resource for the current source."""

        source = self.current_segment.source
        tm_matches: list[TMSuggestion] = []
        terms: list[TermSuggestion] = []
        for resource in self.repository.list_resources():
            if not resource.active or not resource.lookup:
                continue
            if resource.kind is ResourceKind.TRANSLATION_MEMORY:
                engine = self._tm_engines.get(resource.id)
                match = engine.query_exact(source) if engine is not None else None
                if match is not None:
                    tm_matches.append(
                        TMSuggestion(
                            source=match.source,
                            target=match.target,
                            resource_id=resource.id,
                            resource_name=resource.name,
                            similarity=match.similarity,
                            match_type=match.match_type,
                        )
                    )
            else:
                engine = self._glossary_engines.get(resource.id)
                if engine is None:
                    continue
                for hit in engine.extract_terms(source):
                    terms.append(
                        TermSuggestion(
                            source_term=hit.source_term,
                            target_term=hit.target_term,
                            start_index=hit.start_index,
                            end_index=hit.end_index,
                            resource_id=resource.id,
                            resource_name=resource.name,
                            definition=hit.definition,
                        )
                    )
        return SuggestionBundle(tm_matches=tuple(tm_matches), terms=tuple(terms))

    def _rebuild_engines(self) -> None:
        tm_engines: dict[str, TMEngine] = {}
        glossary_engines: dict[str, GlossaryEngine] = {}
        for resource in self.repository.list_resources():
            if resource.kind is ResourceKind.TRANSLATION_MEMORY:
                tm_engines[resource.id] = TMEngine(str(resource.path))
            else:
                engine = GlossaryEngine()
                if resource.path.exists():
                    GlossaryLoader(engine).load_file(str(resource.path))
                glossary_engines[resource.id] = engine
        self._tm_engines = tm_engines
        self._glossary_engines = glossary_engines


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        controller = EditorController(ResourceRepository(Path(temp_dir)))
        controller.load_sample()
        assert controller.current_segment.source
    print("Editor controller self-test passed.")
