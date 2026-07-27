"""Qt-free editing session and language-resource coordination."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

from editor_contracts import (
    ConfirmResult,
    EditorProject,
    EditorSegment,
    ImportReport,
    ImportRequest,
    ResourceConfig,
    ResourceKind,
    SuggestionBundle,
    TMSuggestion,
    TermSuggestion,
    WriteReport,
)
from editor_project import load_project, sample_project
from glossary_engine import GlossaryEngine
from resource_importer import import_termbase, import_tmx, upsert_term
from resource_repository import ResourceError, ResourceRepository
from tm_engine import SourceUnit, TMEngine


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
        self.reload_resources()

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

    def confirm_current(self) -> ConfirmResult:
        """Write the current translation to every writable TM before confirmation."""

        current = self.current_segment
        if not current.target.strip():
            raise EditorControllerError("target text must not be empty before confirmation")
        unit = SourceUnit(
            id=current.id,
            text=current.source,
            speaker=current.speaker or None,
            file_source=self.project.name,
        )
        written: list[str] = []
        errors: list[str] = []
        for resource in self.repository.list_resources():
            if (
                resource.kind is not ResourceKind.TRANSLATION_MEMORY
                or not resource.active
                or not resource.update
            ):
                continue
            engine = self._tm_engines.get(resource.id)
            if engine is None:
                errors.append(f"{resource.name}: translation memory is not loaded")
            elif engine.save_record(unit, current.target):
                written.append(resource.id)
            else:
                errors.append(f"{resource.name}: unable to write translation memory")

        report = WriteReport(written_resource_ids=tuple(written), errors=tuple(errors))
        if errors:
            return ConfirmResult(
                project=self.project,
                current_index=self._current_index,
                write_report=report,
            )

        segments = list(self.project.segments)
        segments[self._current_index] = replace(current, confirmed=True)
        self._project = replace(self.project, segments=tuple(segments))
        self._dirty = True
        current_index = self._current_index
        next_index = next(
            (
                index
                for index in range(current_index + 1, len(segments))
                if not segments[index].confirmed
            ),
            current_index,
        )
        self._current_index = next_index
        return ConfirmResult(
            project=self.project,
            current_index=next_index,
            write_report=report,
        )

    def apply_tm_suggestion(self, suggestion: TMSuggestion) -> EditorProject:
        """Apply one typed TM suggestion without confirming the segment."""

        if not isinstance(suggestion, TMSuggestion):
            raise EditorControllerError("a TM suggestion contract is required")
        if suggestion.source != self.current_segment.source:
            raise EditorControllerError("TM suggestion does not belong to the current segment")
        return self.update_target(suggestion.target)

    def insert_term_suggestion(
        self,
        suggestion: TermSuggestion,
        position: int | None = None,
    ) -> EditorProject:
        """Insert one term target at an editor cursor position without confirmation."""

        if not isinstance(suggestion, TermSuggestion):
            raise EditorControllerError("a term suggestion contract is required")
        target = self.current_segment.target
        insertion_point = len(target) if position is None else position
        if insertion_point < 0 or insertion_point > len(target):
            raise EditorControllerError("term insertion position is outside the target text")
        updated = target[:insertion_point] + suggestion.target_term + target[insertion_point:]
        return self.update_target(updated)

    def add_term(self, source: str, target: str) -> ResourceConfig:
        """Persist a term in the first active Update termbase and reload it."""

        if not source.strip() or not target.strip():
            raise EditorControllerError("source and target terms must not be empty")
        resource = next(
            (
                configured
                for configured in self.repository.list_resources()
                if configured.kind is ResourceKind.TERMBASE
                and configured.active
                and configured.update
            ),
            None,
        )
        if resource is None:
            raise EditorControllerError("no active writable termbase is available")
        report = upsert_term(resource.path, source, target)
        if not report.succeeded:
            raise EditorControllerError("; ".join(report.errors))
        self._glossary_engines[resource.id] = self._load_glossary_engine(resource.path)
        return resource

    def list_resources(self) -> tuple[ResourceConfig, ...]:
        """Return the current persistent resource configuration."""

        return self.repository.list_resources()

    def create_resource(self, name: str, kind: ResourceKind) -> ResourceConfig:
        """Create a managed resource and make it available immediately."""

        try:
            resource = self.repository.create_resource(name, kind)
            self.reload_resources()
            return resource
        except ResourceError as exc:
            raise EditorControllerError(str(exc)) from exc

    def update_resource(self, resource: ResourceConfig) -> ResourceConfig:
        """Persist resource state through the repository and rebuild engine sets."""

        try:
            updated = self.repository.update_resource(resource)
            self.reload_resources()
            return updated
        except ResourceError as exc:
            raise EditorControllerError(str(exc)) from exc

    def import_resource(self, request: ImportRequest) -> ImportReport:
        """Import into one configured resource and hot reload on any written result."""

        try:
            resource = self.repository.get(request.resource_id)
        except ResourceError as exc:
            raise EditorControllerError(str(exc)) from exc
        if resource.kind is ResourceKind.TRANSLATION_MEMORY:
            report = import_tmx(
                request.input_path,
                resource.path,
                request.source_locale,
                request.target_locale,
            )
        else:
            report = import_termbase(request.input_path, resource.path)
        if report.imported:
            try:
                self.reload_resources()
            except EditorControllerError as exc:
                return ImportReport(
                    imported=report.imported,
                    skipped=report.skipped,
                    overwritten=report.overwritten,
                    errors=(*report.errors, f"resource reload failed: {exc}"),
                )
        return report

    def reload_resources(self) -> None:
        """Build a complete active engine set before replacing the last known-good set."""

        tm_engines: dict[str, TMEngine] = {}
        glossary_engines: dict[str, GlossaryEngine] = {}
        try:
            for resource in self.repository.list_resources():
                if not resource.active:
                    continue
                if resource.kind is ResourceKind.TRANSLATION_MEMORY:
                    tm_engines[resource.id] = self._load_tm_engine(resource.path)
                else:
                    glossary_engines[resource.id] = self._load_glossary_engine(resource.path)
        except (OSError, UnicodeError, ValueError, csv.Error, json.JSONDecodeError) as exc:
            raise EditorControllerError(f"unable to reload language resources: {exc}") from exc
        self._tm_engines = tm_engines
        self._glossary_engines = glossary_engines

    @staticmethod
    def _load_tm_engine(path: Path) -> TMEngine:
        if not path.exists() or not path.is_file():
            raise ValueError(f"translation memory does not exist: {path}")
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"translation memory line {line_number} must be an object")
            source = record.get("source")
            target = record.get("target")
            if not isinstance(source, str) or not source.strip():
                raise ValueError(f"translation memory line {line_number} has no source")
            if not isinstance(target, str) or not target.strip():
                raise ValueError(f"translation memory line {line_number} has no target")
        return TMEngine(str(path))

    @staticmethod
    def _load_glossary_engine(path: Path) -> GlossaryEngine:
        if not path.exists() or not path.is_file():
            raise ValueError(f"termbase does not exist: {path}")
        terms: dict[str, str] = {}
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for line_number, row in enumerate(csv.reader(handle), start=1):
                if not row or not any(cell.strip() for cell in row):
                    continue
                if len(row) < 2:
                    raise ValueError(f"termbase row {line_number} has fewer than two columns")
                source = row[0].strip()
                target = row[1].strip()
                if source.casefold() == "source" and target.casefold() == "target":
                    continue
                if not source or not target:
                    raise ValueError(f"termbase row {line_number} has an empty term")
                terms[source] = target
        engine = GlossaryEngine()
        for source, target in terms.items():
            engine.add_term(source, target, path.name)
        return engine


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        controller = EditorController(ResourceRepository(Path(temp_dir)))
        controller.load_sample()
        assert controller.current_segment.source
    print("Editor controller self-test passed.")
