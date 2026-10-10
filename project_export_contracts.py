"""Immutable display-only projections for project export previews and results."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProjectExportDiagnostic:
    code: str
    severity: str
    safe_summary: str
    document_id: str
    source_ref: str
    local_segment_id: str | None = None
    line_number: int | None = None
    byte_offset: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectExportView:
    preview_id: str
    session_id: str
    workspace_revision: int
    request_generation: int
    document_id: str
    source_ref: str
    target_path: str
    segment_count: int
    modified_count: int | None
    empty_count: int
    unconfirmed_count: int
    status: str
    diagnostics: tuple[ProjectExportDiagnostic, ...] = ()
    output_sha256: str | None = None
    output_byte_count: int | None = None
    target_action: str = 'new'


@dataclass(frozen=True, slots=True)
class ProjectExportResult:
    """Export facts only; this never acknowledges a ProjectPackage save.

    Output facts are present only for a publication proved by the Parser owner.
    Uncertain output may be visible on disk and requires inspection.
    """

    preview_id: str
    session_id: str
    workspace_revision: int
    request_generation: int
    document_id: str
    source_ref: str
    target_path: str
    outcome: str
    diagnostics: tuple[ProjectExportDiagnostic, ...] = ()
    output_sha256: str | None = None
    output_byte_count: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectExportBatchView:
    preview_id: str
    session_id: str
    workspace_revision: int
    request_generation: int
    target_path: str
    files: tuple[ProjectExportView, ...]
    missing_directories: tuple[str, ...]
    status: str
    diagnostics: tuple[ProjectExportDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectExportBatchResult:
    preview_id: str
    session_id: str
    workspace_revision: int
    request_generation: int
    target_path: str
    files: tuple[ProjectExportResult, ...]
    outcome: str
    diagnostics: tuple[ProjectExportDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectExportDirectoryFailure:
    relative_path: str
    outcome: str
    code: str


@dataclass(frozen=True, slots=True)
class ProjectExportDirectoryResult:
    preview_id: str
    session_id: str
    workspace_revision: int
    request_generation: int
    target_path: str
    outcome: str
    created_directories: tuple[str, ...] = ()
    directory_failures: tuple[ProjectExportDirectoryFailure, ...] = ()
    diagnostics: tuple[ProjectExportDiagnostic, ...] = ()
