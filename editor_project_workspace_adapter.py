"""Compatibility boundary between legacy single JSON and ProjectWorkspace."""

from __future__ import annotations

import os
from pathlib import Path

from editor_contracts import EditorProject, EditorSegment
from parser_composition import create_parser_application_surface
from parser_contracts import (
    CodecIdentity,
    ContractViolation,
    DocumentHeader,
    EffectivePurpose,
    LOCALCAT_JSON_V1,
    ParsedSegment,
    ReadRequest,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
    TranslationState,
)
from project_workspace_contracts import (
    EditingOverlayEntry,
    ProjectDocument,
    ProjectOrigin,
    ProjectOriginKind,
    ProjectPersistenceKind,
    ProjectSourceSegment,
    ProjectWorkspace,
    ProjectWorkspaceError,
    WriterCapabilitySnapshot,
)
from project_workspace_identity import (
    derive_device_local_origin_key,
    derive_legacy_single_json_document_id,
    derive_legacy_single_json_project_id,
    editing_state_digest_v1,
    normalize_portable_ref_v1,
    source_fingerprint_v1,
)


_LOCALCAT_JSON_CODEC_IDENTITY = CodecIdentity("localcat", "localcat-json", "1")


def _fail() -> None:
    raise ProjectWorkspaceError("PROJECT.WORKSPACE.CONTRACT_INVALID")


def _absolute_lexical_path(file_path: Path) -> Path:
    if not isinstance(file_path, Path):
        _fail()
    try:
        expanded = file_path.expanduser()
    except RuntimeError:
        _fail()
    return Path(os.path.abspath(os.fspath(expanded)))


def _workspace_from_verified_legacy_project(
    project: EditorProject,
    *,
    device_local_origin_key: str,
    source_ref: str,
    codec_identity: CodecIdentity,
    writer_capability_snapshot: WriterCapabilitySnapshot,
    source_snapshot_digest: str,
) -> ProjectWorkspace:
    """Map already verified legacy facts without granting any live capability."""

    if type(project) is not EditorProject:
        _fail()
    if type(codec_identity) is not CodecIdentity:
        _fail()
    if type(writer_capability_snapshot) is not WriterCapabilitySnapshot:
        _fail()
    normalized_ref = normalize_portable_ref_v1(source_ref)
    project_id = derive_legacy_single_json_project_id(device_local_origin_key)
    document_id = derive_legacy_single_json_document_id(normalized_ref)
    source_segments: list[ProjectSourceSegment] = []
    overlay: list[EditingOverlayEntry] = []
    for segment in project.segments:
        fingerprint = source_fingerprint_v1(segment.source, segment.speaker)
        source_segments.append(
            ProjectSourceSegment(
                local_segment_id=segment.id,
                source=segment.source,
                raw_speaker=segment.speaker,
                source_fingerprint=fingerprint,
            )
        )
        overlay.append(
            EditingOverlayEntry(
                document_id=document_id,
                local_segment_id=segment.id,
                source_fingerprint=fingerprint,
                target=segment.target,
                confirmed=segment.confirmed,
                saved_state_digest=editing_state_digest_v1(
                    document_id,
                    segment.id,
                    fingerprint,
                    segment.target,
                    segment.confirmed,
                ),
            )
        )
    document = ProjectDocument(
        document_id=document_id,
        source_ref=normalized_ref,
        display_name=project.name,
        order=0,
        format_id=LOCALCAT_JSON_V1.value,
        codec_identity=codec_identity,
        writer_capability_snapshot=writer_capability_snapshot,
        source_snapshot_digest=source_snapshot_digest,
        source_segments=tuple(source_segments),
        editing_overlay=tuple(overlay),
        codec_private_member=None,
    )
    return ProjectWorkspace(
        schema_version=1,
        project_id=project_id,
        name=project.name,
        source_locale=project.source_locale,
        target_locale=project.target_locale,
        origin=ProjectOrigin(
            kind=ProjectOriginKind.SINGLE_FILE,
            profile_version="localcat-json-v1",
            portable_root_ref=normalized_ref,
        ),
        persistence_kind=ProjectPersistenceKind.LEGACY_SINGLE_JSON,
        documents=(document,),
    )


