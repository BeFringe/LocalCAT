"""Immutable contracts for LocalCAT project workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import unicodedata

from parser_contracts import CodecIdentity, FormatId
from project_workspace_identity import (
    ProjectWorkspaceError,
    normalize_portable_ref_v1,
    validate_document_id,
    validate_local_segment_id,
    validate_portable_ref_collection,
    validate_project_id,
    validate_sha256,
)


PROJECT_LIMIT_PROFILE_ID = "localcat-project-limits-v1"
MAX_PROJECT_DOCUMENTS = 1_024
MAX_SEGMENTS_PER_DOCUMENT = 100_000
MAX_SEGMENTS_PER_PROJECT = 100_000
MAX_PROJECT_NAME_SCALARS = 512
MAX_CODEC_PRIVATE_MEMBER_BYTES = 256 * 1024 * 1024

_PROFILE_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


def _fail(code: str = "PROJECT.WORKSPACE.CONTRACT_INVALID") -> None:
    raise ProjectWorkspaceError(code)


def _exact_text(
    value: object,
    *,
    max_scalars: int | None = None,
    allow_empty: bool = False,
    allow_controls: bool = False,
) -> str:
    if type(value) is not str:
        _fail()
    assert type(value) is str
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail()
    if any(unicodedata.category(character) == "Cs" for character in value):
        _fail()
    if not allow_controls and any(
        unicodedata.category(character) == "Cc" for character in value
    ):
        _fail()
    if not allow_empty and not value.strip():
        _fail()
    if max_scalars is not None and len(value) > max_scalars:
        _fail("PROJECT.WORKSPACE.LIMIT_EXCEEDED")
    return value


def _exact_bool(value: object) -> bool:
    if type(value) is not bool:
        _fail()
    return value


def _exact_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        _fail()
    return value


def _exact_tuple(value: object) -> tuple[object, ...]:
    if type(value) is not tuple:
        _fail()
    return value


class ProjectOriginKind(Enum):
    SINGLE_FILE = "single_file"
    DIRECTORY = "directory"
    WORKBOOK = "workbook"


class ProjectPersistenceKind(Enum):
    LEGACY_SINGLE_JSON = "legacy_single_json"
    PROJECT_PACKAGE = "project_package"


@dataclass(frozen=True, slots=True)
class SegmentIdentity:
    document_id: str
    local_segment_id: str

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        validate_local_segment_id(self.local_segment_id)


@dataclass(frozen=True, slots=True)
class ProjectOrigin:
    kind: ProjectOriginKind
    profile_version: str
    portable_root_ref: str

    def __post_init__(self) -> None:
        if type(self.kind) is not ProjectOriginKind:
            _fail()
        profile = _exact_text(self.profile_version)
        if _PROFILE_TOKEN.fullmatch(profile) is None:
            _fail()
        normalized = normalize_portable_ref_v1(self.portable_root_ref)
        if normalized != self.portable_root_ref:
            _fail("PROJECT.WORKSPACE.PATH_INVALID")


@dataclass(frozen=True, slots=True)
class WriterCapabilitySnapshot:
    canonical_write: bool
    source_round_trip_write: bool
    format_profile: str

    def __post_init__(self) -> None:
        _exact_bool(self.canonical_write)
        _exact_bool(self.source_round_trip_write)
        profile = _exact_text(self.format_profile)
        if _PROFILE_TOKEN.fullmatch(profile) is None:
            _fail()


@dataclass(frozen=True, slots=True)
class CodecPrivateMemberRef:
    member_path: str
    sha256: str
    byte_count: int
    codec_identity: CodecIdentity
    profile_version: str

    def __post_init__(self) -> None:
        normalized = normalize_portable_ref_v1(self.member_path)
        if normalized != self.member_path or not normalized.startswith("codec-private/"):
            _fail("PROJECT.WORKSPACE.PATH_INVALID")
        validate_sha256(self.sha256)
        count = _exact_nonnegative_int(self.byte_count)
        if count > MAX_CODEC_PRIVATE_MEMBER_BYTES:
            _fail("PROJECT.WORKSPACE.LIMIT_EXCEEDED")
        if type(self.codec_identity) is not CodecIdentity:
            _fail()
        profile = _exact_text(self.profile_version)
        if _PROFILE_TOKEN.fullmatch(profile) is None:
            _fail()


@dataclass(frozen=True, slots=True)
class ProjectSourceSegment:
    local_segment_id: str
    source: str
    raw_speaker: str
    source_fingerprint: str

    def __post_init__(self) -> None:
        validate_local_segment_id(self.local_segment_id)
        _exact_text(self.source, allow_controls=True)
        _exact_text(self.raw_speaker, allow_empty=True, allow_controls=True)
        validate_sha256(self.source_fingerprint)


@dataclass(frozen=True, slots=True)
class EditingOverlayEntry:
    document_id: str
    local_segment_id: str
    source_fingerprint: str
    target: str
    confirmed: bool
    saved_state_digest: str

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        validate_local_segment_id(self.local_segment_id)
        validate_sha256(self.source_fingerprint)
        _exact_text(self.target, allow_empty=True, allow_controls=True)
        _exact_bool(self.confirmed)
        validate_sha256(self.saved_state_digest)


@dataclass(frozen=True, slots=True)
class ProjectSegment:
    identity: SegmentIdentity
    source: str
    target: str
    raw_speaker: str
    confirmed: bool
    source_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.identity) is not SegmentIdentity:
            _fail()
        _exact_text(self.source, allow_controls=True)
        _exact_text(self.target, allow_empty=True, allow_controls=True)
        _exact_text(self.raw_speaker, allow_empty=True, allow_controls=True)
        _exact_bool(self.confirmed)
        validate_sha256(self.source_fingerprint)


@dataclass(frozen=True, slots=True)
class ProjectDocument:
    document_id: str
    source_ref: str
    display_name: str
    order: int
    format_id: str
    codec_identity: CodecIdentity
    writer_capability_snapshot: WriterCapabilitySnapshot
    source_snapshot_digest: str
    source_segments: tuple[ProjectSourceSegment, ...]
    editing_overlay: tuple[EditingOverlayEntry, ...]
    codec_private_member: CodecPrivateMemberRef | None

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        normalized_ref = normalize_portable_ref_v1(self.source_ref)
        if normalized_ref != self.source_ref:
            _fail("PROJECT.WORKSPACE.PATH_INVALID")
        _exact_text(
            self.display_name,
            max_scalars=MAX_PROJECT_NAME_SCALARS,
            allow_controls=True,
        )
        _exact_nonnegative_int(self.order)
        try:
            FormatId(self.format_id)
        except (TypeError, ValueError):
            _fail()
        if type(self.codec_identity) is not CodecIdentity:
            _fail()
        if type(self.writer_capability_snapshot) is not WriterCapabilitySnapshot:
            _fail()
        validate_sha256(self.source_snapshot_digest)
        _exact_tuple(self.source_segments)
        _exact_tuple(self.editing_overlay)
        if not self.source_segments:
            _fail()
        if len(self.source_segments) > MAX_SEGMENTS_PER_DOCUMENT:
            _fail("PROJECT.WORKSPACE.LIMIT_EXCEEDED")
        if len(self.editing_overlay) != len(self.source_segments):
            _fail()
        if any(type(item) is not ProjectSourceSegment for item in self.source_segments):
            _fail()
        if any(type(item) is not EditingOverlayEntry for item in self.editing_overlay):
            _fail()
        local_ids = tuple(item.local_segment_id for item in self.source_segments)
        if len(local_ids) != len(set(local_ids)):
            _fail("PROJECT.WORKSPACE.IDENTITY_DUPLICATE")
        overlay_ids = tuple(item.local_segment_id for item in self.editing_overlay)
        if len(overlay_ids) != len(set(overlay_ids)):
            _fail("PROJECT.WORKSPACE.IDENTITY_DUPLICATE")
        if overlay_ids != local_ids:
            _fail()
        for source, overlay in zip(
            self.source_segments,
            self.editing_overlay,
            strict=True,
        ):
            if overlay.document_id != self.document_id:
                _fail()
            if overlay.source_fingerprint != source.source_fingerprint:
                _fail()
        if self.codec_private_member is not None:
            if type(self.codec_private_member) is not CodecPrivateMemberRef:
                _fail()
            if self.codec_private_member.codec_identity != self.codec_identity:
                _fail()

    @property
    def segment_identities(self) -> tuple[SegmentIdentity, ...]:
        return tuple(
            SegmentIdentity(self.document_id, segment.local_segment_id)
            for segment in self.source_segments
        )

    @property
    def segments(self) -> tuple[ProjectSegment, ...]:
        return tuple(
            ProjectSegment(
                identity=SegmentIdentity(self.document_id, source.local_segment_id),
                source=source.source,
                target=overlay.target,
                raw_speaker=source.raw_speaker,
                confirmed=overlay.confirmed,
                source_fingerprint=source.source_fingerprint,
            )
            for source, overlay in zip(
                self.source_segments,
                self.editing_overlay,
                strict=True,
            )
        )


@dataclass(frozen=True, slots=True)
class ProjectWorkspace:
    schema_version: int
    project_id: str
    name: str
    source_locale: str
    target_locale: str
    origin: ProjectOrigin
    persistence_kind: ProjectPersistenceKind
    documents: tuple[ProjectDocument, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail()
        validate_project_id(self.project_id)
        _exact_text(
            self.name,
            max_scalars=MAX_PROJECT_NAME_SCALARS,
            allow_controls=True,
        )
        _exact_text(self.source_locale, allow_controls=True)
        _exact_text(self.target_locale, allow_controls=True)
        if type(self.origin) is not ProjectOrigin:
            _fail()
        if type(self.persistence_kind) is not ProjectPersistenceKind:
            _fail()
        _exact_tuple(self.documents)
        if not self.documents:
            _fail()
        if len(self.documents) > MAX_PROJECT_DOCUMENTS:
            _fail("PROJECT.WORKSPACE.LIMIT_EXCEEDED")
        if any(type(document) is not ProjectDocument for document in self.documents):
            _fail()
        if tuple(document.order for document in self.documents) != tuple(
            range(len(self.documents))
        ):
            _fail()
        document_ids = tuple(document.document_id for document in self.documents)
        if len(document_ids) != len(set(document_ids)):
            _fail("PROJECT.WORKSPACE.IDENTITY_DUPLICATE")
        validate_portable_ref_collection(
            tuple(document.source_ref for document in self.documents),
            allow_exact_duplicates=self.origin.kind is ProjectOriginKind.WORKBOOK,
        )
        segment_count = sum(len(document.source_segments) for document in self.documents)
        if segment_count > MAX_SEGMENTS_PER_PROJECT:
            _fail("PROJECT.WORKSPACE.LIMIT_EXCEEDED")
        if self.origin.kind is ProjectOriginKind.SINGLE_FILE and len(self.documents) != 1:
            _fail()
        if self.persistence_kind is ProjectPersistenceKind.LEGACY_SINGLE_JSON:
            if self.origin.kind is not ProjectOriginKind.SINGLE_FILE:
                _fail()
            if len(self.documents) != 1:
                _fail()
            legacy_document = self.documents[0]
            if legacy_document.format_id != "localcat-json-v1":
                _fail()
            if legacy_document.codec_identity != CodecIdentity(
                "localcat", "localcat-json", "1"
            ):
                _fail()
            if self.origin.profile_version != "localcat-json-v1":
                _fail()
            if self.origin.portable_root_ref != legacy_document.source_ref:
                _fail()
            if legacy_document.display_name != self.name:
                _fail()
            if legacy_document.codec_private_member is not None:
                _fail()
            writer = legacy_document.writer_capability_snapshot
            if (
                not writer.canonical_write
                or writer.source_round_trip_write
                or writer.format_profile != "localcat-json-v1"
            ):
                _fail()


def require_workspace_segment_identity(
    workspace: ProjectWorkspace,
    expected_project_id: str,
    identity: SegmentIdentity,
) -> ProjectSegment:
    """Resolve an identity only inside its exact project authority."""

    if type(workspace) is not ProjectWorkspace:
        _fail()
    validate_project_id(expected_project_id)
    if type(identity) is not SegmentIdentity:
        _fail()
    if workspace.project_id != expected_project_id:
        _fail()
    for document in workspace.documents:
        if document.document_id != identity.document_id:
            continue
        for segment in document.segments:
            if segment.identity == identity:
                return segment
        break
    _fail()


__all__ = (
    "CodecPrivateMemberRef",
    "EditingOverlayEntry",
    "MAX_PROJECT_DOCUMENTS",
    "MAX_SEGMENTS_PER_DOCUMENT",
    "MAX_SEGMENTS_PER_PROJECT",
    "PROJECT_LIMIT_PROFILE_ID",
    "ProjectDocument",
    "ProjectOrigin",
    "ProjectOriginKind",
    "ProjectPersistenceKind",
    "ProjectSegment",
    "ProjectSourceSegment",
    "ProjectWorkspace",
    "ProjectWorkspaceError",
    "SegmentIdentity",
    "WriterCapabilitySnapshot",
    "require_workspace_segment_identity",
)
