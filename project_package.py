"""Deterministic ProjectPackage v1 carrier and manual package transaction.

The module owns the logical ProjectPackage grammar and the one physical
carrier approved by ADR-019.  It deliberately does not import Parser codecs,
Qt, TM/Term stores, sync providers, or ResourcePackage authority.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import tempfile
from typing import BinaryIO, Iterator
import unicodedata
import zipfile
import zlib

from parser_contracts import CodecIdentity
from project_save import (
    PendingRecoveryFacts,
    ProjectRecoveryReport,
    ProjectSaveReport,
    ProjectSaveService,
    RecoveryAction,
    RecoveryPhase,
    RecoveryPreview,
    SaveJournalState,
    WorkspaceSaveBaseline,
)
from project_workspace import (
    ProjectWorkspaceService,
    ReconciliationAssociation,
    ReconciliationDecision,
    ReconciliationReceipt,
    workspace_content_digest_v1,
)
from project_workspace_contracts import (
    MAX_CODEC_PRIVATE_MEMBER_BYTES,
    MAX_PROJECT_DOCUMENTS,
    MAX_SEGMENTS_PER_DOCUMENT,
    MAX_SEGMENTS_PER_PROJECT,
    PROJECT_LIMIT_PROFILE_ID,
    CodecPrivateMemberRef,
    EditingOverlayEntry,
    OriginBinding,
    ProjectDocument,
    ProjectOrigin,
    ProjectOriginKind,
    ProjectPersistenceKind,
    ProjectSourceSegment,
    ProjectWorkspace,
    SegmentIdentity,
    SourcePresence,
    WriterCapabilitySnapshot,
)
from project_workspace_identity import (
    ProjectWorkspaceError,
    normalize_portable_ref_v1,
    validate_document_id,
    validate_project_id,
    validate_sha256,
)


PROJECT_PACKAGE_CARRIER_PROFILE = "localcat-project-package-zip-v1"
PROJECT_PACKAGE_MANIFEST_SCHEMA = "localcat-project-package-manifest-v1"
PROJECT_PACKAGE_DOCUMENT_SCHEMA = "localcat-project-package-document-v1"
PROJECT_PACKAGE_RECEIPT_SCHEMA = "localcat-project-package-receipt-v1"

MAX_PACKAGE_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_MEMBERS = 8_192
MAX_PACKAGE_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_DOCUMENT_MEMBER_BYTES = 100 * 1024 * 1024
MAX_PACKAGE_PHYSICAL_MEMBER_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_TOTAL_DECODED_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_SAFE_ISSUES = 256
MAX_PACKAGE_JSON_DEPTH = 64
_COPY_BYTES = 1024 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_OPERATION_ID = re.compile(r"(?:save|package-import)-[0-9a-f]{64}\Z")
_SAFE_CODES = frozenset(
    {
        "PROJECT.PACKAGE.SOURCE_UNSAFE",
        "PROJECT.PACKAGE.FORMAT_UNSUPPORTED",
        "PROJECT.PACKAGE.MANIFEST_INVALID",
        "PROJECT.PACKAGE.MEMBER_INVALID",
        "PROJECT.PACKAGE.DIGEST_MISMATCH",
        "PROJECT.PACKAGE.LIMIT_EXCEEDED",
        "PROJECT.PACKAGE.PREVIEW_STALE",
        "PROJECT.PACKAGE.SOURCE_STALE",
        "PROJECT.PACKAGE.DESTINATION_STALE",
        "PROJECT.PACKAGE.APPLY_FAILED",
        "PROJECT.PACKAGE.RECOVERY_REQUIRED",
        "PROJECT.PACKAGE.CODEC_UNAVAILABLE",
        "PROJECT.RECONCILE.DECISION_REQUIRED",
    }
)
_DOCUMENT_MEMBER = re.compile(r"documents/(doc-[0-9a-f]{64})\.json\Z")
_SOURCE_MEMBER = re.compile(
    r"sources/(doc-[0-9a-f]{64})/([0-9a-f]{64})\.bin\Z"
)
_PRIVATE_MEMBER = re.compile(
    r"codec-private/(doc-[0-9a-f]{64})/([0-9a-f]{64})\.bin\Z"
)
_LOCAL_HEADER = struct.Struct("<4s5H3L2H")
_CENTRAL_HEADER = struct.Struct("<4s6H3L5H2L")
_EOCD = struct.Struct("<4s4H2LH")
_DOS_DATE_1980_01_01 = 33
_VERSION_MADE_BY_UNIX_20 = (3 << 8) | 20
_VERSION_NEEDED_STORED = 10
_REGULAR_0644_EXTERNAL = (stat.S_IFREG | 0o644) << 16


def _fail(code: str) -> None:
    raise ProjectWorkspaceError(code)


def _exact_nonnegative_int(value: object, *, code: str) -> int:
    if type(value) is not int or value < 0:
        _fail(code)
    return value


def _exact_bool(value: object, *, code: str) -> bool:
    if type(value) is not bool:
        _fail(code)
    return value


def _exact_tuple(value: object, *, code: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        _fail(code)
    return value


def _safe_text(value: object, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
    assert type(value) is str
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
    if any(unicodedata.category(character) == "Cs" for character in value):
        _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
    return value


def _operation_id(value: object) -> str:
    if type(value) is not str or _OPERATION_ID.fullmatch(value) is None:
        _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
    return value


def _safe_code_tuple(value: object) -> tuple[str, ...]:
    items = _exact_tuple(value, code="PROJECT.PACKAGE.MANIFEST_INVALID")
    if (
        len(items) > MAX_PACKAGE_SAFE_ISSUES
        or len(items) != len(set(items))
        or any(type(item) is not str or item not in _SAFE_CODES for item in items)
    ):
        _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
    return items


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ProjectWorkspaceError("PROJECT.PACKAGE.MANIFEST_INVALID") from error


def _json_depth(value: object, depth: int = 1) -> int:
    if depth > MAX_PACKAGE_JSON_DEPTH:
        _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
    if type(value) is dict:
        return max(
            (depth, *(_json_depth(item, depth + 1) for item in value.values()))
        )
    if type(value) is list:
        return max((depth, *(_json_depth(item, depth + 1) for item in value)))
    return depth


def _decode_canonical_json(payload: bytes, *, manifest: bool) -> object:
    if type(payload) is not bytes:
        raise TypeError("canonical JSON payload must be exact bytes")
    duplicate = False

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                duplicate = True
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        code = (
            "PROJECT.PACKAGE.MANIFEST_INVALID"
            if manifest
            else "PROJECT.PACKAGE.MEMBER_INVALID"
        )
        raise ProjectWorkspaceError(code) from error
    if duplicate or _canonical_json(value) != payload:
        _fail(
            "PROJECT.PACKAGE.MANIFEST_INVALID"
            if manifest
            else "PROJECT.PACKAGE.MEMBER_INVALID"
        )
    _json_depth(value)
    return value


def _require_keys(value: object, expected: frozenset[str], *, manifest: bool) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != expected:
        _fail(
            "PROJECT.PACKAGE.MANIFEST_INVALID"
            if manifest
            else "PROJECT.PACKAGE.MEMBER_INVALID"
        )
    return value


@dataclass(frozen=True, slots=True)
class ProjectPackageMemberReference:
    path: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        _validate_physical_member_name(self.path)
        validate_sha256(self.sha256)
        _exact_nonnegative_int(
            self.byte_count,
            code="PROJECT.PACKAGE.MEMBER_INVALID",
        )


@dataclass(frozen=True, slots=True)
class ProjectPackageDocumentEntry:
    document_id: str
    order: int
    source_ref: str
    display_name: str
    document_member: ProjectPackageMemberReference
    source_member: ProjectPackageMemberReference
    codec_private_member: ProjectPackageMemberReference | None

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        _exact_nonnegative_int(self.order, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        if normalize_portable_ref_v1(self.source_ref) != self.source_ref:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        _safe_text(self.display_name)
        if type(self.document_member) is not ProjectPackageMemberReference:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if type(self.source_member) is not ProjectPackageMemberReference:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.codec_private_member is not None and type(
            self.codec_private_member
        ) is not ProjectPackageMemberReference:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")


@dataclass(frozen=True, slots=True)
class ProjectPackageManifest:
    schema: str
    carrier_profile: str
    limit_profile: str
    project_id: str
    workspace_content_digest: str
    name: str
    source_locale: str
    target_locale: str
    origin_kind: str
    origin_profile: str
    portable_root_ref: str
    documents: tuple[ProjectPackageDocumentEntry, ...]

    def __post_init__(self) -> None:
        if self.schema != PROJECT_PACKAGE_MANIFEST_SCHEMA:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        if self.carrier_profile != PROJECT_PACKAGE_CARRIER_PROFILE:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        if self.limit_profile != PROJECT_LIMIT_PROFILE_ID:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        validate_project_id(self.project_id)
        validate_sha256(self.workspace_content_digest)
        _safe_text(self.name)
        _safe_text(self.source_locale, allow_empty=True)
        _safe_text(self.target_locale, allow_empty=True)
        try:
            ProjectOriginKind(self.origin_kind)
        except (TypeError, ValueError):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        _safe_text(self.origin_profile)
        if normalize_portable_ref_v1(self.portable_root_ref) != self.portable_root_ref:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        _exact_tuple(self.documents, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        if not 1 <= len(self.documents) <= MAX_PROJECT_DOCUMENTS:
            _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
        if any(type(item) is not ProjectPackageDocumentEntry for item in self.documents):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if tuple(item.order for item in self.documents) != tuple(range(len(self.documents))):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        ids = tuple(item.document_id for item in self.documents)
        if len(ids) != len(set(ids)):
            _fail("PROJECT.WORKSPACE.IDENTITY_DUPLICATE")


@dataclass(frozen=True, slots=True)
class ProjectPackageValidationReport:
    artifact_digest: str
    workspace_content_digest: str
    carrier_profile: str
    manifest_schema: str
    project_id: str
    document_count: int
    segment_count: int
    member_count: int
    opaque_member_count: int
    editing_state_count: int
    artifact_byte_count: int
    decoded_member_byte_count: int
    safe_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_sha256(self.artifact_digest)
        validate_sha256(self.workspace_content_digest)
        if self.carrier_profile != PROJECT_PACKAGE_CARRIER_PROFILE:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        if self.manifest_schema != PROJECT_PACKAGE_MANIFEST_SCHEMA:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        validate_project_id(self.project_id)
        for value in (
            self.document_count,
            self.segment_count,
            self.member_count,
            self.opaque_member_count,
            self.editing_state_count,
            self.artifact_byte_count,
            self.decoded_member_byte_count,
        ):
            _exact_nonnegative_int(value, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        _exact_tuple(self.safe_issues, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.safe_issues:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")


class ProjectPackageImportMode(Enum):
    NEW = "new"
    REPLACE = "replace"
    UPDATE_SAME_PROJECT = "update_same_project"


class ProjectPackageOperationKind(Enum):
    EXPORT_COPY = "export_copy"
    SAVE = "save"
    IMPORT = "import"
    RECONCILE_IMPORT = "reconcile_import"


@dataclass(frozen=True, slots=True)
class ProjectPackagePersistenceBinding:
    """Device-local binding to one independently validated package artifact."""

    path: Path
    project_id: str
    artifact_digest: str
    workspace_content_digest: str
    device: int
    inode: int
    byte_count: int
    mtime_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            _fail("PROJECT.PACKAGE.SOURCE_UNSAFE")
        if _canonical_user_path(self.path) != self.path:
            _fail("PROJECT.PACKAGE.SOURCE_UNSAFE")
        validate_project_id(self.project_id)
        validate_sha256(self.artifact_digest)
        validate_sha256(self.workspace_content_digest)
        for value in (self.device, self.inode, self.byte_count, self.mtime_ns):
            _exact_nonnegative_int(value, code="PROJECT.PACKAGE.MANIFEST_INVALID")


@dataclass(frozen=True, slots=True)
class ProjectPackageMemberDigest:
    path: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        _validate_physical_member_name(self.path)
        validate_sha256(self.sha256)
        _exact_nonnegative_int(
            self.byte_count,
            code="PROJECT.PACKAGE.MANIFEST_INVALID",
        )


@dataclass(frozen=True, slots=True)
class ProjectPackageDocumentResult:
    document_id: str
    status: str
    safe_code: str | None = None

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        if self.status not in {"saved", "installed", "reconciled", "unchanged"}:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.safe_code is not None:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")


@dataclass(frozen=True, slots=True)
class ProjectPackageCodecAvailability:
    """Body-safe live-codec projection for one packaged document.

    This is deliberately descriptive only: it neither grants writer authority
    nor asks Core to interpret codec-private bytes.
    """

    document_id: str
    codec_identity: CodecIdentity
    available: bool
    safe_warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        if type(self.codec_identity) is not CodecIdentity:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        _exact_bool(self.available, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        _safe_code_tuple(self.safe_warnings)
        expected = () if self.available else ("PROJECT.PACKAGE.CODEC_UNAVAILABLE",)
        if self.safe_warnings != expected:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")


@dataclass(frozen=True, slots=True)
class ProjectPackageImportPreview:
    operation_id: str
    mode: ProjectPackageImportMode
    project_id: str
    project_name: str
    source_artifact_digest: str
    destination_before_digest: str | None
    workspace_content_digest: str
    document_count: int
    segment_count: int
    editing_state_count: int
    opaque_member_count: int
    destination_exists: bool
    same_project: bool
    unchanged_count: int = 0
    source_changed_count: int = 0
    new_count: int = 0
    removed_count: int = 0
    ambiguous_count: int = 0
    unresolved_count: int = 0
    safe_warnings: tuple[str, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    required_decision_identities: tuple[SegmentIdentity, ...] = ()

    def __post_init__(self) -> None:
        if not _operation_id(self.operation_id).startswith("package-import-"):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if type(self.mode) is not ProjectPackageImportMode:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        validate_project_id(self.project_id)
        _safe_text(self.project_name)
        validate_sha256(self.source_artifact_digest)
        if self.destination_before_digest is not None:
            validate_sha256(self.destination_before_digest)
        validate_sha256(self.workspace_content_digest)
        for value in (
            self.document_count,
            self.segment_count,
            self.editing_state_count,
            self.opaque_member_count,
            self.unchanged_count,
            self.source_changed_count,
            self.new_count,
            self.removed_count,
            self.ambiguous_count,
            self.unresolved_count,
        ):
            _exact_nonnegative_int(value, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        _exact_bool(self.destination_exists, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        _exact_bool(self.same_project, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        _safe_code_tuple(self.safe_warnings)
        _safe_code_tuple(self.blocking_reasons)
        _exact_tuple(
            self.required_decision_identities,
            code="PROJECT.PACKAGE.MANIFEST_INVALID",
        )
        if any(
            type(item) is not SegmentIdentity
            for item in self.required_decision_identities
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if len(self.required_decision_identities) != len(
            set(self.required_decision_identities)
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.mode is ProjectPackageImportMode.NEW:
            valid_mode = (
                not self.destination_exists
                and not self.same_project
                and self.destination_before_digest is None
            )
        elif self.mode is ProjectPackageImportMode.REPLACE:
            valid_mode = (
                self.destination_exists
                and not self.same_project
                and self.destination_before_digest is not None
            )
        else:
            valid_mode = (
                self.destination_exists
                and self.same_project
                and self.destination_before_digest is not None
            )
        if not valid_mode:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.mode is not ProjectPackageImportMode.UPDATE_SAME_PROJECT and (
            self.unchanged_count
            or self.source_changed_count
            or self.new_count
            or self.removed_count
            or self.ambiguous_count
            or self.unresolved_count
            or self.blocking_reasons
            or self.required_decision_identities
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")


@dataclass(frozen=True, slots=True)
class ProjectPackageExportReceipt:
    receipt_schema: str
    operation_id: str
    project_id: str
    carrier_profile: str
    manifest_schema: str
    artifact_digest: str
    workspace_content_digest: str
    destination_before_digest: str | None
    document_count: int
    segment_count: int
    member_count: int
    byte_count: int
    durable: bool
    recovery_required: bool
    operation_kind: ProjectPackageOperationKind = ProjectPackageOperationKind.SAVE
    workspace_revision: int = 0
    member_digests: tuple[ProjectPackageMemberDigest, ...] = ()
    document_results: tuple[ProjectPackageDocumentResult, ...] = ()
    safe_warnings: tuple[str, ...] = ()
    safe_errors: tuple[str, ...] = ()
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.receipt_schema != PROJECT_PACKAGE_RECEIPT_SCHEMA:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if not _operation_id(self.operation_id).startswith("save-"):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        validate_project_id(self.project_id)
        if self.carrier_profile != PROJECT_PACKAGE_CARRIER_PROFILE:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        if self.manifest_schema != PROJECT_PACKAGE_MANIFEST_SCHEMA:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        validate_sha256(self.artifact_digest)
        validate_sha256(self.workspace_content_digest)
        if self.destination_before_digest is not None:
            validate_sha256(self.destination_before_digest)
        for value in (self.document_count, self.segment_count, self.member_count, self.byte_count):
            _exact_nonnegative_int(value, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        _exact_bool(self.durable, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        _exact_bool(self.recovery_required, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.durable == self.recovery_required:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if type(self.operation_kind) is not ProjectPackageOperationKind or self.operation_kind not in {
            ProjectPackageOperationKind.EXPORT_COPY,
            ProjectPackageOperationKind.SAVE,
        }:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        _exact_nonnegative_int(self.workspace_revision, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        for values, expected in (
            (self.member_digests, ProjectPackageMemberDigest),
            (self.document_results, ProjectPackageDocumentResult),
        ):
            _exact_tuple(values, code="PROJECT.PACKAGE.MANIFEST_INVALID")
            if any(type(item) is not expected for item in values):
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        _safe_code_tuple(self.safe_warnings)
        _safe_code_tuple(self.safe_errors)
        _exact_bool(self.retryable, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.durable and (self.safe_errors or self.retryable):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if not self.durable or self.recovery_required:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if (
            len(self.member_digests) != self.member_count
            or len({item.path for item in self.member_digests})
            != len(self.member_digests)
            or len(self.document_results) != self.document_count
            or len({item.document_id for item in self.document_results})
            != len(self.document_results)
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if any(
            item.status not in {"saved", "unchanged"}
            for item in self.document_results
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")


@dataclass(frozen=True, slots=True)
class ProjectPackageImportReceipt:
    receipt_schema: str
    operation_id: str
    project_id: str
    carrier_profile: str
    manifest_schema: str
    source_artifact_digest: str
    destination_before_digest: str | None
    destination_after_digest: str
    workspace_content_digest: str
    document_count: int
    segment_count: int
    member_count: int
    byte_count: int
    durable: bool
    recovery_required: bool
    operation_kind: ProjectPackageOperationKind = ProjectPackageOperationKind.IMPORT
    mode: ProjectPackageImportMode = ProjectPackageImportMode.NEW
    workspace_revision: int = 0
    member_digests: tuple[ProjectPackageMemberDigest, ...] = ()
    document_results: tuple[ProjectPackageDocumentResult, ...] = ()
    reconciliation: ReconciliationReceipt | None = None
    safe_warnings: tuple[str, ...] = ()
    safe_errors: tuple[str, ...] = ()
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.receipt_schema != PROJECT_PACKAGE_RECEIPT_SCHEMA:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if not _operation_id(self.operation_id).startswith("package-import-"):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        validate_project_id(self.project_id)
        if self.carrier_profile != PROJECT_PACKAGE_CARRIER_PROFILE:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        if self.manifest_schema != PROJECT_PACKAGE_MANIFEST_SCHEMA:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        for digest in (
            self.source_artifact_digest,
            self.destination_after_digest,
            self.workspace_content_digest,
        ):
            validate_sha256(digest)
        if self.destination_before_digest is not None:
            validate_sha256(self.destination_before_digest)
        for value in (self.document_count, self.segment_count, self.member_count, self.byte_count):
            _exact_nonnegative_int(value, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        _exact_bool(self.durable, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        _exact_bool(self.recovery_required, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.durable == self.recovery_required:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if type(self.operation_kind) is not ProjectPackageOperationKind or self.operation_kind not in {
            ProjectPackageOperationKind.IMPORT,
            ProjectPackageOperationKind.RECONCILE_IMPORT,
        }:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if type(self.mode) is not ProjectPackageImportMode:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        _exact_nonnegative_int(self.workspace_revision, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        for values, expected in (
            (self.member_digests, ProjectPackageMemberDigest),
            (self.document_results, ProjectPackageDocumentResult),
        ):
            _exact_tuple(values, code="PROJECT.PACKAGE.MANIFEST_INVALID")
            if any(type(item) is not expected for item in values):
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        _safe_code_tuple(self.safe_warnings)
        _safe_code_tuple(self.safe_errors)
        _exact_bool(self.retryable, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.durable and (self.safe_errors or self.retryable):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if not self.durable or self.recovery_required:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if (
            len(self.member_digests) != self.member_count
            or len({item.path for item in self.member_digests})
            != len(self.member_digests)
            or len(self.document_results) != self.document_count
            or len({item.document_id for item in self.document_results})
            != len(self.document_results)
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.mode is ProjectPackageImportMode.UPDATE_SAME_PROJECT:
            if (
                self.operation_kind is not ProjectPackageOperationKind.RECONCILE_IMPORT
                or type(self.reconciliation) is not ReconciliationReceipt
            ):
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
            if any(item.status != "reconciled" for item in self.document_results):
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
            if (
                self.reconciliation.project_id != self.project_id
                or self.reconciliation.published_revision != self.workspace_revision
                or self.reconciliation.published_workspace_digest
                != self.workspace_content_digest
            ):
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        elif (
            self.operation_kind is not ProjectPackageOperationKind.IMPORT
            or self.reconciliation is not None
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        elif any(item.status != "installed" for item in self.document_results):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if (
            (self.mode is ProjectPackageImportMode.NEW)
            != (self.destination_before_digest is None)
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if (
            self.mode
            in {ProjectPackageImportMode.NEW, ProjectPackageImportMode.REPLACE}
            and self.source_artifact_digest != self.destination_after_digest
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")


@dataclass(frozen=True, slots=True)
class ProjectPackageExportResult:
    save_report: ProjectSaveReport
    receipt: ProjectPackageExportReceipt | None
    persistence_binding: ProjectPackagePersistenceBinding | None = None

    def __post_init__(self) -> None:
        if type(self.save_report) is not ProjectSaveReport:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.receipt is not None and type(self.receipt) is not ProjectPackageExportReceipt:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.save_report.journal_state is SaveJournalState.COMMITTED:
            if (
                self.receipt is None
                or not self.receipt.durable
                or type(self.persistence_binding)
                is not ProjectPackagePersistenceBinding
            ):
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        elif (
            self.save_report.journal_state is SaveJournalState.RECOVERY_REQUIRED
            and self.save_report.saved_count > 0
        ):
            if (
                self.receipt is not None
                or type(self.persistence_binding)
                is not ProjectPackagePersistenceBinding
                or self.persistence_binding.workspace_content_digest
                != self.save_report.workspace_content_digest
            ):
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        elif self.receipt is not None or self.persistence_binding is not None:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.receipt is not None and (
            self.receipt.operation_id != self.save_report.operation_id
            or self.receipt.workspace_revision != self.save_report.workspace_revision
            or self.receipt.workspace_content_digest
            != self.save_report.workspace_content_digest
            or tuple(item.document_id for item in self.receipt.document_results)
            != tuple(item.document_id for item in self.save_report.document_results)
            or tuple(item.status for item in self.receipt.document_results)
            != tuple(
                "unchanged" if item.status.value == "unchanged" else "saved"
                for item in self.save_report.document_results
            )
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.receipt is not None:
            assert type(self.persistence_binding) is ProjectPackagePersistenceBinding
            if (
                self.persistence_binding.project_id != self.receipt.project_id
                or self.persistence_binding.artifact_digest
                != self.receipt.artifact_digest
                or self.persistence_binding.workspace_content_digest
                != self.receipt.workspace_content_digest
            ):
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")


@dataclass(frozen=True, slots=True)
class ProjectPackageRecoveryPreview:
    operation_id: str
    project_id: str
    phase: RecoveryPhase
    last_known_good_digest: str | None
    candidate_digest: str
    available_actions: tuple[RecoveryAction, ...]
    safe_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _operation_id(self.operation_id)
        validate_project_id(self.project_id)
        if type(self.phase) is not RecoveryPhase:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        if self.last_known_good_digest is not None:
            validate_sha256(self.last_known_good_digest)
        validate_sha256(self.candidate_digest)
        _exact_tuple(self.available_actions, code="PROJECT.PACKAGE.MANIFEST_INVALID")
        if (
            len(self.available_actions) != len(set(self.available_actions))
            or any(type(item) is not RecoveryAction for item in self.available_actions)
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
        _safe_code_tuple(self.safe_codes)
        expected_actions = (
            ((RecoveryAction.ABANDON_STAGED_COPY,),)
            if self.phase
            in {RecoveryPhase.STAGING, RecoveryPhase.STAGED, RecoveryPhase.ARMED}
            else (
                (RecoveryAction.COMPLETE_COMMIT, RecoveryAction.ROLLBACK),
                (RecoveryAction.COMPLETE_COMMIT,),
            )
        )
        if self.available_actions not in expected_actions:
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")


@dataclass(frozen=True, slots=True)
class _FileFacts:
    device: int
    inode: int
    size: int
    mtime_ns: int
    digest: str


@dataclass(frozen=True, slots=True)
class _RawEntry:
    name: str
    crc32: int
    byte_count: int
    local_offset: int
    data_offset: int


@dataclass(frozen=True, slots=True)
class _Blob:
    member_path: str
    sha256: str
    byte_count: int
    opener: object

    @contextmanager
    def open(self) -> Iterator[BinaryIO]:
        stream = self.opener()
        if not hasattr(stream, "read") or not hasattr(stream, "close"):
            raise TypeError("blob opener must return a binary reader")
        try:
            yield stream
        finally:
            stream.close()


@dataclass(frozen=True, slots=True)
class _ParentFacts:
    device: int
    inode: int


def _canonical_user_path(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or not path.name:
        _fail("PROJECT.PACKAGE.SOURCE_UNSAFE")
    try:
        parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectWorkspaceError("PROJECT.PACKAGE.SOURCE_UNSAFE") from error
    return parent / path.name


def _selected_user_path(path: object) -> Path:
    if not isinstance(path, Path):
        raise TypeError("package path must be pathlib.Path")
    try:
        selected = path if path.is_absolute() else path.absolute()
    except (OSError, RuntimeError) as error:
        raise ProjectWorkspaceError("PROJECT.PACKAGE.SOURCE_UNSAFE") from error
    return _canonical_user_path(selected)


def _bind_parent(path: Path) -> _ParentFacts:
    canonical = _canonical_user_path(path)
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(canonical.parent, flags)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISDIR(status.st_mode):
                _fail("PROJECT.PACKAGE.SOURCE_UNSAFE")
            return _ParentFacts(status.st_dev, status.st_ino)
        finally:
            os.close(descriptor)
    except ProjectWorkspaceError:
        raise
    except OSError as error:
        raise ProjectWorkspaceError("PROJECT.PACKAGE.SOURCE_UNSAFE") from error


def _require_parent(
    path: Path,
    expected: _ParentFacts,
    *,
    code: str = "PROJECT.PACKAGE.DESTINATION_STALE",
) -> None:
    try:
        observed = _bind_parent(path)
    except ProjectWorkspaceError as error:
        raise ProjectWorkspaceError(code) from error
    if observed != expected:
        _fail(code)


@contextmanager
def _bound_parent_descriptor(
    path: Path,
    expected: _ParentFacts,
) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path.parent, flags)
        status = os.fstat(descriptor)
    except OSError as error:
        raise ProjectWorkspaceError("PROJECT.PACKAGE.DESTINATION_STALE") from error
    if (
        not stat.S_ISDIR(status.st_mode)
        or (status.st_dev, status.st_ino) != (expected.device, expected.inode)
    ):
        os.close(descriptor)
        descriptor = -1
        _fail("PROJECT.PACKAGE.DESTINATION_STALE")
    try:
        yield descriptor
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _replace_in_bound_parent(
    source: Path,
    destination: Path,
    expected: _ParentFacts,
) -> None:
    if source.parent != destination.parent:
        _fail("PROJECT.PACKAGE.DESTINATION_STALE")
    with _bound_parent_descriptor(destination, expected) as parent:
        os.replace(
            source.name,
            destination.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        os.fsync(parent)


def _unlink_in_bound_parent(
    path: Path,
    expected: _ParentFacts,
    *,
    missing_ok: bool = False,
) -> None:
    with _bound_parent_descriptor(path, expected) as parent:
        try:
            os.unlink(path.name, dir_fd=parent)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        os.fsync(parent)


@dataclass(frozen=True, slots=True)
class ProjectPackageBlobSource:
    document_id: str
    member_path: str
    path: Path
    expected_sha256: str
    expected_byte_count: int
    _file_facts: _FileFacts = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validate_document_id(self.document_id)
        _validate_physical_member_name(self.member_path)
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            _fail("PROJECT.PACKAGE.SOURCE_UNSAFE")
        validate_sha256(self.expected_sha256)
        _exact_nonnegative_int(
            self.expected_byte_count,
            code="PROJECT.PACKAGE.MEMBER_INVALID",
        )
        if self.expected_byte_count > MAX_CODEC_PRIVATE_MEMBER_BYTES:
            _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
        object.__setattr__(self, "path", _canonical_user_path(self.path))
        descriptor, facts = _open_regular(self.path, include_digest=True)
        os.close(descriptor)
        if (
            facts.digest != self.expected_sha256
            or facts.size != self.expected_byte_count
        ):
            _fail("PROJECT.PACKAGE.SOURCE_STALE")
        object.__setattr__(self, "_file_facts", facts)

    @classmethod
    def from_path(
        cls,
        *,
        document_id: str,
        member_path: str,
        path: Path,
        expected_sha256: str,
        expected_byte_count: int,
    ) -> ProjectPackageBlobSource:
        return cls(
            document_id=document_id,
            member_path=member_path,
            path=path,
            expected_sha256=expected_sha256,
            expected_byte_count=expected_byte_count,
        )

    def _blob(self) -> _Blob:
        def open_source() -> BinaryIO:
            descriptor, facts = _open_regular(self.path, include_digest=True)
            if facts != self._file_facts:
                os.close(descriptor)
                _fail("PROJECT.PACKAGE.SOURCE_STALE")
            return os.fdopen(descriptor, "rb", closefd=True)

        return _Blob(
            self.member_path,
            self.expected_sha256,
            self.expected_byte_count,
            open_source,
        )


def _open_regular(path: Path, *, include_digest: bool) -> tuple[int, _FileFacts]:
    if not isinstance(path, Path) or not path.is_absolute() or not path.name:
        _fail("PROJECT.PACKAGE.SOURCE_UNSAFE")
    parent_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        parent_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        parent_flags |= os.O_CLOEXEC
    descriptor = -1
    parent = -1
    try:
        parent = os.open(path.parent, parent_flags)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(path.name, flags, dir_fd=parent)
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            _fail("PROJECT.PACKAGE.SOURCE_UNSAFE")
        digest = "0" * 64
        if include_digest:
            hasher = hashlib.sha256()
            offset = 0
            while True:
                block = os.pread(descriptor, _COPY_BYTES, offset)
                if not block:
                    break
                hasher.update(block)
                offset += len(block)
            digest = hasher.hexdigest()
        return descriptor, _FileFacts(
            status.st_dev,
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            digest,
        )
    except ProjectWorkspaceError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise ProjectWorkspaceError("PROJECT.PACKAGE.SOURCE_UNSAFE") from error
    finally:
        if parent >= 0:
            os.close(parent)


def _validate_physical_member_name(value: object) -> str:
    if type(value) is not str or not value or not value.isascii():
        _fail("PROJECT.PACKAGE.MEMBER_INVALID")
    if value == "manifest.json":
        return value
    match = (
        _DOCUMENT_MEMBER.fullmatch(value)
        or _SOURCE_MEMBER.fullmatch(value)
        or _PRIVATE_MEMBER.fullmatch(value)
    )
    if match is None:
        _fail("PROJECT.PACKAGE.MEMBER_INVALID")
    return value


def _member_order_key(name: str) -> tuple[int, bytes]:
    if name == "manifest.json":
        return (0, b"")
    if name.startswith("documents/"):
        group = 1
    elif name.startswith("sources/"):
        group = 2
    elif name.startswith("codec-private/"):
        group = 3
    else:
        _fail("PROJECT.PACKAGE.MEMBER_INVALID")
    return group, name.encode("ascii")


def _stream_digest(stream: BinaryIO, *, expected_count: int | None = None) -> tuple[str, int]:
    hasher = hashlib.sha256()
    count = 0
    while True:
        block = stream.read(_COPY_BYTES)
        if not block:
            break
        if type(block) is not bytes:
            raise TypeError("blob reader must return exact bytes")
        count += len(block)
        if count > MAX_PACKAGE_TOTAL_DECODED_BYTES:
            _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
        hasher.update(block)
    if expected_count is not None and count != expected_count:
        _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")
    return hasher.hexdigest(), count


def _artifact_digest(descriptor: int, byte_count: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < byte_count:
        block = os.pread(descriptor, min(_COPY_BYTES, byte_count - offset), offset)
        if not block:
            _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()


def _require_descriptor_snapshot(
    descriptor: int,
    expected: _FileFacts,
    *,
    code: str = "PROJECT.PACKAGE.SOURCE_STALE",
) -> None:
    """Prove that one retained descriptor still names the validated generation."""

    try:
        status = os.fstat(descriptor)
    except OSError as error:
        raise ProjectWorkspaceError(code) from error
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or status.st_dev != expected.device
        or status.st_ino != expected.inode
        or status.st_size != expected.size
        or status.st_mtime_ns != expected.mtime_ns
        or _artifact_digest(descriptor, expected.size) != expected.digest
    ):
        _fail(code)


def _require_path_snapshot(path: Path, expected: _FileFacts) -> None:
    descriptor, observed = _open_regular(path, include_digest=True)
    try:
        if observed != expected:
            _fail("PROJECT.PACKAGE.SOURCE_STALE")
    finally:
        os.close(descriptor)


def _parse_raw_zip(descriptor: int, artifact_size: int) -> tuple[_RawEntry, ...]:
    if artifact_size < _EOCD.size or artifact_size > MAX_PACKAGE_ARTIFACT_BYTES:
        _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
    eocd_bytes = os.pread(descriptor, _EOCD.size, artifact_size - _EOCD.size)
    if len(eocd_bytes) != _EOCD.size:
        _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
    try:
        signature, disk, central_disk, on_disk, total, central_size, central_offset, comment = _EOCD.unpack(eocd_bytes)
    except struct.error as error:
        raise ProjectWorkspaceError("PROJECT.PACKAGE.FORMAT_UNSUPPORTED") from error
    if (
        signature != b"PK\x05\x06"
        or disk != 0
        or central_disk != 0
        or on_disk != total
        or total < 1
        or total > MAX_PACKAGE_MEMBERS
        or comment != 0
        or central_offset + central_size + _EOCD.size != artifact_size
    ):
        _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")

    local_entries: list[_RawEntry] = []
    offset = 0
    while offset < central_offset:
        header = os.pread(descriptor, _LOCAL_HEADER.size, offset)
        if len(header) != _LOCAL_HEADER.size:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        try:
            (
                signature,
                needed,
                flags,
                method,
                dos_time,
                dos_date,
                crc,
                compressed,
                uncompressed,
                name_count,
                extra_count,
            ) = _LOCAL_HEADER.unpack(header)
        except struct.error as error:
            raise ProjectWorkspaceError("PROJECT.PACKAGE.FORMAT_UNSUPPORTED") from error
        if (
            signature != b"PK\x03\x04"
            or needed != _VERSION_NEEDED_STORED
            or flags != 0
            or method != zipfile.ZIP_STORED
            or dos_time != 0
            or dos_date != _DOS_DATE_1980_01_01
            or compressed != uncompressed
            or extra_count != 0
            or name_count == 0
            or uncompressed > MAX_PACKAGE_PHYSICAL_MEMBER_BYTES
        ):
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        name_bytes = os.pread(descriptor, name_count, offset + _LOCAL_HEADER.size)
        try:
            name = name_bytes.decode("ascii", errors="strict")
        except UnicodeError as error:
            raise ProjectWorkspaceError("PROJECT.PACKAGE.MEMBER_INVALID") from error
        _validate_physical_member_name(name)
        data_offset = offset + _LOCAL_HEADER.size + name_count
        next_offset = data_offset + uncompressed
        if next_offset > central_offset:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        local_entries.append(_RawEntry(name, crc, uncompressed, offset, data_offset))
        offset = next_offset
    if offset != central_offset or len(local_entries) != total:
        _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")

    central_entries: list[_RawEntry] = []
    offset = central_offset
    for _index in range(total):
        header = os.pread(descriptor, _CENTRAL_HEADER.size, offset)
        if len(header) != _CENTRAL_HEADER.size:
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        try:
            (
                signature,
                made_by,
                needed,
                flags,
                method,
                dos_time,
                dos_date,
                crc,
                compressed,
                uncompressed,
                name_count,
                extra_count,
                comment_count,
                disk_start,
                internal_attr,
                external_attr,
                local_offset,
            ) = _CENTRAL_HEADER.unpack(header)
        except struct.error as error:
            raise ProjectWorkspaceError("PROJECT.PACKAGE.FORMAT_UNSUPPORTED") from error
        if (
            signature != b"PK\x01\x02"
            or made_by != _VERSION_MADE_BY_UNIX_20
            or needed != _VERSION_NEEDED_STORED
            or flags != 0
            or method != zipfile.ZIP_STORED
            or dos_time != 0
            or dos_date != _DOS_DATE_1980_01_01
            or compressed != uncompressed
            or extra_count != 0
            or comment_count != 0
            or disk_start != 0
            or internal_attr != 0
            or external_attr != _REGULAR_0644_EXTERNAL
            or name_count == 0
        ):
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
        name_offset = offset + _CENTRAL_HEADER.size
        name_bytes = os.pread(descriptor, name_count, name_offset)
        try:
            name = name_bytes.decode("ascii", errors="strict")
        except UnicodeError as error:
            raise ProjectWorkspaceError("PROJECT.PACKAGE.MEMBER_INVALID") from error
        _validate_physical_member_name(name)
        central_entries.append(_RawEntry(name, crc, uncompressed, local_offset, -1))
        offset = name_offset + name_count
    if offset != central_offset + central_size:
        _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")

    names = tuple(item.name for item in local_entries)
    if (
        len(names) != len(set(names))
        or len({unicodedata.normalize("NFC", name).casefold() for name in names})
        != len(names)
        or names != tuple(sorted(names, key=_member_order_key))
        or names[0] != "manifest.json"
    ):
        _fail("PROJECT.PACKAGE.MEMBER_INVALID")
    if len(central_entries) != len(local_entries):
        _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
    for local, central in zip(local_entries, central_entries, strict=True):
        if (
            local.name != central.name
            or local.crc32 != central.crc32
            or local.byte_count != central.byte_count
            or local.local_offset != central.local_offset
        ):
            _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
    return tuple(local_entries)


def _read_entry(
    descriptor: int,
    entry: _RawEntry,
    *,
    materialize: bool,
) -> tuple[str, bytes | None]:
    digest = hashlib.sha256()
    crc = 0
    count = 0
    output = bytearray() if materialize else None
    while count < entry.byte_count:
        block = os.pread(
            descriptor,
            min(_COPY_BYTES, entry.byte_count - count),
            entry.data_offset + count,
        )
        if not block:
            _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")
        count += len(block)
        digest.update(block)
        crc = zlib.crc32(block, crc)
        if output is not None:
            output.extend(block)
    if count != entry.byte_count or crc & 0xFFFFFFFF != entry.crc32:
        _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")
    return digest.hexdigest(), None if output is None else bytes(output)


class _StoredMemberReader:
    """Bounded stored-member reader over one retained package descriptor."""

    __slots__ = (
        "_descriptor",
        "_entry",
        "_expected_digest",
        "_file_facts",
        "_path",
        "_offset",
        "_crc",
        "_digest",
        "_closed",
        "_verified",
    )

    def __init__(
        self,
        descriptor: int,
        entry: _RawEntry,
        *,
        expected_digest: str,
        file_facts: _FileFacts,
        path: Path,
    ) -> None:
        self._descriptor = descriptor
        self._entry = entry
        self._expected_digest = expected_digest
        self._file_facts = file_facts
        self._path = path
        self._offset = 0
        self._crc = 0
        self._digest = hashlib.sha256()
        self._closed = False
        self._verified = False

    def readable(self) -> bool:
        return not self._closed

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("member reader is closed")
        if type(size) is not int:
            raise TypeError("member read size must be exact int")
        remaining = self._entry.byte_count - self._offset
        requested = remaining if size < 0 else min(size, remaining)
        if requested == 0:
            self._verify_complete()
            return b""
        payload = os.pread(
            self._descriptor,
            requested,
            self._entry.data_offset + self._offset,
        )
        if not payload:
            _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")
        self._offset += len(payload)
        self._crc = zlib.crc32(payload, self._crc)
        self._digest.update(payload)
        if self._offset == self._entry.byte_count:
            self._verify_complete()
        return payload

    def _verify_complete(self) -> None:
        if self._verified or self._offset != self._entry.byte_count:
            return
        if (
            self._crc & 0xFFFFFFFF != self._entry.crc32
            or self._digest.hexdigest() != self._expected_digest
        ):
            _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")
        _require_descriptor_snapshot(self._descriptor, self._file_facts)
        _require_path_snapshot(self._path, self._file_facts)
        self._verified = True

    def close(self) -> None:
        if self._closed:
            return
        try:
            while self._offset < self._entry.byte_count:
                self.read(min(_COPY_BYTES, self._entry.byte_count - self._offset))
            self._verify_complete()
        finally:
            os.close(self._descriptor)
            self._closed = True

    def __enter__(self) -> _StoredMemberReader:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def _member_ref(value: object, *, manifest: bool) -> ProjectPackageMemberReference:
    item = _require_keys(
        value,
        frozenset({"path", "sha256", "byte_count"}),
        manifest=manifest,
    )
    return ProjectPackageMemberReference(
        path=item["path"],
        sha256=item["sha256"],
        byte_count=item["byte_count"],
    )


def _ref_json(value: ProjectPackageMemberReference) -> dict[str, object]:
    return {"byte_count": value.byte_count, "path": value.path, "sha256": value.sha256}


def _codec_json(identity: CodecIdentity) -> dict[str, object]:
    return {
        "codec_id": identity.codec_id,
        "codec_version": identity.codec_version,
        "provider_id": identity.provider_id,
    }


def _decode_codec(value: object) -> CodecIdentity:
    item = _require_keys(
        value,
        frozenset({"provider_id", "codec_id", "codec_version"}),
        manifest=False,
    )
    try:
        return CodecIdentity(item["provider_id"], item["codec_id"], item["codec_version"])
    except (TypeError, ValueError) as error:
        raise ProjectWorkspaceError("PROJECT.PACKAGE.MEMBER_INVALID") from error


def _document_json(document: ProjectDocument) -> bytes:
    return _canonical_json(
        {
            "codec_identity": _codec_json(document.codec_identity),
            "codec_private_member": (
                None
                if document.codec_private_member is None
                else {
                    **_ref_json(
                        ProjectPackageMemberReference(
                            document.codec_private_member.member_path,
                            document.codec_private_member.sha256,
                            document.codec_private_member.byte_count,
                        )
                    ),
                    "codec_identity": _codec_json(
                        document.codec_private_member.codec_identity
                    ),
                    "profile_version": document.codec_private_member.profile_version,
                }
            ),
            "display_name": document.display_name,
            "document_id": document.document_id,
            "editing_overlay": [
                {
                    "confirmed": item.confirmed,
                    "document_id": item.document_id,
                    "local_segment_id": item.local_segment_id,
                    "saved_state_digest": item.saved_state_digest,
                    "source_fingerprint": item.source_fingerprint,
                    "target": item.target,
                }
                for item in document.editing_overlay
            ],
            "format_id": document.format_id,
            "order": document.order,
            "schema": PROJECT_PACKAGE_DOCUMENT_SCHEMA,
            "source_ref": document.source_ref,
            "source_segments": [
                {
                    "local_segment_id": item.local_segment_id,
                    "raw_speaker": item.raw_speaker,
                    "source": item.source,
                    "source_fingerprint": item.source_fingerprint,
                    "source_presence": item.source_presence.value,
                }
                for item in document.source_segments
            ],
            "source_snapshot_digest": document.source_snapshot_digest,
            "writer_capability_snapshot": {
                "canonical_write": document.writer_capability_snapshot.canonical_write,
                "format_profile": document.writer_capability_snapshot.format_profile,
                "source_round_trip_write": document.writer_capability_snapshot.source_round_trip_write,
            },
        }
    )


def _decode_document(payload: bytes) -> ProjectDocument:
    item = _require_keys(
        _decode_canonical_json(payload, manifest=False),
        frozenset(
            {
                "schema",
                "document_id",
                "source_ref",
                "display_name",
                "order",
                "format_id",
                "codec_identity",
                "writer_capability_snapshot",
                "source_snapshot_digest",
                "source_segments",
                "editing_overlay",
                "codec_private_member",
            }
        ),
        manifest=False,
    )
    if item["schema"] != PROJECT_PACKAGE_DOCUMENT_SCHEMA:
        _fail("PROJECT.PACKAGE.FORMAT_UNSUPPORTED")
    writer = _require_keys(
        item["writer_capability_snapshot"],
        frozenset({"canonical_write", "source_round_trip_write", "format_profile"}),
        manifest=False,
    )
    sources = item["source_segments"]
    overlays = item["editing_overlay"]
    if type(sources) is not list or type(overlays) is not list:
        _fail("PROJECT.PACKAGE.MEMBER_INVALID")
    if not sources or len(sources) > MAX_SEGMENTS_PER_DOCUMENT:
        _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
    source_values: list[ProjectSourceSegment] = []
    for source in sources:
        value = _require_keys(
            source,
            frozenset(
                {
                    "local_segment_id",
                    "source",
                    "raw_speaker",
                    "source_fingerprint",
                    "source_presence",
                }
            ),
            manifest=False,
        )
        try:
            presence = SourcePresence(value["source_presence"])
            source_values.append(
                ProjectSourceSegment(
                    local_segment_id=value["local_segment_id"],
                    source=value["source"],
                    raw_speaker=value["raw_speaker"],
                    source_fingerprint=value["source_fingerprint"],
                    source_presence=presence,
                )
            )
        except (TypeError, ValueError, ProjectWorkspaceError) as error:
            raise ProjectWorkspaceError("PROJECT.PACKAGE.MEMBER_INVALID") from error
    overlay_values: list[EditingOverlayEntry] = []
    for overlay in overlays:
        value = _require_keys(
            overlay,
            frozenset(
                {
                    "document_id",
                    "local_segment_id",
                    "source_fingerprint",
                    "target",
                    "confirmed",
                    "saved_state_digest",
                }
            ),
            manifest=False,
        )
        try:
            overlay_values.append(
                EditingOverlayEntry(
                    document_id=value["document_id"],
                    local_segment_id=value["local_segment_id"],
                    source_fingerprint=value["source_fingerprint"],
                    target=value["target"],
                    confirmed=value["confirmed"],
                    saved_state_digest=value["saved_state_digest"],
                )
            )
        except (TypeError, ValueError, ProjectWorkspaceError) as error:
            raise ProjectWorkspaceError("PROJECT.PACKAGE.MEMBER_INVALID") from error
    private_value = item["codec_private_member"]
    private = None
    if private_value is not None:
        value = _require_keys(
            private_value,
            frozenset(
                {"path", "sha256", "byte_count", "codec_identity", "profile_version"}
            ),
            manifest=False,
        )
        reference = _member_ref(
            {key: value[key] for key in ("path", "sha256", "byte_count")},
            manifest=False,
        )
        private = CodecPrivateMemberRef(
            member_path=reference.path,
            sha256=reference.sha256,
            byte_count=reference.byte_count,
            codec_identity=_decode_codec(value["codec_identity"]),
            profile_version=value["profile_version"],
        )
    try:
        return ProjectDocument(
            document_id=item["document_id"],
            source_ref=item["source_ref"],
            display_name=item["display_name"],
            order=item["order"],
            format_id=item["format_id"],
            codec_identity=_decode_codec(item["codec_identity"]),
            writer_capability_snapshot=WriterCapabilitySnapshot(
                canonical_write=writer["canonical_write"],
                source_round_trip_write=writer["source_round_trip_write"],
                format_profile=writer["format_profile"],
            ),
            source_snapshot_digest=item["source_snapshot_digest"],
            source_segments=tuple(source_values),
            editing_overlay=tuple(overlay_values),
            codec_private_member=private,
        )
    except (TypeError, ValueError, ProjectWorkspaceError) as error:
        raise ProjectWorkspaceError("PROJECT.PACKAGE.MEMBER_INVALID") from error


def _manifest_json(manifest: ProjectPackageManifest) -> bytes:
    return _canonical_json(
        {
            "carrier_profile": manifest.carrier_profile,
            "documents": [
                {
                    "codec_private_member": (
                        None
                        if entry.codec_private_member is None
                        else _ref_json(entry.codec_private_member)
                    ),
                    "display_name": entry.display_name,
                    "document_id": entry.document_id,
                    "document_member": _ref_json(entry.document_member),
                    "order": entry.order,
                    "source_member": _ref_json(entry.source_member),
                    "source_ref": entry.source_ref,
                }
                for entry in manifest.documents
            ],
            "limit_profile": manifest.limit_profile,
            "name": manifest.name,
            "origin_kind": manifest.origin_kind,
            "origin_profile": manifest.origin_profile,
            "portable_root_ref": manifest.portable_root_ref,
            "project_id": manifest.project_id,
            "schema": manifest.schema,
            "source_locale": manifest.source_locale,
            "target_locale": manifest.target_locale,
            "workspace_content_digest": manifest.workspace_content_digest,
        }
    )


def _decode_manifest(payload: bytes) -> ProjectPackageManifest:
    if len(payload) > MAX_PACKAGE_MANIFEST_BYTES:
        _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
    item = _require_keys(
        _decode_canonical_json(payload, manifest=True),
        frozenset(
            {
                "schema",
                "carrier_profile",
                "limit_profile",
                "project_id",
                "workspace_content_digest",
                "name",
                "source_locale",
                "target_locale",
                "origin_kind",
                "origin_profile",
                "portable_root_ref",
                "documents",
            }
        ),
        manifest=True,
    )
    documents = item["documents"]
    if type(documents) is not list:
        _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
    entries: list[ProjectPackageDocumentEntry] = []
    for raw_entry in documents:
        value = _require_keys(
            raw_entry,
            frozenset(
                {
                    "document_id",
                    "order",
                    "source_ref",
                    "display_name",
                    "document_member",
                    "source_member",
                    "codec_private_member",
                }
            ),
            manifest=True,
        )
        private = value["codec_private_member"]
        entries.append(
            ProjectPackageDocumentEntry(
                document_id=value["document_id"],
                order=value["order"],
                source_ref=value["source_ref"],
                display_name=value["display_name"],
                document_member=_member_ref(value["document_member"], manifest=True),
                source_member=_member_ref(value["source_member"], manifest=True),
                codec_private_member=(
                    None if private is None else _member_ref(private, manifest=True)
                ),
            )
        )
    return ProjectPackageManifest(
        schema=item["schema"],
        carrier_profile=item["carrier_profile"],
        limit_profile=item["limit_profile"],
        project_id=item["project_id"],
        workspace_content_digest=item["workspace_content_digest"],
        name=item["name"],
        source_locale=item["source_locale"],
        target_locale=item["target_locale"],
        origin_kind=item["origin_kind"],
        origin_profile=item["origin_profile"],
        portable_root_ref=item["portable_root_ref"],
        documents=tuple(entries),
    )


@dataclass(frozen=True, slots=True)
class OpenedProjectPackage:
    path: Path
    workspace: ProjectWorkspace
    manifest: ProjectPackageManifest
    validation: ProjectPackageValidationReport
    _file_facts: _FileFacts
    _entries: tuple[_RawEntry, ...]
    _member_digests: tuple[ProjectPackageMemberDigest, ...]

    @property
    def persistence_binding(self) -> ProjectPackagePersistenceBinding:
        return ProjectPackagePersistenceBinding(
            path=self.path,
            project_id=self.workspace.project_id,
            artifact_digest=self.validation.artifact_digest,
            workspace_content_digest=self.validation.workspace_content_digest,
            device=self._file_facts.device,
            inode=self._file_facts.inode,
            byte_count=self._file_facts.size,
            mtime_ns=self._file_facts.mtime_ns,
        )

    def create_workspace_service(
        self,
        *,
        session_id: str,
        revision: int,
    ) -> ProjectWorkspaceService:
        """Open an editable package session without inventing source-origin authority."""

        return ProjectWorkspaceService(
            self.workspace,
            None,
            session_id=session_id,
            revision=revision,
        )

    def create_save_service(
        self,
        *,
        session_id: str,
        revision: int,
    ) -> ProjectSaveService:
        workspace_service = self.create_workspace_service(
            session_id=session_id,
            revision=revision,
        )
        return ProjectSaveService(
            workspace_service,
            baseline=WorkspaceSaveBaseline.from_workspace(
                self.workspace,
                workspace_revision=revision,
                saved_package_digest=self.validation.workspace_content_digest,
            ),
        )

    def codec_availability(
        self,
        available_codec_identities: tuple[CodecIdentity, ...],
    ) -> tuple[ProjectPackageCodecAvailability, ...]:
        """Project live codec availability without importing a registry.

        Opening, editing, and package persistence never depend on this
        projection. A caller may use it to explain why source write-back or
        codec-private interpretation is unavailable.
        """

        if type(available_codec_identities) is not tuple or any(
            type(item) is not CodecIdentity for item in available_codec_identities
        ):
            raise TypeError("available codec identities must be an exact tuple")
        if len(available_codec_identities) != len(set(available_codec_identities)):
            raise TypeError("available codec identities must be unique")
        available = frozenset(available_codec_identities)
        return tuple(
            ProjectPackageCodecAvailability(
                document_id=document.document_id,
                codec_identity=document.codec_identity,
                available=document.codec_identity in available,
                safe_warnings=(
                    ()
                    if document.codec_identity in available
                    else ("PROJECT.PACKAGE.CODEC_UNAVAILABLE",)
                ),
            )
            for document in self.workspace.documents
        )

    def _member_reference(self, member_path: str) -> ProjectPackageMemberReference:
        if member_path == "manifest.json":
            digest = next(
                (item for item in self._member_digests if item.path == member_path),
                None,
            )
            if digest is None:
                _fail("PROJECT.PACKAGE.MEMBER_INVALID")
            return ProjectPackageMemberReference(
                digest.path,
                digest.sha256,
                digest.byte_count,
            )
        for document in self.manifest.documents:
            for reference in (
                document.document_member,
                document.source_member,
                document.codec_private_member,
            ):
                if reference is not None and reference.path == member_path:
                    return reference
        _fail("PROJECT.PACKAGE.MEMBER_INVALID")

    @contextmanager
    def _open_member_unchecked(
        self, member_path: str
    ) -> Iterator[_StoredMemberReader]:
        requested = _validate_physical_member_name(member_path)
        reference = self._member_reference(requested)
        descriptor, facts = _open_regular(self.path, include_digest=True)
        if facts != self._file_facts:
            os.close(descriptor)
            _fail("PROJECT.PACKAGE.SOURCE_STALE")
        entry = next((item for item in self._entries if item.name == requested), None)
        if entry is None or entry.byte_count != reference.byte_count:
            os.close(descriptor)
            _fail("PROJECT.PACKAGE.MEMBER_INVALID")
        reader = _StoredMemberReader(
            descriptor,
            entry,
            expected_digest=reference.sha256,
            file_facts=facts,
            path=self.path,
        )
        try:
            yield reader
        finally:
            reader.close()

    @contextmanager
    def open_member(
        self,
        member_path: str,
        *,
        codec_identity: CodecIdentity | None = None,
    ) -> Iterator[_StoredMemberReader]:
        requested = _validate_physical_member_name(member_path)
        private_manifest_document = next(
            (
                manifest_document
                for manifest_document in self.manifest.documents
                if manifest_document.codec_private_member is not None
                and manifest_document.codec_private_member.path == requested
            ),
            None,
        )
        if private_manifest_document is not None:
            private_document = next(
                document
                for document in self.workspace.documents
                if document.document_id == private_manifest_document.document_id
            )
            if (
                type(codec_identity) is not CodecIdentity
                or codec_identity != private_document.codec_identity
            ):
                _fail("PROJECT.PACKAGE.CODEC_UNAVAILABLE")
        elif codec_identity is not None and type(codec_identity) is not CodecIdentity:
            raise TypeError("codec identity must be exact or None")
        with self._open_member_unchecked(requested) as reader:
            yield reader

@dataclass(frozen=True, slots=True)
class ProjectPackageImportResult:
    """Cold-opened installed package paired with its durable public receipt."""

    installed: OpenedProjectPackage
    receipt: ProjectPackageImportReceipt

    def __post_init__(self) -> None:
        if (
            type(self.installed) is not OpenedProjectPackage
            or type(self.receipt) is not ProjectPackageImportReceipt
            or self.installed.workspace.project_id != self.receipt.project_id
            or self.installed.validation.artifact_digest
            != self.receipt.destination_after_digest
            or self.installed.validation.workspace_content_digest
            != self.receipt.workspace_content_digest
            or self.installed.validation.document_count
            != self.receipt.document_count
            or self.installed.validation.segment_count
            != self.receipt.segment_count
            or self.installed.validation.member_count
            != self.receipt.member_count
            or self.installed.validation.artifact_byte_count
            != self.receipt.byte_count
            or self.installed._member_digests != self.receipt.member_digests
            or tuple(
                document.document_id for document in self.installed.workspace.documents
            )
            != tuple(item.document_id for item in self.receipt.document_results)
        ):
            _fail("PROJECT.PACKAGE.MANIFEST_INVALID")


@dataclass(frozen=True, slots=True)
class PreparedProjectPackageImport:
    """Single-use, body-safe handle for one fully materialized import candidate.

    The package service retains the workspace, reconciliation and carrier
    authority privately.  Application callers may use this handle to prepare
    their own projections before the durable publication starts, then consume
    the same candidate through :meth:`commit_prepared_import`.
    """

    operation_id: str
    project_id: str
    mode: ProjectPackageImportMode
    workspace_revision: int
    workspace_content_digest: str

    def __post_init__(self) -> None:
        if not _operation_id(self.operation_id).startswith("package-import-"):
            _fail("PROJECT.PACKAGE.PREVIEW_STALE")
        validate_project_id(self.project_id)
        if type(self.mode) is not ProjectPackageImportMode:
            _fail("PROJECT.PACKAGE.PREVIEW_STALE")
        _exact_nonnegative_int(
            self.workspace_revision,
            code="PROJECT.PACKAGE.PREVIEW_STALE",
        )
        validate_sha256(self.workspace_content_digest)


def _validate_artifact(path: Path) -> OpenedProjectPackage:
    source_descriptor, facts = _open_regular(path, include_digest=True)
    sealed = tempfile.TemporaryFile(mode="w+b")
    descriptor = sealed.fileno()
    try:
        offset = 0
        while offset < facts.size:
            block = os.pread(
                source_descriptor,
                min(_COPY_BYTES, facts.size - offset),
                offset,
            )
            if not block:
                _fail("PROJECT.PACKAGE.SOURCE_STALE")
            written = 0
            while written < len(block):
                count = os.write(descriptor, block[written:])
                if count <= 0:
                    raise OSError("sealed package snapshot write failed")
                written += count
            offset += len(block)
        if _artifact_digest(descriptor, facts.size) != facts.digest:
            _fail("PROJECT.PACKAGE.SOURCE_STALE")
        entries = _parse_raw_zip(descriptor, facts.size)
        manifest_entry = entries[0]
        if manifest_entry.byte_count > MAX_PACKAGE_MANIFEST_BYTES:
            _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
        manifest_digest, manifest_payload = _read_entry(
            descriptor,
            manifest_entry,
            materialize=True,
        )
        assert manifest_payload is not None
        member_digests: list[ProjectPackageMemberDigest] = [
            ProjectPackageMemberDigest(
                manifest_entry.name,
                manifest_digest,
                manifest_entry.byte_count,
            )
        ]
        manifest = _decode_manifest(manifest_payload)
        expected_names = {"manifest.json"}
        for entry in manifest.documents:
            if entry.document_member.path != f"documents/{entry.document_id}.json":
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
            if entry.source_member.path != (
                f"sources/{entry.document_id}/{entry.source_member.sha256}.bin"
            ):
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
            if entry.codec_private_member is not None and entry.codec_private_member.path != (
                f"codec-private/{entry.document_id}/"
                f"{entry.codec_private_member.sha256}.bin"
            ):
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
            expected_names.add(entry.document_member.path)
            expected_names.add(entry.source_member.path)
            if entry.codec_private_member is not None:
                expected_names.add(entry.codec_private_member.path)
        observed_names = {entry.name for entry in entries}
        if expected_names != observed_names:
            _fail("PROJECT.PACKAGE.MEMBER_INVALID")

        entries_by_name = {entry.name: entry for entry in entries}
        documents: list[ProjectDocument] = []
        total_decoded = len(manifest_payload)
        opaque_count = 0
        for manifest_entry_value in manifest.documents:
            document_entry = entries_by_name[manifest_entry_value.document_member.path]
            if document_entry.byte_count > MAX_PACKAGE_DOCUMENT_MEMBER_BYTES:
                _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
            document_digest, document_payload = _read_entry(
                descriptor,
                document_entry,
                materialize=True,
            )
            assert document_payload is not None
            total_decoded += len(document_payload)
            if (
                document_digest != manifest_entry_value.document_member.sha256
                or len(document_payload)
                != manifest_entry_value.document_member.byte_count
            ):
                _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")
            document = _decode_document(document_payload)
            if (
                document.document_id != manifest_entry_value.document_id
                or document.order != manifest_entry_value.order
                or document.source_ref != manifest_entry_value.source_ref
                or document.display_name != manifest_entry_value.display_name
            ):
                _fail("PROJECT.PACKAGE.MANIFEST_INVALID")

            source_entry = entries_by_name[manifest_entry_value.source_member.path]
            source_digest, _ = _read_entry(
                descriptor,
                source_entry,
                materialize=False,
            )
            total_decoded += source_entry.byte_count
            if (
                source_digest != manifest_entry_value.source_member.sha256
                or source_entry.byte_count
                != manifest_entry_value.source_member.byte_count
                or source_digest != document.source_snapshot_digest
            ):
                _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")

            private_ref = manifest_entry_value.codec_private_member
            if private_ref is None:
                if document.codec_private_member is not None:
                    _fail("PROJECT.PACKAGE.MANIFEST_INVALID")
            else:
                private_entry = entries_by_name[private_ref.path]
                private_digest, _ = _read_entry(
                    descriptor,
                    private_entry,
                    materialize=False,
                )
                total_decoded += private_entry.byte_count
                opaque_count += 1
                if (
                    private_digest != private_ref.sha256
                    or private_entry.byte_count != private_ref.byte_count
                    or document.codec_private_member is None
                    or document.codec_private_member.member_path != private_ref.path
                    or document.codec_private_member.sha256 != private_ref.sha256
                    or document.codec_private_member.byte_count != private_ref.byte_count
                ):
                    _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")
            if total_decoded > MAX_PACKAGE_TOTAL_DECODED_BYTES:
                _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
            documents.append(document)
            member_digests.append(
                ProjectPackageMemberDigest(
                    document_entry.name,
                    document_digest,
                    document_entry.byte_count,
                )
            )
            member_digests.append(
                ProjectPackageMemberDigest(
                    source_entry.name,
                    source_digest,
                    source_entry.byte_count,
                )
            )
            if private_ref is not None:
                member_digests.append(
                    ProjectPackageMemberDigest(
                        private_entry.name,
                        private_digest,
                        private_entry.byte_count,
                    )
                )

        try:
            workspace = ProjectWorkspace(
                schema_version=1,
                project_id=manifest.project_id,
                name=manifest.name,
                source_locale=manifest.source_locale,
                target_locale=manifest.target_locale,
                origin=ProjectOrigin(
                    kind=ProjectOriginKind(manifest.origin_kind),
                    profile_version=manifest.origin_profile,
                    portable_root_ref=manifest.portable_root_ref,
                ),
                persistence_kind=ProjectPersistenceKind.PROJECT_PACKAGE,
                documents=tuple(documents),
            )
        except (TypeError, ValueError, ProjectWorkspaceError) as error:
            raise ProjectWorkspaceError("PROJECT.PACKAGE.MANIFEST_INVALID") from error
        workspace_digest = workspace_content_digest_v1(workspace)
        if workspace_digest != manifest.workspace_content_digest:
            _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")
        # The artifact digest and all semantic members must come from one
        # retained regular-file generation.  Re-check after the final member
        # read so an equal-size in-place rewrite cannot bind A's digest to B's
        # workspace facts.
        _require_descriptor_snapshot(source_descriptor, facts)
        _require_path_snapshot(path, facts)
        segment_count = sum(len(document.source_segments) for document in documents)
        report = ProjectPackageValidationReport(
            artifact_digest=facts.digest,
            workspace_content_digest=workspace_digest,
            carrier_profile=PROJECT_PACKAGE_CARRIER_PROFILE,
            manifest_schema=PROJECT_PACKAGE_MANIFEST_SCHEMA,
            project_id=workspace.project_id,
            document_count=len(documents),
            segment_count=segment_count,
            member_count=len(entries),
            opaque_member_count=opaque_count,
            editing_state_count=sum(len(item.editing_overlay) for item in documents),
            artifact_byte_count=facts.size,
            decoded_member_byte_count=total_decoded,
        )
        return OpenedProjectPackage(
            path,
            workspace,
            manifest,
            report,
            facts,
            entries,
            tuple(sorted(member_digests, key=lambda item: _member_order_key(item.path))),
        )
    finally:
        sealed.close()
        os.close(source_descriptor)


def _open_bound_source(binding: OriginBinding, document: ProjectDocument) -> BinaryIO:
    bound = next(
        (item for item in binding.documents if item.document_id == document.document_id),
        None,
    )
    if bound is None or bound.source_ref != document.source_ref:
        _fail("PROJECT.PACKAGE.SOURCE_STALE")
    root_path = Path(binding.absolute_root)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    try:
        root_descriptor = os.open(root_path, flags)
    except OSError as error:
        raise ProjectWorkspaceError("PROJECT.PACKAGE.SOURCE_UNSAFE") from error
    current = root_descriptor
    try:
        root_status = os.fstat(root_descriptor)
        if (root_status.st_dev, root_status.st_ino) != (
            binding.root_device,
            binding.root_inode,
        ):
            _fail("PROJECT.PACKAGE.SOURCE_STALE")
        parts = document.source_ref.split("/")
        for component in parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                dir_fd=current,
            )
            if current != root_descriptor:
                os.close(current)
            current = child
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=current,
        )
        status = os.fstat(descriptor)
        expected = bound.source_identity
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or f"{status.st_dev}:{status.st_ino}" != expected.regular_file_identity
            or status.st_size != expected.original_size
            or status.st_mtime_ns != expected.original_mtime_ns
        ):
            os.close(descriptor)
            _fail("PROJECT.PACKAGE.SOURCE_STALE")
        return os.fdopen(descriptor, "rb", closefd=True)
    except ProjectWorkspaceError:
        raise
    except OSError as error:
        raise ProjectWorkspaceError("PROJECT.PACKAGE.SOURCE_UNSAFE") from error
    finally:
        if current != root_descriptor:
            os.close(current)
        os.close(root_descriptor)


def _source_blob(binding: OriginBinding, document: ProjectDocument) -> _Blob:
    bound = next(
        (item for item in binding.documents if item.document_id == document.document_id),
        None,
    )
    if bound is None:
        _fail("PROJECT.PACKAGE.SOURCE_STALE")
    identity = bound.source_identity
    path = f"sources/{document.document_id}/{identity.content_sha256}.bin"
    return _Blob(
        path,
        identity.content_sha256,
        identity.byte_count,
        lambda: _open_bound_source(binding, document),
    )


def _bytes_blob(member_path: str, payload: bytes) -> _Blob:
    digest = hashlib.sha256(payload).hexdigest()
    return _Blob(member_path, digest, len(payload), lambda: io.BytesIO(payload))


def _package_member_blob(
    opened: OpenedProjectPackage,
    reference: ProjectPackageMemberReference,
) -> _Blob:
    def open_source() -> BinaryIO:
        manager = opened._open_member_unchecked(reference.path)
        reader = manager.__enter__()

        class _ManagedReader:
            __slots__ = ("_reader", "_manager")

            def __init__(self, value: BinaryIO, context: object) -> None:
                self._reader = value
                self._manager = context

            def read(self, size: int = -1) -> bytes:
                return self._reader.read(size)

            def close(self) -> None:
                self._manager.__exit__(None, None, None)

        return _ManagedReader(reader, manager)  # type: ignore[return-value]

    return _Blob(reference.path, reference.sha256, reference.byte_count, open_source)


def _zip_info(member_path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(member_path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 10
    info.flag_bits = 0
    info.internal_attr = 0
    info.external_attr = _REGULAR_0644_EXTERNAL
    info.extra = b""
    info.comment = b""
    return info


def _write_blob(archive: zipfile.ZipFile, blob: _Blob) -> None:
    with blob.open() as source, archive.open(
        _zip_info(blob.member_path),
        "w",
        force_zip64=False,
    ) as destination:
        digest = hashlib.sha256()
        count = 0
        while True:
            block = source.read(_COPY_BYTES)
            if not block:
                break
            if type(block) is not bytes:
                raise TypeError("blob reader must return exact bytes")
            count += len(block)
            if count > blob.byte_count or count > MAX_PACKAGE_TOTAL_DECODED_BYTES:
                _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
            digest.update(block)
            destination.write(block)
        if count != blob.byte_count or digest.hexdigest() != blob.sha256:
            _fail("PROJECT.PACKAGE.SOURCE_STALE")


def _build_package_candidate(
    path: Path,
    workspace: ProjectWorkspace,
    origin_binding: OriginBinding | None,
    private_sources: tuple[ProjectPackageBlobSource, ...],
    *,
    last_known_good_package: OpenedProjectPackage | None = None,
    additional_package_sources: tuple[OpenedProjectPackage, ...] = (),
    destination_parent_facts: _ParentFacts | None = None,
) -> ProjectPackageValidationReport:
    if type(workspace) is not ProjectWorkspace:
        raise TypeError("package candidate requires exact workspace")
    if origin_binding is not None and type(origin_binding) is not OriginBinding:
        raise TypeError("origin binding must be exact or None")
    if origin_binding is not None and workspace.project_id != origin_binding.project_id:
        _fail("PROJECT.PACKAGE.SOURCE_STALE")
    if last_known_good_package is not None:
        if (
            type(last_known_good_package) is not OpenedProjectPackage
            or last_known_good_package.workspace.project_id != workspace.project_id
        ):
            _fail("PROJECT.PACKAGE.SOURCE_STALE")
    if type(additional_package_sources) is not tuple or any(
        type(item) is not OpenedProjectPackage for item in additional_package_sources
    ):
        raise TypeError("additional package sources must be exact opened packages")
    package_sources = tuple(
        item
        for item in (last_known_good_package, *additional_package_sources)
        if item is not None
    )

    def package_document_source(
        document: ProjectDocument,
    ) -> tuple[OpenedProjectPackage, ProjectDocument, ProjectPackageDocumentEntry] | None:
        for opened in package_sources:
            source_document = next(
                (
                    item
                    for item in opened.workspace.documents
                    if item.document_id == document.document_id
                ),
                None,
            )
            source_entry = next(
                (
                    item
                    for item in opened.manifest.documents
                    if item.document_id == document.document_id
                ),
                None,
            )
            if (
                source_document is not None
                and source_entry is not None
                and source_document.source_snapshot_digest
                == document.source_snapshot_digest
            ):
                return opened, source_document, source_entry
        return None

    def package_document_private(
        document: ProjectDocument,
    ) -> tuple[OpenedProjectPackage, ProjectPackageDocumentEntry] | None:
        expected = document.codec_private_member
        if expected is None:
            return None
        for opened in package_sources:
            source_document = next(
                (
                    item
                    for item in opened.workspace.documents
                    if item.document_id == document.document_id
                ),
                None,
            )
            source_entry = next(
                (
                    item
                    for item in opened.manifest.documents
                    if item.document_id == document.document_id
                ),
                None,
            )
            if (
                source_document is not None
                and source_entry is not None
                and source_document.codec_identity == document.codec_identity
                and source_document.codec_private_member == expected
                and source_entry.codec_private_member is not None
            ):
                return opened, source_entry
        return None
    private_by_document = {item.document_id: item for item in private_sources}
    if len(private_by_document) != len(private_sources):
        _fail("PROJECT.PACKAGE.MEMBER_INVALID")
    document_blobs: list[_Blob] = []
    source_blobs: list[_Blob] = []
    private_blobs: list[_Blob] = []
    manifest_entries: list[ProjectPackageDocumentEntry] = []
    for document in workspace.documents:
        document_payload = _document_json(document)
        if len(document_payload) > MAX_PACKAGE_DOCUMENT_MEMBER_BYTES:
            _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
        document_blob = _bytes_blob(f"documents/{document.document_id}.json", document_payload)
        package_source = package_document_source(document)
        if package_source is not None:
            source_package, lkg_document, lkg_entry = package_source
            source_blob = _package_member_blob(
                source_package,
                lkg_entry.source_member,
            )
        elif origin_binding is not None:
            source_blob = _source_blob(origin_binding, document)
        else:
            _fail("PROJECT.PACKAGE.SOURCE_STALE")
        private_reference = None
        if document.codec_private_member is not None:
            supplied = private_by_document.get(document.document_id)
            expected = document.codec_private_member
            if supplied is not None:
                private_blob = supplied._blob()
            else:
                package_private = package_document_private(document)
                if package_private is None:
                    _fail("PROJECT.PACKAGE.MEMBER_INVALID")
                private_package, private_entry = package_private
                assert private_entry.codec_private_member is not None
                private_blob = _package_member_blob(
                    private_package,
                    private_entry.codec_private_member,
                )
            if (
                private_blob.member_path != expected.member_path
                or private_blob.sha256 != expected.sha256
                or private_blob.byte_count != expected.byte_count
            ):
                _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")
            private_blobs.append(private_blob)
            private_reference = ProjectPackageMemberReference(
                private_blob.member_path,
                private_blob.sha256,
                private_blob.byte_count,
            )
        elif document.document_id in private_by_document:
            _fail("PROJECT.PACKAGE.MEMBER_INVALID")
        document_blobs.append(document_blob)
        source_blobs.append(source_blob)
        manifest_entries.append(
            ProjectPackageDocumentEntry(
                document_id=document.document_id,
                order=document.order,
                source_ref=document.source_ref,
                display_name=document.display_name,
                document_member=ProjectPackageMemberReference(
                    document_blob.member_path,
                    document_blob.sha256,
                    document_blob.byte_count,
                ),
                source_member=ProjectPackageMemberReference(
                    source_blob.member_path,
                    source_blob.sha256,
                    source_blob.byte_count,
                ),
                codec_private_member=private_reference,
            )
        )
    if set(private_by_document) - {
        item.document_id
        for item in workspace.documents
        if item.codec_private_member is not None
    }:
        _fail("PROJECT.PACKAGE.MEMBER_INVALID")
    manifest = ProjectPackageManifest(
        schema=PROJECT_PACKAGE_MANIFEST_SCHEMA,
        carrier_profile=PROJECT_PACKAGE_CARRIER_PROFILE,
        limit_profile=PROJECT_LIMIT_PROFILE_ID,
        project_id=workspace.project_id,
        workspace_content_digest=workspace_content_digest_v1(workspace),
        name=workspace.name,
        source_locale=workspace.source_locale,
        target_locale=workspace.target_locale,
        origin_kind=workspace.origin.kind.value,
        origin_profile=workspace.origin.profile_version,
        portable_root_ref=workspace.origin.portable_root_ref,
        documents=tuple(manifest_entries),
    )
    manifest_blob = _bytes_blob("manifest.json", _manifest_json(manifest))
    blobs = tuple(
        sorted(
            (manifest_blob, *document_blobs, *source_blobs, *private_blobs),
            key=lambda item: _member_order_key(item.member_path),
        )
    )
    if len(blobs) > MAX_PACKAGE_MEMBERS:
        _fail("PROJECT.PACKAGE.LIMIT_EXCEEDED")
    facts = destination_parent_facts or _bind_parent(path)
    with _bound_parent_descriptor(path, facts) as parent:
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "w+b", closefd=False) as output:
                with zipfile.ZipFile(
                    output,
                    mode="w",
                    compression=zipfile.ZIP_STORED,
                    allowZip64=False,
                    strict_timestamps=True,
                ) as archive:
                    archive.comment = b""
                    for blob in blobs:
                        _write_blob(archive, blob)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            try:
                os.unlink(path.name, dir_fd=parent)
                os.fsync(parent)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        os.fsync(parent)
    _require_parent(path, facts)
    return _validate_artifact(path).validation


def _copy_regular(
    source: Path,
    destination: Path,
    *,
    destination_parent_facts: _ParentFacts | None = None,
) -> _FileFacts:
    source_descriptor, source_facts = _open_regular(source, include_digest=True)
    destination_descriptor = -1
    parent_facts = destination_parent_facts or _bind_parent(destination)
    try:
        with _bound_parent_descriptor(destination, parent_facts) as parent:
            destination_descriptor = os.open(
                destination.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            offset = 0
            digest = hashlib.sha256()
            while offset < source_facts.size:
                block = os.pread(
                    source_descriptor,
                    min(_COPY_BYTES, source_facts.size - offset),
                    offset,
                )
                if not block:
                    _fail("PROJECT.PACKAGE.SOURCE_STALE")
                written = 0
                while written < len(block):
                    count = os.write(destination_descriptor, block[written:])
                    if count <= 0:
                        raise OSError("package copy write failed")
                    written += count
                digest.update(block)
                offset += len(block)
            if digest.hexdigest() != source_facts.digest:
                _fail("PROJECT.PACKAGE.SOURCE_STALE")
            os.fsync(destination_descriptor)
            os.fsync(parent)
    except BaseException:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
            destination_descriptor = -1
        try:
            _unlink_in_bound_parent(destination, parent_facts, missing_ok=True)
        except (OSError, ProjectWorkspaceError):
            pass
        raise
    finally:
        os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    copied_descriptor, copied = _open_regular(destination, include_digest=True)
    os.close(copied_descriptor)
    if copied.size != source_facts.size or copied.digest != source_facts.digest:
        _fail("PROJECT.PACKAGE.DIGEST_MISMATCH")
    return copied


def _port_validate_artifact(path: Path) -> OpenedProjectPackage:
    try:
        return _validate_artifact(path)
    except ProjectWorkspaceError as error:
        raise OSError("package artifact validation failed") from error


def _port_copy_regular(
    source: Path,
    destination: Path,
    *,
    destination_parent_facts: _ParentFacts | None = None,
) -> _FileFacts:
    try:
        return _copy_regular(
            source,
            destination,
            destination_parent_facts=destination_parent_facts,
        )
    except ProjectWorkspaceError as error:
        raise OSError("package artifact copy failed") from error


@dataclass(slots=True)
class _PackageCandidate:
    operation_id: str
    target: Path
    candidate_path: Path
    lkg_path: Path
    journal_path: Path
    project_id: str
    candidate_workspace: ProjectWorkspace | None
    candidate_workspace_digest: str
    last_known_good_workspace: ProjectWorkspace | None
    candidate_artifact_digest: str | None
    last_known_good_artifact_digest: str | None
    requested_document_ids: tuple[str, ...]
    phase: RecoveryPhase
    parent_device: int
    parent_inode: int


def _journal_payload(handle: _PackageCandidate) -> bytes:
    return _canonical_json(
        {
            "candidate_artifact_digest": handle.candidate_artifact_digest,
            "candidate_workspace_digest": handle.candidate_workspace_digest,
            "last_known_good_artifact_digest": (
                handle.last_known_good_artifact_digest
            ),
            "last_known_good_workspace_digest": (
                None
                if handle.last_known_good_workspace is None
                else workspace_content_digest_v1(handle.last_known_good_workspace)
            ),
            "operation_id": handle.operation_id,
            "phase": handle.phase.value,
            "project_id": handle.project_id,
            "parent_device": handle.parent_device,
            "parent_inode": handle.parent_inode,
            "schema": "localcat-project-package-journal-v1",
        }
    )


def _write_journal(handle: _PackageCandidate) -> None:
    temporary = handle.journal_path.with_name(
        handle.journal_path.name + "." + secrets.token_hex(16) + ".tmp"
    )
    descriptor = -1
    parent_facts = _ParentFacts(handle.parent_device, handle.parent_inode)
    with _bound_parent_descriptor(handle.journal_path, parent_facts) as parent:
        try:
            descriptor = os.open(
                temporary.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            payload = _journal_payload(handle)
            offset = 0
            while offset < len(payload):
                count = os.write(descriptor, payload[offset:])
                if count <= 0:
                    raise OSError("journal write failed")
                offset += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.rename(
                temporary.name,
                handle.journal_path.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            os.fsync(parent)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary.name, dir_fd=parent)
            except OSError:
                pass
            try:
                os.fsync(parent)
            except OSError:
                pass
            raise


class _ProjectPackagePersistencePort:
    """C2C adapter for the carrier-neutral C2B save state machine."""

    def __init__(
        self,
        target: Path,
        origin_binding: OriginBinding | None,
        private_sources: tuple[ProjectPackageBlobSource, ...],
        *,
        persistence_binding: ProjectPackagePersistenceBinding | None = None,
        additional_package_sources: tuple[OpenedProjectPackage, ...] = (),
        allow_cross_project_lkg: bool = False,
    ) -> None:
        if not isinstance(target, Path) or not target.is_absolute() or not target.name:
            _fail("PROJECT.PACKAGE.SOURCE_UNSAFE")
        if origin_binding is not None and type(origin_binding) is not OriginBinding:
            raise TypeError("package port origin binding must be exact or None")
        if type(private_sources) is not tuple or any(
            type(item) is not ProjectPackageBlobSource for item in private_sources
        ):
            raise TypeError("private sources must be an exact tuple")
        if persistence_binding is not None and type(
            persistence_binding
        ) is not ProjectPackagePersistenceBinding:
            raise TypeError("persistence binding must be exact or None")
        if type(additional_package_sources) is not tuple or any(
            type(item) is not OpenedProjectPackage
            for item in additional_package_sources
        ):
            raise TypeError("additional package sources must be exact opened packages")
        if type(allow_cross_project_lkg) is not bool:
            raise TypeError("cross-project LKG flag must be exact bool")
        self._target = _canonical_user_path(target)
        self._parent_facts = _bind_parent(self._target)
        self._binding = origin_binding
        self._private_sources = private_sources
        self._persistence_binding = persistence_binding
        self._additional_package_sources = additional_package_sources
        self._allow_cross_project_lkg = allow_cross_project_lkg
        self._journal = self._target.with_name(
            f".{self._target.name}.localcat-save-journal-v1"
        )
        self._handles: dict[str, _PackageCandidate] = {}
        self.destination_before_digest: str | None = None
        self.committed_opened: OpenedProjectPackage | None = None

    def _paths(self, operation_id: str) -> tuple[Path, Path]:
        return (
            self._target.with_name(f".{self._target.name}.{operation_id}.candidate"),
            self._target.with_name(f".{self._target.name}.{operation_id}.lkg"),
        )

    def _require_bound_parent(self) -> None:
        try:
            _require_parent(self._target, self._parent_facts)
        except ProjectWorkspaceError as error:
            raise OSError("package destination parent changed") from error

    def _require(self, value: object) -> _PackageCandidate:
        if type(value) is not _PackageCandidate:
            raise OSError("invalid package candidate")
        handle = self._handles.get(value.operation_id)
        if handle is not value:
            raise OSError("foreign package candidate")
        return value

    def _clean_path(self, path: Path) -> None:
        _unlink_in_bound_parent(path, self._parent_facts, missing_ok=True)

    def _clean_residue(self, handle: _PackageCandidate) -> None:
        self._require_bound_parent()
        self._clean_path(handle.candidate_path)
        self._clean_path(handle.lkg_path)
        self._clean_path(handle.journal_path)

    def stage_candidate(
        self,
        *,
        operation_id: str,
        candidate_workspace: ProjectWorkspace,
        last_known_good_workspace: ProjectWorkspace | None,
        requested_document_ids: tuple[str, ...],
    ) -> object:
        _operation_id(operation_id)
        self._require_bound_parent()
        if self._journal.exists():
            raise OSError("pending recovery exists")
        if (
            self._binding is not None
            and candidate_workspace.project_id != self._binding.project_id
        ):
            raise OSError("candidate project does not match origin binding")
        lkg_artifact_digest = None
        opened_lkg = None
        if last_known_good_workspace is None:
            if self._target.exists():
                raise OSError("destination exists without a durable baseline")
        else:
            if not self._target.exists():
                raise OSError("durable baseline destination is missing")
            opened_lkg = _port_validate_artifact(self._target)
            if (
                opened_lkg.workspace != last_known_good_workspace
                or (
                    not self._allow_cross_project_lkg
                    and opened_lkg.workspace.project_id
                    != candidate_workspace.project_id
                )
            ):
                raise OSError("durable baseline destination is stale")
            if (
                self._persistence_binding is None
                or opened_lkg.persistence_binding != self._persistence_binding
            ):
                raise OSError("durable package binding is stale")
            lkg_artifact_digest = opened_lkg.validation.artifact_digest
        self.destination_before_digest = lkg_artifact_digest
        candidate_path, lkg_path = self._paths(operation_id)
        handle = _PackageCandidate(
            operation_id=operation_id,
            target=self._target,
            candidate_path=candidate_path,
            lkg_path=lkg_path,
            journal_path=self._journal,
            project_id=candidate_workspace.project_id,
            candidate_workspace=candidate_workspace,
            candidate_workspace_digest=workspace_content_digest_v1(
                candidate_workspace
            ),
            last_known_good_workspace=last_known_good_workspace,
            candidate_artifact_digest=None,
            last_known_good_artifact_digest=lkg_artifact_digest,
            requested_document_ids=requested_document_ids,
            phase=RecoveryPhase.STAGING,
            parent_device=self._parent_facts.device,
            parent_inode=self._parent_facts.inode,
        )
        self._handles[operation_id] = handle
        try:
            _write_journal(handle)
            validation = _build_package_candidate(
                candidate_path,
                candidate_workspace,
                self._binding,
                self._private_sources,
                last_known_good_package=(
                    opened_lkg
                    if opened_lkg is None
                    or opened_lkg.workspace.project_id
                    == candidate_workspace.project_id
                    else None
                ),
                additional_package_sources=self._additional_package_sources,
                destination_parent_facts=self._parent_facts,
            )
            handle.candidate_artifact_digest = validation.artifact_digest
            handle.phase = RecoveryPhase.STAGED
            _write_journal(handle)
        except ProjectWorkspaceError as error:
            raise OSError("package candidate staging failed") from error
        return handle

    def validate_candidate(self, candidate_handle: object) -> None:
        self._require_bound_parent()
        handle = self._require(candidate_handle)
        if (
            handle.phase is not RecoveryPhase.STAGED
            or handle.candidate_workspace is None
            or handle.candidate_artifact_digest is None
        ):
            raise OSError("candidate phase is invalid")
        opened = _port_validate_artifact(handle.candidate_path)
        if (
            opened.workspace != handle.candidate_workspace
            or opened.validation.artifact_digest != handle.candidate_artifact_digest
        ):
            raise OSError("candidate cold validation mismatch")

    def arm_publication(self, candidate_handle: object) -> None:
        self._require_bound_parent()
        handle = self._require(candidate_handle)
        if handle.phase is not RecoveryPhase.STAGED:
            raise OSError("candidate phase is invalid")
        if handle.last_known_good_workspace is not None:
            copied = _port_copy_regular(
                handle.target,
                handle.lkg_path,
                destination_parent_facts=self._parent_facts,
            )
            if copied.digest != handle.last_known_good_artifact_digest:
                raise OSError("last known good copy mismatch")
        handle.phase = RecoveryPhase.ARMED
        _write_journal(handle)

    def publish_candidate(self, candidate_handle: object) -> None:
        self._require_bound_parent()
        handle = self._require(candidate_handle)
        if handle.phase is not RecoveryPhase.ARMED:
            raise OSError("candidate phase is invalid")
        if handle.last_known_good_workspace is None:
            if handle.target.exists():
                raise OSError("destination appeared after publication arm")
        else:
            opened = _port_validate_artifact(handle.target)
            if (
                opened.validation.artifact_digest
                != handle.last_known_good_artifact_digest
                or self._persistence_binding is None
                or opened.persistence_binding != self._persistence_binding
            ):
                raise OSError("destination changed after publication arm")
        handle.phase = RecoveryPhase.PUBLISHING
        _write_journal(handle)
        _replace_in_bound_parent(
            handle.candidate_path,
            handle.target,
            self._parent_facts,
        )
        self._require_bound_parent()
        handle.phase = RecoveryPhase.PUBLISHED
        _write_journal(handle)

    def readback_candidate(self, candidate_handle: object) -> ProjectWorkspace:
        self._require_bound_parent()
        handle = self._require(candidate_handle)
        if handle.candidate_artifact_digest is None:
            raise OSError("candidate artifact is not complete")
        if handle.phase in {RecoveryPhase.PUBLISHED, RecoveryPhase.COMMIT_UNCERTAIN}:
            source = handle.target
        elif handle.phase is RecoveryPhase.PUBLISHING:
            source = self._candidate_source_for_publishing(handle)
        else:
            source = handle.candidate_path
        opened = _port_validate_artifact(source)
        if opened.validation.artifact_digest != handle.candidate_artifact_digest:
            raise OSError("candidate artifact readback mismatch")
        if source == handle.target:
            self.committed_opened = opened
        return opened.workspace

    def _candidate_source_for_publishing(self, handle: _PackageCandidate) -> Path:
        expected = handle.candidate_artifact_digest
        if expected is None:
            raise OSError("candidate artifact is not complete")
        for path in (handle.target, handle.candidate_path):
            try:
                opened = _port_validate_artifact(path)
            except OSError:
                continue
            if opened.validation.artifact_digest == expected:
                return path
        raise OSError("publishing candidate cannot be independently proved")

    def commit_candidate(self, candidate_handle: object) -> None:
        self._require_bound_parent()
        handle = self._require(candidate_handle)
        if handle.phase is not RecoveryPhase.PUBLISHED:
            raise OSError("candidate phase is invalid")
        handle.phase = RecoveryPhase.COMMIT_UNCERTAIN
        _write_journal(handle)
        opened = _port_validate_artifact(handle.target)
        if opened.validation.artifact_digest != handle.candidate_artifact_digest:
            raise OSError("published candidate changed before commit")
        self._clean_residue(handle)

    def rollback_candidate(
        self, candidate_handle: object
    ) -> ProjectWorkspace | None:
        self._require_bound_parent()
        handle = self._require(candidate_handle)
        if handle.phase in {
            RecoveryPhase.PUBLISHING,
            RecoveryPhase.PUBLISHED,
            RecoveryPhase.COMMIT_UNCERTAIN,
        }:
            target_is_candidate = False
            if handle.target.exists() and handle.candidate_artifact_digest is not None:
                try:
                    target_is_candidate = (
                        _port_validate_artifact(handle.target).validation.artifact_digest
                        == handle.candidate_artifact_digest
                    )
                except OSError as error:
                    raise OSError(
                        "rollback target cannot be independently identified"
                    ) from error
            if handle.last_known_good_workspace is None:
                if target_is_candidate:
                    _unlink_in_bound_parent(handle.target, self._parent_facts)
            else:
                if target_is_candidate:
                    _replace_in_bound_parent(
                        handle.lkg_path,
                        handle.target,
                        self._parent_facts,
                    )
        observed = handle.last_known_good_workspace
        if observed is None:
            if handle.target.exists():
                raise OSError("rollback failed to restore absent destination")
        else:
            opened = _port_validate_artifact(handle.target)
            if opened.workspace != observed:
                raise OSError("rollback readback mismatch")
        self._clean_residue(handle)
        return observed

    def inspect_pending_recovery(self) -> object | None:
        self._require_bound_parent()
        if not self._journal.exists():
            return None
        try:
            descriptor, facts = _open_regular(self._journal, include_digest=False)
        except ProjectWorkspaceError as error:
            raise OSError("package recovery journal is unsafe") from error
        try:
            if facts.size > MAX_PACKAGE_MANIFEST_BYTES:
                raise OSError("journal is oversized")
            payload = os.pread(descriptor, facts.size, 0)
        finally:
            os.close(descriptor)
        try:
            value = _require_keys(
                _decode_canonical_json(payload, manifest=True),
                frozenset(
                    {
                        "schema",
                        "operation_id",
                        "project_id",
                        "phase",
                        "candidate_artifact_digest",
                        "candidate_workspace_digest",
                        "last_known_good_artifact_digest",
                        "last_known_good_workspace_digest",
                        "parent_device",
                        "parent_inode",
                    }
                ),
                manifest=True,
            )
            operation_id = _operation_id(value["operation_id"])
            phase = RecoveryPhase(value["phase"])
        except (TypeError, ValueError, ProjectWorkspaceError) as error:
            raise OSError("package recovery journal is invalid") from error
        existing = self._handles.get(operation_id)
        if existing is not None:
            if existing.phase is phase:
                return existing
            self._handles.pop(operation_id, None)
        candidate_path, lkg_path = self._paths(operation_id)
        candidate_workspace_digest = value["candidate_workspace_digest"]
        candidate_artifact_digest = value["candidate_artifact_digest"]
        try:
            validate_project_id(value["project_id"])
            validate_sha256(candidate_workspace_digest)
            _exact_nonnegative_int(
                value["parent_device"], code="PROJECT.PACKAGE.MANIFEST_INVALID"
            )
            _exact_nonnegative_int(
                value["parent_inode"], code="PROJECT.PACKAGE.MANIFEST_INVALID"
            )
            if candidate_artifact_digest is not None:
                validate_sha256(candidate_artifact_digest)
            if value["last_known_good_workspace_digest"] is not None:
                validate_sha256(value["last_known_good_workspace_digest"])
        except (TypeError, ValueError, ProjectWorkspaceError) as error:
            raise OSError("package recovery journal is invalid") from error
        if phase is RecoveryPhase.STAGING:
            if candidate_artifact_digest is not None:
                raise OSError("staging journal unexpectedly names an artifact")
            candidate = None
        else:
            if candidate_artifact_digest is None:
                raise OSError("complete candidate digest is missing")
            if phase in {RecoveryPhase.PUBLISHED, RecoveryPhase.COMMIT_UNCERTAIN}:
                candidate_source = self._target
            elif phase is RecoveryPhase.PUBLISHING:
                candidates: list[OpenedProjectPackage] = []
                for path in (self._target, candidate_path):
                    try:
                        observed = _port_validate_artifact(path)
                    except OSError:
                        continue
                    if observed.validation.artifact_digest == candidate_artifact_digest:
                        candidates.append(observed)
                if len(candidates) != 1:
                    raise OSError("publishing candidate cannot be uniquely proved")
                candidate = candidates[0]
                candidate_source = candidate.path
            else:
                candidate_source = candidate_path
            if phase is not RecoveryPhase.PUBLISHING:
                try:
                    candidate = _port_validate_artifact(candidate_source)
                except OSError as error:
                    raise OSError("package recovery candidate is invalid") from error
            if (
                candidate.validation.artifact_digest != candidate_artifact_digest
                or workspace_content_digest_v1(candidate.workspace)
                != candidate_workspace_digest
                or candidate.workspace.project_id != value["project_id"]
            ):
                raise OSError("package recovery candidate mismatch")
        lkg_workspace = None
        lkg_cleanup_missing = False
        lkg_artifact_digest = value["last_known_good_artifact_digest"]
        if lkg_artifact_digest is not None:
            try:
                validate_sha256(lkg_artifact_digest)
            except (TypeError, ValueError, ProjectWorkspaceError) as error:
                raise OSError("package recovery journal is invalid") from error
            lkg_opened = None
            for lkg_source in (lkg_path, self._target):
                try:
                    observed_lkg = _port_validate_artifact(lkg_source)
                except OSError:
                    continue
                if observed_lkg.validation.artifact_digest == lkg_artifact_digest:
                    lkg_opened = observed_lkg
                    break
            if lkg_opened is None:
                if phase is not RecoveryPhase.COMMIT_UNCERTAIN:
                    raise OSError("last known good artifact is missing")
                lkg_cleanup_missing = True
            else:
                if lkg_opened.validation.artifact_digest != lkg_artifact_digest:
                    raise OSError("last known good artifact mismatch")
                lkg_workspace = lkg_opened.workspace
        handle = _PackageCandidate(
            operation_id=operation_id,
            target=self._target,
            candidate_path=candidate_path,
            lkg_path=lkg_path,
            journal_path=self._journal,
            project_id=value["project_id"],
            candidate_workspace=(None if candidate is None else candidate.workspace),
            candidate_workspace_digest=candidate_workspace_digest,
            last_known_good_workspace=lkg_workspace,
            candidate_artifact_digest=candidate_artifact_digest,
            last_known_good_artifact_digest=lkg_artifact_digest,
            requested_document_ids=(
                ()
                if candidate is None
                else tuple(item.document_id for item in candidate.workspace.documents)
            ),
            phase=phase,
            parent_device=value["parent_device"],
            parent_inode=value["parent_inode"],
        )
        if (
            value["schema"] != "localcat-project-package-journal-v1"
            or (handle.parent_device, handle.parent_inode)
            != (self._parent_facts.device, self._parent_facts.inode)
            or value["last_known_good_workspace_digest"]
            != (
                value["last_known_good_workspace_digest"]
                if lkg_cleanup_missing
                else (
                    None
                    if lkg_workspace is None
                    else workspace_content_digest_v1(lkg_workspace)
                )
            )
        ):
            raise OSError("package recovery facts mismatch")
        self._handles[operation_id] = handle
        return handle

    def describe_pending_recovery(
        self, recovery_handle: object
    ) -> PendingRecoveryFacts:
        handle = self._require(recovery_handle)
        return PendingRecoveryFacts(
            operation_id=handle.operation_id,
            project_id=handle.project_id,
            phase=handle.phase,
            candidate_digest=handle.candidate_workspace_digest,
            last_known_good_digest=(
                None
                if handle.last_known_good_workspace is None
                else workspace_content_digest_v1(handle.last_known_good_workspace)
            ),
        )

    def read_recovery_last_known_good(
        self, recovery_handle: object
    ) -> ProjectWorkspace | None:
        return self._require(recovery_handle).last_known_good_workspace

    def read_recovery_candidate(self, recovery_handle: object) -> ProjectWorkspace:
        handle = self._require(recovery_handle)
        return self.readback_candidate(handle)

    def complete_pending_commit(self, recovery_handle: object) -> ProjectWorkspace:
        self._require_bound_parent()
        handle = self._require(recovery_handle)
        if handle.phase not in {
            RecoveryPhase.PUBLISHING,
            RecoveryPhase.PUBLISHED,
            RecoveryPhase.COMMIT_UNCERTAIN,
        }:
            raise OSError("pending candidate was not published")
        candidate_source = (
            self._candidate_source_for_publishing(handle)
            if handle.phase is RecoveryPhase.PUBLISHING
            else handle.target
        )
        if candidate_source != handle.target:
            _replace_in_bound_parent(
                candidate_source,
                handle.target,
                self._parent_facts,
            )
        workspace = self.readback_candidate(handle)
        self._clean_residue(handle)
        return workspace

    def rollback_pending(
        self, recovery_handle: object
    ) -> ProjectWorkspace | None:
        return self.rollback_candidate(recovery_handle)

    def abandon_staged_copy(
        self, recovery_handle: object
    ) -> ProjectWorkspace | None:
        self._require_bound_parent()
        handle = self._require(recovery_handle)
        if handle.phase not in {
            RecoveryPhase.STAGING,
            RecoveryPhase.STAGED,
            RecoveryPhase.ARMED,
        }:
            raise OSError("published package cannot be abandoned")
        workspace = handle.last_known_good_workspace
        self._clean_residue(handle)
        return workspace


@dataclass(frozen=True, slots=True)
class _ImportPlan:
    operation_id: str
    source: Path
    destination: Path
    source_facts: _FileFacts
    destination_facts: _FileFacts | None
    source_parent_facts: _ParentFacts
    destination_parent_facts: _ParentFacts
    opened: OpenedProjectPackage
    preview: ProjectPackageImportPreview
    destination_opened: OpenedProjectPackage | None
    workspace_service: ProjectWorkspaceService | None
    reconciliation_service: ProjectWorkspaceService | None
    reconciliation_operation_id: str | None
    active_session_id: str | None
    active_revision: int | None
    active_workspace_digest: str | None


@dataclass(frozen=True, slots=True)
class _PreparedImportPlan:
    token: PreparedProjectPackageImport
    plan: _ImportPlan
    candidate_service: ProjectWorkspaceService
    reconciliation_receipt: ReconciliationReceipt | None


class ProjectPackageService:
    """Public Application surface for ProjectPackage v1."""

    def __init__(self) -> None:
        self._import_plans: dict[str, _ImportPlan] = {}
        self._prepared_import_plans: dict[str, _PreparedImportPlan] = {}
        self._recovery_ports: dict[
            str,
            tuple[
                Path,
                _ProjectPackagePersistencePort,
                RecoveryPhase,
                str,
                str | None,
                tuple[RecoveryAction, ...],
            ],
        ] = {}

    def validate(self, source: Path) -> ProjectPackageValidationReport:
        return _validate_artifact(_selected_user_path(source)).validation

    def open(self, source: Path) -> OpenedProjectPackage:
        return _validate_artifact(_selected_user_path(source))

    def inspect_recovery(
        self,
        destination: Path,
    ) -> ProjectPackageRecoveryPreview | None:
        if not isinstance(destination, Path):
            raise TypeError("destination must be pathlib.Path")
        target = _selected_user_path(destination)
        port = _ProjectPackagePersistencePort(target, None, ())
        try:
            handle = port.inspect_pending_recovery()
        except OSError as error:
            raise ProjectWorkspaceError("PROJECT.PACKAGE.RECOVERY_REQUIRED") from error
        if handle is None:
            return None
        facts = port.describe_pending_recovery(handle)
        actions = (
            (RecoveryAction.ABANDON_STAGED_COPY,)
            if facts.phase in {
                RecoveryPhase.STAGING,
                RecoveryPhase.STAGED,
                RecoveryPhase.ARMED,
            }
            else (
                (RecoveryAction.COMPLETE_COMMIT,)
                if (
                    facts.phase is RecoveryPhase.COMMIT_UNCERTAIN
                    and type(handle) is _PackageCandidate
                    and handle.last_known_good_artifact_digest is not None
                    and handle.last_known_good_workspace is None
                )
                else (RecoveryAction.COMPLETE_COMMIT, RecoveryAction.ROLLBACK)
            )
        )
        self._recovery_ports[facts.operation_id] = (
            target,
            port,
            facts.phase,
            facts.candidate_digest,
            facts.last_known_good_digest,
            actions,
        )
        return ProjectPackageRecoveryPreview(
            operation_id=facts.operation_id,
            project_id=facts.project_id,
            phase=facts.phase,
            last_known_good_digest=facts.last_known_good_digest,
            candidate_digest=facts.candidate_digest,
            available_actions=actions,
            safe_codes=(),
        )

    def recover(
        self,
        destination: Path,
        operation_id: str,
        choice: RecoveryAction,
    ) -> ProjectRecoveryReport:
        if not isinstance(destination, Path):
            raise TypeError("destination must be pathlib.Path")
        target = _selected_user_path(destination)
        plan = self._recovery_ports.pop(operation_id, None)
        if plan is None or plan[0] != target or choice not in plan[5]:
            _fail("PROJECT.PACKAGE.RECOVERY_REQUIRED")
        port = plan[1]
        try:
            handle = port.inspect_pending_recovery()
            if handle is None:
                raise OSError("recovery journal disappeared")
            facts = port.describe_pending_recovery(handle)
            if (
                facts.operation_id != operation_id
                or facts.phase is not plan[2]
                or facts.candidate_digest != plan[3]
                or facts.last_known_good_digest != plan[4]
            ):
                raise OSError("recovery preview is stale")
            if choice is RecoveryAction.COMPLETE_COMMIT:
                recovered = port.complete_pending_commit(handle)
                state = SaveJournalState.COMMITTED
            elif choice is RecoveryAction.ROLLBACK:
                recovered = port.rollback_pending(handle)
                state = SaveJournalState.ROLLED_BACK
            else:
                recovered = port.abandon_staged_copy(handle)
                state = SaveJournalState.CLEAN
            if port.inspect_pending_recovery() is not None:
                raise OSError("recovery cleanup is incomplete")
        except OSError:
            return ProjectRecoveryReport(
                operation_id=operation_id,
                action=choice,
                journal_state=SaveJournalState.RECOVERY_REQUIRED,
                workspace_content_digest=plan[4],
                recovery_required=True,
                retryable=True,
                safe_code="PROJECT.SAVE.RECOVERY_REQUIRED",
            )
        return ProjectRecoveryReport(
            operation_id=operation_id,
            action=choice,
            journal_state=state,
            workspace_content_digest=(
                None if recovered is None else workspace_content_digest_v1(recovered)
            ),
            recovery_required=False,
            retryable=False,
        )

    @staticmethod
    def _export_receipt(
        report: ProjectSaveReport,
        port: _ProjectPackagePersistencePort,
        *,
        operation_kind: ProjectPackageOperationKind,
    ) -> ProjectPackageExportReceipt | None:
        if report.journal_state is not SaveJournalState.COMMITTED:
            return None
        opened = port.committed_opened
        if opened is None:
            raise ProjectWorkspaceError("PROJECT.PACKAGE.RECOVERY_REQUIRED")
        return ProjectPackageExportReceipt(
            receipt_schema=PROJECT_PACKAGE_RECEIPT_SCHEMA,
            operation_id=report.operation_id,
            project_id=opened.workspace.project_id,
            carrier_profile=PROJECT_PACKAGE_CARRIER_PROFILE,
            manifest_schema=PROJECT_PACKAGE_MANIFEST_SCHEMA,
            artifact_digest=opened.validation.artifact_digest,
            workspace_content_digest=opened.validation.workspace_content_digest,
            destination_before_digest=port.destination_before_digest,
            document_count=opened.validation.document_count,
            segment_count=opened.validation.segment_count,
            member_count=opened.validation.member_count,
            byte_count=opened.validation.artifact_byte_count,
            durable=True,
            recovery_required=False,
            operation_kind=operation_kind,
            workspace_revision=report.workspace_revision,
            member_digests=opened._member_digests,
            document_results=tuple(
                ProjectPackageDocumentResult(
                    item.document_id,
                    "unchanged" if item.status.value == "unchanged" else "saved",
                )
                for item in report.document_results
            ),
        )

    def export_copy(
        self,
        workspace_service: ProjectWorkspaceService,
        destination: Path,
        *,
        codec_private_sources: tuple[ProjectPackageBlobSource, ...] = (),
    ) -> ProjectPackageExportReceipt:
        """Export an independent copy without adopting the caller's save baseline."""

        if type(workspace_service) is not ProjectWorkspaceService:
            raise TypeError("workspace_service must be exact ProjectWorkspaceService")
        if not isinstance(destination, Path):
            raise TypeError("destination must be pathlib.Path")
        target = _selected_user_path(destination)
        clone = ProjectWorkspaceService(
            workspace_service.workspace,
            workspace_service.origin_binding,
            session_id=workspace_service.session_id,
            revision=workspace_service.revision,
        )
        disposable = ProjectSaveService(clone, baseline=None)
        port = _ProjectPackagePersistencePort(
            target,
            clone.origin_binding,
            codec_private_sources,
        )
        report = disposable.save_workspace(port)
        receipt = self._export_receipt(
            report,
            port,
            operation_kind=ProjectPackageOperationKind.EXPORT_COPY,
        )
        if receipt is None:
            if report.recovery_required:
                _fail("PROJECT.PACKAGE.RECOVERY_REQUIRED")
            _fail("PROJECT.PACKAGE.APPLY_FAILED")
        return receipt

    def save_workspace(
        self,
        save_service: ProjectSaveService,
        destination: Path,
        *,
        codec_private_sources: tuple[ProjectPackageBlobSource, ...] = (),
        persistence_binding: ProjectPackagePersistenceBinding | None = None,
    ) -> ProjectPackageExportResult:
        if type(save_service) is not ProjectSaveService:
            raise TypeError("save_service must be exact ProjectSaveService")
        if not isinstance(destination, Path):
            raise TypeError("destination must be pathlib.Path")
        target = _selected_user_path(destination)
        if save_service.saved_workspace_snapshot is not None:
            if persistence_binding is None:
                raise TypeError(
                    "bound package save requires an exact persistence binding"
                )
            if persistence_binding.path != target:
                _fail("PROJECT.PACKAGE.DESTINATION_STALE")
        elif persistence_binding is not None:
            raise TypeError("first package save must not supply a persistence binding")
        port = _ProjectPackagePersistencePort(
            target,
            save_service.workspace_service.origin_binding,
            codec_private_sources,
            persistence_binding=persistence_binding,
        )
        report = save_service.save_workspace(port)
        receipt = self._export_receipt(
            report,
            port,
            operation_kind=ProjectPackageOperationKind.SAVE,
        )
        binding = (
            None
            if port.committed_opened is None
            else port.committed_opened.persistence_binding
        )
        return ProjectPackageExportResult(report, receipt, binding)

    def save_document(
        self,
        save_service: ProjectSaveService,
        document_id: str,
        destination: Path,
        *,
        codec_private_sources: tuple[ProjectPackageBlobSource, ...] = (),
        persistence_binding: ProjectPackagePersistenceBinding,
    ) -> ProjectPackageExportResult:
        """Persist one document baseline through the same package transaction."""

        if type(save_service) is not ProjectSaveService:
            raise TypeError("save_service must be exact ProjectSaveService")
        if type(document_id) is not str:
            raise TypeError("document_id must be exact str")
        if not isinstance(destination, Path):
            raise TypeError("destination must be pathlib.Path")
        if type(persistence_binding) is not ProjectPackagePersistenceBinding:
            raise TypeError("document save requires an exact persistence binding")
        target = _selected_user_path(destination)
        if persistence_binding.path != target:
            _fail("PROJECT.PACKAGE.DESTINATION_STALE")
        port = _ProjectPackagePersistencePort(
            target,
            save_service.workspace_service.origin_binding,
            codec_private_sources,
            persistence_binding=persistence_binding,
        )
        report = save_service.save_document(document_id, port)
        receipt = self._export_receipt(
            report,
            port,
            operation_kind=ProjectPackageOperationKind.SAVE,
        )
        binding = (
            None
            if port.committed_opened is None
            else port.committed_opened.persistence_binding
        )
        return ProjectPackageExportResult(report, receipt, binding)

    def export_workspace(
        self,
        save_service: ProjectSaveService,
        destination: Path,
        *,
        codec_private_sources: tuple[ProjectPackageBlobSource, ...] = (),
    ) -> ProjectPackageExportResult:
        """Compatibility spelling for the bound-save operation."""

        target = _selected_user_path(destination)
        inferred_binding = None
        if save_service.saved_workspace_snapshot is not None:
            inferred_binding = self.open(target).persistence_binding
        return self.save_workspace(
            save_service,
            target,
            codec_private_sources=codec_private_sources,
            persistence_binding=inferred_binding,
        )

    def preview_import(
        self,
        source: Path,
        destination: Path,
        *,
        workspace_service: ProjectWorkspaceService | None = None,
        associations: tuple[ReconciliationAssociation, ...] = (),
    ) -> ProjectPackageImportPreview:
        if not isinstance(source, Path) or not isinstance(destination, Path):
            raise TypeError("package paths must be pathlib.Path")
        source_path = _selected_user_path(source)
        destination_path = _selected_user_path(destination)
        source_parent_facts = _bind_parent(source_path)
        destination_parent_facts = _bind_parent(destination_path)
        opened = _validate_artifact(source_path)
        if workspace_service is not None and type(
            workspace_service
        ) is not ProjectWorkspaceService:
            raise TypeError("workspace_service must be exact or None")
        if type(associations) is not tuple or any(
            type(item) is not ReconciliationAssociation for item in associations
        ):
            raise TypeError("associations must be an exact tuple")
        source_descriptor, source_facts = _open_regular(
            source_path,
            include_digest=True,
        )
        os.close(source_descriptor)
        destination_facts = None
        destination_project_id = None
        destination_opened = None
        if destination_path.exists():
            destination_descriptor, destination_facts = _open_regular(
                destination_path,
                include_digest=True,
            )
            os.close(destination_descriptor)
            try:
                destination_opened = _validate_artifact(destination_path)
                destination_project_id = destination_opened.workspace.project_id
            except ProjectWorkspaceError:
                destination_project_id = None
        same_project = destination_project_id == opened.workspace.project_id
        mode = (
            ProjectPackageImportMode.NEW
            if destination_facts is None
            else (
                ProjectPackageImportMode.UPDATE_SAME_PROJECT
                if same_project
                else ProjectPackageImportMode.REPLACE
            )
        )
        operation_id = "package-import-" + secrets.token_hex(32)
        validation = opened.validation
        reconciliation_service = None
        reconciliation_operation_id = None
        active_session_id = None
        active_revision = None
        active_workspace_digest = None
        reconciliation_preview = None
        if same_project:
            if (
                workspace_service is None
                or workspace_service.workspace.project_id != opened.workspace.project_id
            ):
                _fail("PROJECT.PACKAGE.PREVIEW_STALE")
            reconciliation_service = ProjectWorkspaceService(
                workspace_service.workspace,
                workspace_service.origin_binding,
                session_id=workspace_service.session_id,
                revision=workspace_service.revision,
            )
            reconciliation_preview = (
                reconciliation_service.stage_workspace_reconciliation(
                    opened.workspace,
                    associations=associations,
                    session_id=workspace_service.session_id,
                    base_revision=workspace_service.revision,
                )
            )
            reconciliation_operation_id = reconciliation_preview.operation_id
            active_session_id = workspace_service.session_id
            active_revision = workspace_service.revision
            active_workspace_digest = workspace_service.workspace_content_digest
        preview = ProjectPackageImportPreview(
            operation_id=operation_id,
            mode=mode,
            project_id=opened.workspace.project_id,
            project_name=opened.workspace.name,
            source_artifact_digest=validation.artifact_digest,
            destination_before_digest=(
                None if destination_facts is None else destination_facts.digest
            ),
            workspace_content_digest=validation.workspace_content_digest,
            document_count=validation.document_count,
            segment_count=validation.segment_count,
            editing_state_count=validation.editing_state_count,
            opaque_member_count=validation.opaque_member_count,
            destination_exists=destination_facts is not None,
            same_project=same_project,
            unchanged_count=(
                0
                if reconciliation_preview is None
                else len(reconciliation_preview.unchanged_identities)
            ),
            source_changed_count=(
                0
                if reconciliation_preview is None
                else len(reconciliation_preview.source_changed_identities)
            ),
            new_count=(
                0
                if reconciliation_preview is None
                else len(reconciliation_preview.new_identities)
            ),
            removed_count=(
                0
                if reconciliation_preview is None
                else len(reconciliation_preview.removed_identities)
            ),
            ambiguous_count=(
                0
                if reconciliation_preview is None
                else len(reconciliation_preview.ambiguous_identities)
            ),
            unresolved_count=(
                0
                if reconciliation_preview is None
                else len(reconciliation_preview.unresolved_identities)
            ),
            blocking_reasons=(
                ()
                if reconciliation_preview is None
                or not reconciliation_preview.required_decision_identities
                else ("PROJECT.RECONCILE.DECISION_REQUIRED",)
            ),
            required_decision_identities=(
                ()
                if reconciliation_preview is None
                else reconciliation_preview.required_decision_identities
            ),
        )
        self._import_plans[operation_id] = _ImportPlan(
            operation_id,
            source_path,
            destination_path,
            source_facts,
            destination_facts,
            source_parent_facts,
            destination_parent_facts,
            opened,
            preview,
            destination_opened,
            workspace_service,
            reconciliation_service,
            reconciliation_operation_id,
            active_session_id,
            active_revision,
            active_workspace_digest,
        )
        return preview

    @staticmethod
    def _require_import_plan_files_current(plan: _ImportPlan) -> None:
        _require_parent(
            plan.source,
            plan.source_parent_facts,
            code="PROJECT.PACKAGE.SOURCE_STALE",
        )
        _require_parent(plan.destination, plan.destination_parent_facts)
        source_descriptor, source_facts = _open_regular(
            plan.source,
            include_digest=True,
        )
        os.close(source_descriptor)
        if source_facts != plan.source_facts:
            _fail("PROJECT.PACKAGE.SOURCE_STALE")
        if plan.destination_facts is None:
            if plan.destination.exists():
                _fail("PROJECT.PACKAGE.DESTINATION_STALE")
        else:
            destination_descriptor, destination_facts = _open_regular(
                plan.destination,
                include_digest=True,
            )
            os.close(destination_descriptor)
            if destination_facts != plan.destination_facts:
                _fail("PROJECT.PACKAGE.DESTINATION_STALE")

    @staticmethod
    def _require_active_import_plan_current(plan: _ImportPlan) -> None:
        if plan.preview.mode is not ProjectPackageImportMode.UPDATE_SAME_PROJECT:
            return
        active = plan.workspace_service
        if (
            active is None
            or active.session_id != plan.active_session_id
            or active.revision != plan.active_revision
            or active.workspace_content_digest != plan.active_workspace_digest
        ):
            _fail("PROJECT.PACKAGE.PREVIEW_STALE")

    def prepare_import(
        self,
        operation_id: str,
        *,
        decisions: tuple[ReconciliationDecision, ...] = (),
        session_id: str | None = None,
        base_revision: int | None = None,
    ) -> PreparedProjectPackageImport:
        """Materialize one exact candidate without publishing package bytes."""

        if type(operation_id) is not str:
            raise TypeError("operation_id must be exact str")
        plan = self._import_plans.pop(operation_id, None)
        if plan is None:
            _fail("PROJECT.PACKAGE.PREVIEW_STALE")
        self._require_import_plan_files_current(plan)
        self._require_active_import_plan_current(plan)

        reconciliation_receipt = None
        if plan.preview.mode is ProjectPackageImportMode.UPDATE_SAME_PROJECT:
            reconciliation_service = plan.reconciliation_service
            if (
                reconciliation_service is None
                or plan.reconciliation_operation_id is None
                or session_id != plan.active_session_id
                or base_revision != plan.active_revision
            ):
                _fail("PROJECT.PACKAGE.PREVIEW_STALE")
            reconciliation_receipt = (
                reconciliation_service.apply_workspace_reconciliation(
                    plan.reconciliation_operation_id,
                    incoming=plan.opened.workspace,
                    decisions=decisions,
                    session_id=session_id,
                    base_revision=base_revision,
                )
            )
            candidate_service = reconciliation_service
        else:
            if decisions or session_id is not None or base_revision is not None:
                _fail("PROJECT.PACKAGE.PREVIEW_STALE")
            candidate_service = plan.opened.create_workspace_service(
                session_id="package-import-candidate",
                revision=0,
            )

        token = PreparedProjectPackageImport(
            operation_id=operation_id,
            project_id=candidate_service.workspace.project_id,
            mode=plan.preview.mode,
            workspace_revision=candidate_service.revision,
            workspace_content_digest=candidate_service.workspace_content_digest,
        )
        self._prepared_import_plans[operation_id] = _PreparedImportPlan(
            token,
            plan,
            candidate_service,
            reconciliation_receipt,
        )
        return token

    def create_prepared_import_save_service(
        self,
        prepared: PreparedProjectPackageImport,
        *,
        session_id: str,
    ) -> ProjectSaveService:
        """Build the C3 candidate session from package-owned prepared facts."""

        if type(prepared) is not PreparedProjectPackageImport:
            raise TypeError("prepared import must be exact")
        retained = self._prepared_import_plans.get(prepared.operation_id)
        if retained is None or retained.token is not prepared:
            _fail("PROJECT.PACKAGE.PREVIEW_STALE")
        candidate = retained.candidate_service
        service = ProjectWorkspaceService(
            candidate.workspace,
            candidate.origin_binding,
            session_id=session_id,
            revision=candidate.revision,
        )
        baseline = WorkspaceSaveBaseline.from_workspace(
            service.workspace,
            workspace_revision=service.revision,
        )
        return ProjectSaveService(service, baseline=baseline)

    def discard_prepared_import(
        self,
        prepared: PreparedProjectPackageImport,
    ) -> None:
        """Revoke one uncommitted candidate while leaving artifacts untouched."""

        if type(prepared) is not PreparedProjectPackageImport:
            raise TypeError("prepared import must be exact")
        retained = self._prepared_import_plans.get(prepared.operation_id)
        if retained is None:
            return
        if retained.token is not prepared:
            _fail("PROJECT.PACKAGE.PREVIEW_STALE")
        del self._prepared_import_plans[prepared.operation_id]

    def commit_prepared_import(
        self,
        prepared: PreparedProjectPackageImport,
    ) -> ProjectPackageImportResult:
        """Publish exactly one previously materialized package candidate."""

        if type(prepared) is not PreparedProjectPackageImport:
            raise TypeError("prepared import must be exact")
        retained = self._prepared_import_plans.get(prepared.operation_id)
        if retained is None or retained.token is not prepared:
            _fail("PROJECT.PACKAGE.PREVIEW_STALE")
        del self._prepared_import_plans[prepared.operation_id]
        plan = retained.plan
        candidate_service = retained.candidate_service
        reconciliation_receipt = retained.reconciliation_receipt
        self._require_import_plan_files_current(plan)
        self._require_active_import_plan_current(plan)

        if plan.preview.mode in {
            ProjectPackageImportMode.NEW,
            ProjectPackageImportMode.UPDATE_SAME_PROJECT,
        }:
            if plan.preview.mode is ProjectPackageImportMode.UPDATE_SAME_PROJECT:
                assert plan.destination_opened is not None
                assert plan.active_revision is not None
                baseline = WorkspaceSaveBaseline.from_workspace(
                    plan.destination_opened.workspace,
                    workspace_revision=plan.active_revision,
                )
                persistence_binding = plan.destination_opened.persistence_binding
            else:
                baseline = None
                persistence_binding = None
            save_service = ProjectSaveService(candidate_service, baseline=baseline)
            port = _ProjectPackagePersistencePort(
                plan.destination,
                candidate_service.origin_binding,
                (),
                persistence_binding=persistence_binding,
                additional_package_sources=(plan.opened,),
            )
            report = save_service.save_workspace(port)
            if report.journal_state is not SaveJournalState.COMMITTED:
                _fail(
                    "PROJECT.PACKAGE.RECOVERY_REQUIRED"
                    if report.recovery_required
                    else "PROJECT.PACKAGE.APPLY_FAILED"
                )
            installed = port.committed_opened
            if installed is None:
                _fail("PROJECT.PACKAGE.RECOVERY_REQUIRED")
            validation = installed.validation
            mode = plan.preview.mode
            receipt = ProjectPackageImportReceipt(
                receipt_schema=PROJECT_PACKAGE_RECEIPT_SCHEMA,
                operation_id=prepared.operation_id,
                project_id=installed.workspace.project_id,
                carrier_profile=PROJECT_PACKAGE_CARRIER_PROFILE,
                manifest_schema=PROJECT_PACKAGE_MANIFEST_SCHEMA,
                source_artifact_digest=plan.source_facts.digest,
                destination_before_digest=(
                    None
                    if plan.destination_facts is None
                    else plan.destination_facts.digest
                ),
                destination_after_digest=validation.artifact_digest,
                workspace_content_digest=validation.workspace_content_digest,
                document_count=validation.document_count,
                segment_count=validation.segment_count,
                member_count=validation.member_count,
                byte_count=validation.artifact_byte_count,
                durable=True,
                recovery_required=False,
                operation_kind=(
                    ProjectPackageOperationKind.RECONCILE_IMPORT
                    if mode is ProjectPackageImportMode.UPDATE_SAME_PROJECT
                    else ProjectPackageOperationKind.IMPORT
                ),
                mode=mode,
                workspace_revision=candidate_service.revision,
                member_digests=installed._member_digests,
                document_results=tuple(
                    ProjectPackageDocumentResult(
                        document.document_id,
                        (
                            "reconciled"
                            if mode is ProjectPackageImportMode.UPDATE_SAME_PROJECT
                            else "installed"
                        ),
                    )
                    for document in installed.workspace.documents
                ),
                reconciliation=reconciliation_receipt,
            )
            return ProjectPackageImportResult(installed, receipt)

        if plan.destination_opened is None:
            _fail("PROJECT.PACKAGE.DESTINATION_STALE")
        port = _ProjectPackagePersistencePort(
            plan.destination,
            None,
            (),
            persistence_binding=plan.destination_opened.persistence_binding,
            additional_package_sources=(plan.opened,),
            allow_cross_project_lkg=True,
        )
        handle = None
        try:
            handle = port.stage_candidate(
                operation_id=prepared.operation_id,
                candidate_workspace=plan.opened.workspace,
                last_known_good_workspace=plan.destination_opened.workspace,
                requested_document_ids=tuple(
                    item.document_id for item in plan.opened.workspace.documents
                ),
            )
            port.validate_candidate(handle)
            port.arm_publication(handle)
            port.publish_candidate(handle)
            if port.readback_candidate(handle) != plan.opened.workspace:
                raise OSError("installed package workspace mismatch")
            port.commit_candidate(handle)
            if port.readback_candidate(handle) != plan.opened.workspace:
                raise OSError("installed package final readback mismatch")
            if port.inspect_pending_recovery() is not None:
                raise OSError("installed package recovery residue remains")
        except OSError as error:
            if handle is not None:
                try:
                    rolled_back = port.rollback_candidate(handle)
                except OSError:
                    rolled_back = object()
                if rolled_back == plan.destination_opened.workspace:
                    try:
                        residue = port.inspect_pending_recovery()
                    except OSError:
                        residue = object()
                    if residue is None:
                        raise ProjectWorkspaceError(
                            "PROJECT.PACKAGE.APPLY_FAILED"
                        ) from error
            raise ProjectWorkspaceError("PROJECT.PACKAGE.RECOVERY_REQUIRED") from error
        installed = port.committed_opened
        if installed is None:
            _fail("PROJECT.PACKAGE.RECOVERY_REQUIRED")
        validation = installed.validation
        receipt = ProjectPackageImportReceipt(
            receipt_schema=PROJECT_PACKAGE_RECEIPT_SCHEMA,
            operation_id=prepared.operation_id,
            project_id=installed.workspace.project_id,
            carrier_profile=PROJECT_PACKAGE_CARRIER_PROFILE,
            manifest_schema=PROJECT_PACKAGE_MANIFEST_SCHEMA,
            source_artifact_digest=plan.source_facts.digest,
            destination_before_digest=(
                None
                if plan.destination_facts is None
                else plan.destination_facts.digest
            ),
            destination_after_digest=validation.artifact_digest,
            workspace_content_digest=validation.workspace_content_digest,
            document_count=validation.document_count,
            segment_count=validation.segment_count,
            member_count=validation.member_count,
            byte_count=validation.artifact_byte_count,
            durable=True,
            recovery_required=False,
            operation_kind=ProjectPackageOperationKind.IMPORT,
            mode=ProjectPackageImportMode.REPLACE,
            workspace_revision=0,
            member_digests=installed._member_digests,
            document_results=tuple(
                ProjectPackageDocumentResult(document.document_id, "installed")
                for document in installed.workspace.documents
            ),
        )
        return ProjectPackageImportResult(installed, receipt)

    def apply_import(
        self,
        operation_id: str,
        *,
        decisions: tuple[ReconciliationDecision, ...] = (),
        session_id: str | None = None,
        base_revision: int | None = None,
    ) -> ProjectPackageImportReceipt:
        prepared = self.prepare_import(
            operation_id,
            decisions=decisions,
            session_id=session_id,
            base_revision=base_revision,
        )
        return self.commit_prepared_import(prepared).receipt

    def apply_import_result(
        self,
        operation_id: str,
        *,
        decisions: tuple[ReconciliationDecision, ...] = (),
        session_id: str | None = None,
        base_revision: int | None = None,
    ) -> ProjectPackageImportResult:
        """Apply once and return the independently cold-opened installed artifact."""

        if type(operation_id) is not str:
            raise TypeError("operation_id must be exact str")
        prepared = self.prepare_import(
            operation_id,
            decisions=decisions,
            session_id=session_id,
            base_revision=base_revision,
        )
        return self.commit_prepared_import(prepared)


__all__ = (
    "PROJECT_PACKAGE_CARRIER_PROFILE",
    "PROJECT_PACKAGE_DOCUMENT_SCHEMA",
    "PROJECT_PACKAGE_MANIFEST_SCHEMA",
    "PROJECT_PACKAGE_RECEIPT_SCHEMA",
    "OpenedProjectPackage",
    "ProjectPackageBlobSource",
    "ProjectPackageCodecAvailability",
    "ProjectPackageDocumentEntry",
    "ProjectPackageDocumentResult",
    "ProjectPackageExportReceipt",
    "ProjectPackageExportResult",
    "ProjectPackageImportMode",
    "ProjectPackageImportPreview",
    "ProjectPackageImportReceipt",
    "ProjectPackageImportResult",
    "ProjectPackageManifest",
    "ProjectPackageMemberDigest",
    "ProjectPackageMemberReference",
    "ProjectPackageOperationKind",
    "ProjectPackagePersistenceBinding",
    "ProjectPackageRecoveryPreview",
    "ProjectPackageService",
    "ProjectPackageValidationReport",
    "PreparedProjectPackageImport",
)