def load_legacy_single_json_workspace(file_path: Path) -> ProjectWorkspace:
    """Parse one JSON source once and publish a verified immutable workspace."""

    try:
        path = _absolute_lexical_path(file_path)
        if path.suffix.lower() != ".json":
            _fail()
        source_ref = normalize_portable_ref_v1(path.name)
        purpose = EffectivePurpose.PROJECT_DOCUMENT
        opened = create_parser_application_surface().open_input(
            SourceReference(
                safe_root=str(path.parent),
                selected_path=str(path),
                display_hint=path.name,
            ),
            SelectionRequest(purpose=purpose, format_id=LOCALCAT_JSON_V1),
            ReadRequest(purpose=purpose, format_id=LOCALCAT_JSON_V1),
        )
        if type(opened) is SelectionFailure:
            _fail()
        assert not isinstance(opened, SelectionFailure)
        header: DocumentHeader | None = None
        parsed: list[ParsedSegment] = []
        with opened:
            descriptor = opened.descriptor
            source_identity = opened.source_identity
            if (
                descriptor.purpose is not purpose
                or descriptor.format_id != LOCALCAT_JSON_V1
                or descriptor.identity != _LOCALCAT_JSON_CODEC_IDENTITY
                or descriptor.limit_profile.profile_id != "localcat-json-v1"
                or descriptor.limit_profile.profile_version != 1
                or descriptor.capabilities.format_profile != "localcat-json-v1"
            ):
                _fail()
            session = opened.stream()
            try:
                for event in session:
                    if type(event) is DocumentHeader:
                        if header is not None:
                            _fail()
                        header = event
                    elif type(event) is ParsedSegment:
                        parsed.append(event)
                terminal = session.verified_terminal()
            finally:
                session.close()
        if header is None:
            _fail()
        if terminal.source != source_identity:
            _fail()
        if terminal.codec_identity != descriptor.identity:
            _fail()
        if terminal.limit_profile != descriptor.limit_profile:
            _fail()
        if terminal.record_count != len(parsed):
            _fail()
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
                for segment in parsed
            ),
            path=path,
        )
        capabilities = descriptor.capabilities
        return _workspace_from_verified_legacy_project(
            project,
            device_local_origin_key=derive_device_local_origin_key(str(path)),
            source_ref=source_ref,
            codec_identity=descriptor.identity,
            writer_capability_snapshot=WriterCapabilitySnapshot(
                canonical_write=capabilities.canonical_write,
                source_round_trip_write=capabilities.source_round_trip_write,
                format_profile=capabilities.format_profile,
            ),
            source_snapshot_digest=source_identity.content_sha256,
        )
    except ProjectWorkspaceError:
        raise
    except (ContractViolation, OSError) as error:
        raise ProjectWorkspaceError(
            "PROJECT.WORKSPACE.CONTRACT_INVALID"
        ) from error


def workspace_to_legacy_editor_project(
    workspace: ProjectWorkspace,
    *,
    path: Path | None = None,
) -> EditorProject:
    """Project one exact legacy single-JSON workspace without flattening."""

    if type(workspace) is not ProjectWorkspace:
        _fail()
    if workspace.origin.kind is not ProjectOriginKind.SINGLE_FILE:
        _fail()
    if workspace.origin.profile_version != "localcat-json-v1":
        _fail()
    if workspace.persistence_kind is not ProjectPersistenceKind.LEGACY_SINGLE_JSON:
        _fail()
    if len(workspace.documents) != 1:
        _fail()
    document = workspace.documents[0]
    if document.format_id != LOCALCAT_JSON_V1.value:
        _fail()
    if document.codec_identity != _LOCALCAT_JSON_CODEC_IDENTITY:
        _fail()
    if document.display_name != workspace.name:
        _fail()
    if document.codec_private_member is not None:
        _fail()
    if not document.writer_capability_snapshot.canonical_write:
        _fail()
    if document.writer_capability_snapshot.source_round_trip_write:
        _fail()
    legacy_path: Path | None = None
    if path is not None:
        legacy_path = _absolute_lexical_path(path)
        if legacy_path.suffix.lower() != ".json":
            _fail()
    return EditorProject(
        name=workspace.name,
        source_locale=workspace.source_locale,
        target_locale=workspace.target_locale,
        segments=tuple(
            EditorSegment(
                id=segment.identity.local_segment_id,
                source=segment.source,
                target=segment.target,
                speaker=segment.raw_speaker,
                confirmed=segment.confirmed,
            )
            for segment in document.segments
        ),
        path=legacy_path,
    )


__all__ = (
    "load_legacy_single_json_workspace",
    "workspace_to_legacy_editor_project",
)
