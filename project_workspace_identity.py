"""Stable identity, path, and digest primitives for LocalCAT workspaces."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import unicodedata


MAX_LOCAL_SEGMENT_ID_BYTES = 1_024
MAX_PORTABLE_REF_BYTES = 1_024
MAX_PORTABLE_REF_SEGMENT_BYTES = 255
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_PROJECT_ID = re.compile(r"prj-[0-9a-f]{64}\Z")
_DOCUMENT_ID = re.compile(r"doc-[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
_WINDOWS_FORBIDDEN_FILENAME_CHARACTERS = frozenset('<>"|?*')
_WORKSPACE_ERROR_CODES = frozenset(
    {
        "PROJECT.WORKSPACE.CONTRACT_INVALID",
        "PROJECT.WORKSPACE.IDENTITY_DUPLICATE",
        "PROJECT.WORKSPACE.PATH_INVALID",
        "PROJECT.WORKSPACE.LIMIT_EXCEEDED",
        "PROJECT.WORKSPACE.SESSION_STALE",
        "PROJECT.INTAKE.INPUT_INVALID",
        "PROJECT.INTAKE.SOURCE_UNSAFE",
        "PROJECT.INTAKE.SOURCE_STALE",
        "PROJECT.RECONCILE.INPUT_INVALID",
        "PROJECT.RECONCILE.PREVIEW_STALE",
        "PROJECT.RECONCILE.DECISION_REQUIRED",
        "PROJECT.RECONCILE.SOURCE_STALE",
        "PROJECT.RECONCILE.APPLY_FAILED",
        "PROJECT.SAVE.WRITER_UNAVAILABLE",
        "PROJECT.SAVE.STAGE_FAILED",
        "PROJECT.SAVE.VALIDATION_FAILED",
        "PROJECT.SAVE.SOURCE_STALE",
        "PROJECT.SAVE.COMMIT_FAILED",
        "PROJECT.SAVE.ROLLBACK_FAILED",
        "PROJECT.SAVE.RECOVERY_REQUIRED",
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
    }
)


class ProjectWorkspaceError(RuntimeError):
    """Body-safe deterministic workspace failure."""

    __slots__ = ("_code", "_retryable", "_frozen")

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        if type(code) is not str or code not in _WORKSPACE_ERROR_CODES:
            raise ValueError("workspace error code must be a stable PROJECT code")
        if type(retryable) is not bool:
            raise TypeError("workspace retryable flag must be exact bool")
        object.__setattr__(self, "_code", code)
        object.__setattr__(self, "_retryable", retryable)
        super().__init__(code)
        object.__setattr__(self, "_frozen", True)

    @property
    def code(self) -> str:
        return self._code

    @property
    def retryable(self) -> bool:
        return self._retryable

    def __setattr__(self, name: str, value: object) -> None:
        if name in {
            "__traceback__",
            "__context__",
            "__cause__",
            "__suppress_context__",
            "__notes__",
        }:
            BaseException.__setattr__(self, name, value)
            return
        if getattr(self, "_frozen", False):
            raise AttributeError("workspace error is immutable")
        raise AttributeError("workspace error state is constructor-owned")

    def __str__(self) -> str:
        return self.code


def _fail(code: str) -> None:
    raise ProjectWorkspaceError(code)


def _exact_text(value: object, *, code: str) -> str:
    if type(value) is not str or not value:
        _fail(code)
    assert type(value) is str
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail(code)
    if not encoded:
        _fail(code)
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cs"}:
            _fail(code)
    return value


def _exact_utf8_text(value: object, *, allow_empty: bool) -> str:
    if type(value) is not str or (not allow_empty and not value):
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    assert type(value) is str
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    if any(unicodedata.category(character) == "Cs" for character in value):
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    return value


def validate_sha256(value: object) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    return value


def validate_project_id(value: object) -> str:
    if type(value) is not str or _PROJECT_ID.fullmatch(value) is None:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    return value


def validate_document_id(value: object) -> str:
    if type(value) is not str or _DOCUMENT_ID.fullmatch(value) is None:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    return value


def validate_local_segment_id(value: object) -> str:
    text = _exact_text(value, code="PROJECT.WORKSPACE.CONTRACT_INVALID")
    if not text.strip():
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    if len(text.encode("utf-8")) > MAX_LOCAL_SEGMENT_ID_BYTES:
        _fail("PROJECT.WORKSPACE.LIMIT_EXCEEDED")
    return text


def normalize_portable_ref_v1(value: object) -> str:
    """Return one canonical portable relative reference or fail closed.

    Raw spelling is validated before Unicode normalization so ambiguous path
    syntax is never silently repaired by ``Path`` or ``PurePath``.
    """

    raw = _exact_text(value, code="PROJECT.WORKSPACE.PATH_INVALID")
    if len(raw.encode("utf-8")) > MAX_PORTABLE_REF_BYTES:
        _fail("PROJECT.WORKSPACE.LIMIT_EXCEEDED")
    if "\\" in raw or raw.startswith("/") or raw.endswith("/"):
        _fail("PROJECT.WORKSPACE.PATH_INVALID")
    raw_segments = raw.split("/")
    if any(segment in {"", ".", ".."} for segment in raw_segments):
        _fail("PROJECT.WORKSPACE.PATH_INVALID")
    if any(":" in segment for segment in raw_segments):
        _fail("PROJECT.WORKSPACE.PATH_INVALID")

    normalized = unicodedata.normalize("NFC", raw)
    if len(normalized.encode("utf-8")) > MAX_PORTABLE_REF_BYTES:
        _fail("PROJECT.WORKSPACE.LIMIT_EXCEEDED")
    segments = normalized.split("/")
    for segment in segments:
        encoded = segment.encode("utf-8")
        if not encoded or len(encoded) > MAX_PORTABLE_REF_SEGMENT_BYTES:
            _fail("PROJECT.WORKSPACE.LIMIT_EXCEEDED")
        if segment.endswith((" ", ".")):
            _fail("PROJECT.WORKSPACE.PATH_INVALID")
        if any(
            character in _WINDOWS_FORBIDDEN_FILENAME_CHARACTERS
            for character in segment
        ):
            _fail("PROJECT.WORKSPACE.PATH_INVALID")
        basename = segment.split(".", 1)[0].casefold()
        if basename in _WINDOWS_RESERVED_BASENAMES:
            _fail("PROJECT.WORKSPACE.PATH_INVALID")
    return normalized


def portable_ref_collision_key(value: object) -> str:
    return normalize_portable_ref_v1(value).casefold()


def validate_portable_ref_collection(
    values: object,
    *,
    allow_exact_duplicates: bool,
) -> tuple[str, ...]:
    if type(values) is not tuple:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    if type(allow_exact_duplicates) is not bool:
        raise TypeError("allow_exact_duplicates must be exact bool")
    normalized_values: list[str] = []
    by_collision_key: dict[str, str] = {}
    for value in values:
        normalized = normalize_portable_ref_v1(value)
        if normalized != value:
            _fail("PROJECT.WORKSPACE.PATH_INVALID")
        key = normalized.casefold()
        previous = by_collision_key.get(key)
        if previous is not None:
            if not allow_exact_duplicates or previous != normalized:
                _fail("PROJECT.WORKSPACE.IDENTITY_DUPLICATE")
        else:
            by_collision_key[key] = normalized
        normalized_values.append(normalized)
    return tuple(normalized_values)


def _length_prefixed_utf8(value: str) -> bytes:
    """Encode one field with an unsigned 64-bit big-endian byte length."""

    encoded = value.encode("utf-8", errors="strict")
    return len(encoded).to_bytes(8, "big", signed=False) + encoded


def _token(prefix: str, domain: bytes, field: str) -> str:
    digest = hashlib.sha256(domain + _length_prefixed_utf8(field)).hexdigest()
    return prefix + digest


def issue_project_id(seed: bytes | None = None) -> str:
    if seed is None:
        seed = secrets.token_bytes(32)
    if type(seed) is not bytes or len(seed) != 32:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    digest = hashlib.sha256(b"localcat.project.issue.v1\0" + seed).hexdigest()
    return "prj-" + digest


def derive_device_local_origin_key(absolute_lexical_path: object) -> str:
    path = _exact_text(
        absolute_lexical_path,
        code="PROJECT.WORKSPACE.PATH_INVALID",
    )
    if not os.path.isabs(path):
        _fail("PROJECT.WORKSPACE.PATH_INVALID")
    return hashlib.sha256(
        b"localcat.device-local-origin.v1\0" + _length_prefixed_utf8(path)
    ).hexdigest()


def derive_legacy_single_json_project_id(device_local_origin_key: object) -> str:
    key = validate_sha256(device_local_origin_key)
    return _token("prj-", b"localcat.project.single-json.v1\0", key)


def derive_legacy_single_json_document_id(source_ref: object) -> str:
    normalized = normalize_portable_ref_v1(source_ref)
    return _token("doc-", b"localcat.document.single-json.v1\0", normalized)


def derive_explicit_selected_document_id(source_ref: object) -> str:
    normalized = normalize_portable_ref_v1(source_ref)
    digest = hashlib.sha256(
        b"localcat.document.explicit-selected-files.v1\0"
        + normalized.encode("utf-8")
    ).hexdigest()
    return "doc-" + digest


def source_fingerprint_v1(
    source: object,
    raw_speaker: object,
    codec_source_state_digest: object = EMPTY_SHA256,
) -> str:
    source_text = _exact_utf8_text(source, allow_empty=False)
    if not source_text.strip():
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    speaker_text = _exact_utf8_text(raw_speaker, allow_empty=True)
    state_digest = validate_sha256(codec_source_state_digest)
    return hashlib.sha256(
        b"localcat.segment-source.v1\0"
        + _length_prefixed_utf8(source_text)
        + _length_prefixed_utf8(speaker_text)
        + bytes.fromhex(state_digest)
    ).hexdigest()


def editing_state_digest_v1(
    document_id: object,
    local_segment_id: object,
    source_fingerprint: object,
    target: object,
    confirmed: object,
) -> str:
    document = validate_document_id(document_id)
    local_id = validate_local_segment_id(local_segment_id)
    fingerprint = validate_sha256(source_fingerprint)
    if type(target) is not str:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    try:
        target.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    if type(confirmed) is not bool:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    return hashlib.sha256(
        b"localcat.editing-overlay.v1\0"
        + _length_prefixed_utf8(document)
        + _length_prefixed_utf8(local_id)
        + bytes.fromhex(fingerprint)
        + _length_prefixed_utf8(target)
        + (b"\x01" if confirmed else b"\x00")
    ).hexdigest()


__all__ = (
    "EMPTY_SHA256",
    "ProjectWorkspaceError",
    "derive_device_local_origin_key",
    "derive_explicit_selected_document_id",
    "derive_legacy_single_json_document_id",
    "derive_legacy_single_json_project_id",
    "editing_state_digest_v1",
    "issue_project_id",
    "normalize_portable_ref_v1",
    "portable_ref_collision_key",
    "source_fingerprint_v1",
    "validate_document_id",
    "validate_local_segment_id",
    "validate_portable_ref_collection",
    "validate_project_id",
    "validate_sha256",
)
