"""Immutable display-only projections for project export previews."""

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
